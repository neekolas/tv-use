"""Persistent receiver registry, per-TV routing and mirror admission."""

import asyncio
import ipaddress
import json
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from aiohttp import web
from jsonschema import Draft202012Validator

from .atv.discovery import describe
from .history import report
from .mcp_http import MCP
from .page_network import PageNetworkPolicy
from .server import TVRuntime
from .stage import ContentStore
from .state import Fault, State, write_private
from .tools.contract import ROUTED_SCHEMAS, TEXT, canonical, schema
from .tools.public import UNKNOWN, outcome

_LOG = logging.getLogger(__name__)


def record_id(value):
    return uuid.uuid5(uuid.NAMESPACE_URL, "atv-bridge:" + value.casefold()).hex


def copy_directory(target, populate):
    """Publish a complete directory or leave the previous state untouched."""
    if target.exists():
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as work:
        staging = Path(work) / "state"
        staging.mkdir(mode=0o700)
        populate(staging)
        staging.rename(target)


def migrate(controller, legacy_host, legacy_id):
    old = controller.pairings
    if not any(old.values()):
        return
    root = controller.root
    if (root / "single-tv-backup" / "migrated").exists():
        return
    target = old.get("tv") or {}
    identifier = target.get("apple_tv_id") or legacy_id
    host = target.get("last_known_host") or legacy_host
    key = record_id(identifier or host or (old.get("airplay") or {}).get("atv_id") or "legacy")
    dest = root / "tvs" / key

    def backup(directory):
        for name in (
            "identity.json",
            "internal_token",
            "pairings.json",
            "observer",
            "tvpipe",
            "media",
            "audit.jsonl",
            "audit.jsonl.1",
        ):
            source = root / name
            if source.is_dir():
                shutil.copytree(source, directory / name)
            elif source.is_file():
                shutil.copy2(source, directory / name)

    archive = root / "single-tv-backup"
    copy_directory(archive, backup)

    def restore(directory):
        for source in archive.iterdir():
            if source.name in ("identity.json", "internal_token"):
                continue
            if source.is_dir():
                shutil.copytree(source, directory / source.name)
            else:
                shutil.copy2(source, directory / source.name)
        records = json.loads((directory / "pairings.json").read_text())
        if records.get("remote_pairing"):
            records["remote_pairing"]["path"] = str(dest / "observer" / "remote.plist")
        write_private(directory / "pairings.json", json.dumps(records))
        write_private(
            directory / "receiver.json",
            json.dumps(
                {
                    "id": key,
                    "identifier": identifier,
                    "host": host,
                    "name": None,
                    "legacy": True,
                }
            ),
        )

    copy_directory(dest, restore)
    write_private(archive / "migrated", key)


class Fleet:
    def __init__(self, root, options, factory=TVRuntime, *, stage_port=8766, stage_host="0.0.0.0",
                 stage_key=None, keep_unlisted=False, page_tailnet=True, page_host_addresses=()):
        """`keep_unlisted` keeps the records of TVs that leave the configured list, instead of
        deleting them, until a listed TV matches one again. `page_tailnet=False` makes pages need an
        allow entry for tailnet addresses, and pages never reach `page_host_addresses`. Standalone
        mode sets all three."""
        self.controller = State(root)
        self.stage_port = stage_port
        self.stage_host = stage_host
        self.stage_key = stage_key
        self.stage_ports = set()
        self.page_network = PageNetworkPolicy(
            options.get("page_network_allow", []), self.stage_ports,
            tailnet=page_tailnet, host_addresses=page_host_addresses,
        )
        self.options = options
        self.factory = factory
        self.runtimes = {}
        self.runners = {}
        self.started = False
        self.enrol_lock = asyncio.Lock()
        self.mirrors = {}
        self.limit = options.get("max_active_mirrors", 1)
        if type(self.limit) is not int or self.limit < 1:
            raise ValueError("max_active_mirrors must be a positive integer")
        self.store = ContentStore(self.controller.root / "content")
        old_host = options.get("apple_tv_host") or ""
        old_id = options.get("apple_tv_id") or ""
        migrate(self.controller, old_host, old_id)
        self.registry = self.controller.load_or_create(
            "receiver-options.json", lambda: {"configured": {}, "removed": []}
        )
        removed = set(self.registry["removed"])
        self.entries = list(options.get("apple_tvs") or [])
        if old_id or old_host:
            self.entries.append({"host": old_host, "id": old_id})
        previous = self.registry["configured"]
        # Kept records stay out of service. A listed TV that matches one reattaches it in register.
        self.kept = None

        def stored(keys):
            return {key for key in keys if (self.controller.root / "tvs" / key / "receiver.json").is_file()}

        if keep_unlisted:
            listed = {self.option_key(entry) for entry in self.entries}
            self.kept = stored(set(self.registry.get("kept", [])) | set(previous.values())) - {
                key for option, key in previous.items() if option in listed
            }
        for metadata in sorted((self.controller.root / "tvs").glob("*/receiver.json")):
            if metadata.parent.name not in removed | (self.kept or set()):
                self._build(json.loads(metadata.read_text()))
        configured = {}
        for entry in self.entries:
            option = self.option_key(entry)
            key = previous.get(option)
            if key not in removed:
                key = self.register(entry).metadata["id"]
            configured[option] = key
        unlisted = set(previous.values()) - set(configured.values())
        if self.kept is None:
            removed.update(unlisted)
        else:
            self.kept = stored(self.kept | unlisted) - set(configured.values())
            for key in sorted(self.kept):
                self.runtimes.pop(key, None)
                meta = json.loads((self.controller.root / "tvs" / key / "receiver.json").read_text())
                _LOG.warning(
                    "Kept the records of TV %s (ID %s), which the TV list no longer names. "
                    "Add the TV to the list again to use its pairings.",
                    meta.get("name") or meta.get("host") or meta.get("identifier") or key, key,
                )
            self.registry["kept"] = sorted(self.kept)
        self.registry.update(configured=configured, removed=sorted(removed))
        self.save_registry()
        for key in removed:
            self.runtimes.pop(key, None)
            self.erase_state(key)
        self.registry["removed"] = sorted(removed.intersection(configured.values()))
        self.save_registry()
        # The public media listener and controller API outlive individual receivers.
        self.primary = self.factory(
            "", State(self.controller.root / "discovery", controller=self.controller)
        )
        self.primary.stage.store = self.store
        self.public_receiver = next(
            (rt for rt in self.runtimes.values() if rt.metadata.get("legacy")),
            next(iter(self.runtimes.values()), None),
        )
        self.mcp = MCP(self)

    @staticmethod
    def option_key(entry):
        return record_id(entry.get("id") or entry.get("host") or entry.get("name") or "")

    def save_registry(self):
        write_private(self.controller.root / "receiver-options.json", json.dumps(self.registry))

    def erase_state(self, key):
        if not re.fullmatch("[0-9a-f]{32}", key):
            raise ValueError("Invalid receiver registry ID")
        directory = self.controller.root / "tvs" / key
        if directory.exists():
            shutil.rmtree(directory)

    async def remove(self, key):
        if not re.fullmatch("[0-9a-f]{32}", key):
            raise ValueError("Use the receiver registry ID")
        async with self.enrol_lock:
            if key not in self.registry["removed"]:
                self.registry["removed"].append(key)
                self.save_registry()
            rt = self.runtimes.get(key)
            if rt:
                rt.retired = True
                await rt.idle.wait()
                await rt.close()
                if runner := self.runners.pop(rt, None):
                    port = runner.addresses[0][1]
                    self.stage_ports.discard(port)
                    await runner.cleanup()
                self.mirrors.pop(key, None)
                self.runtimes.pop(key)
                if rt is self.public_receiver:
                    self.public_receiver = None
            self.erase_state(key)
            if self.kept is not None:
                # Standalone mode: a TV that the list still names returns unpaired at the next start.
                self.kept.discard(key)
                self.registry["kept"] = sorted(self.kept)
                self.registry["removed"].remove(key)
                self.save_registry()
            elif key not in self.registry["configured"].values():
                self.registry["removed"].remove(key)
                self.save_registry()

    def _build(self, meta):
        key = meta["id"]
        directory = self.controller.root / "tvs" / key
        state = State(directory, controller=self.controller)
        rt = self.factory(
            meta.get("host") or "",
            state,
            apple_tv_id=meta.get("identifier") or "",
            **{
                k: self.options[k]
                for k in ("presentation_mode", "mirror_size", "mirror_fps", "mirror_fps_muted", "page_scripts",
                          "default_profile", "app_profile",
                          "watch_history", "history_interval", "history_fast_interval", "history_max_entries")
                if k in self.options
            },
        )
        rt.metadata = meta
        rt.stage.store = self.store
        rt.tools.admit = lambda replace: self.reserve(rt, replace)
        rt.tools.reconcile = lambda: self.reconcile(rt)
        self.runtimes[key] = rt
        write_private(directory / "receiver.json", json.dumps(meta))
        return rt

    def info(self, rt):
        target = rt.state.pairings.get("tv") or {}
        meta = rt.metadata
        return {
            "id": meta["id"],
            "name": meta.get("name") or target.get("name") or meta.get("host") or meta["id"],
            "host": rt.engine.adapter.resolver.address,
            "identifiers": list(
                dict.fromkeys(
                    ([meta["identifier"]] if meta.get("identifier") else [])
                    + target.get("identifiers", [])
                )
            ),
            "paired": bool(rt.state.receiver("airplay")),
            "screen_paired": bool(rt.state.receiver("remote_pairing")),
            "legacy": meta.get("legacy", False),
        }

    def inventory(self):
        return [self.info(rt) for rt in self.runtimes.values()]

    def register(self, entry, *, registry_id=None):
        host = str(ipaddress.IPv4Address(entry["host"])) if entry.get("host") else ""
        identifier = entry.get("id") or ""
        name = entry.get("name") or ""
        if not any((host, identifier, name)):
            raise ValueError("Supply an Apple TV name, host or identifier")
        for rt in self.runtimes.values():
            info = self.info(rt)
            ids = {x.casefold() for x in [info["id"], *info["identifiers"]]}
            if (
                (identifier and identifier.casefold() in ids)
                or (host and host in (info["host"], rt.metadata.get("host")))
                or (not host and not identifier and info["name"].casefold() == name.casefold())
            ):
                return self.update(rt, host, identifier, name)
        if rt := self.reattach(host, identifier, name):
            return self.update(rt, host, identifier, name)
        key = registry_id or record_id(identifier or host or name)
        if not registry_id and key in (self.kept or ()):
            # The derived ID belongs to a kept TV that did not match this entry. Never build onto it.
            key = uuid.uuid4().hex
        rt = self._build(
            {
                "id": key,
                "host": host,
                "identifier": identifier,
                "name": name,
            }
        )
        if name and not host and not identifier:
            rt.state.save_pairings(tv={"name": name})
        return rt

    def update(self, rt, host, identifier, name):
        info = self.info(rt)
        ids = {x.casefold() for x in [info["id"], *info["identifiers"]]}
        if identifier and info["identifiers"] and identifier.casefold() not in ids:
            raise ValueError("Address belongs to another registered receiver")
        resolver = rt.engine.adapter.resolver
        if host:
            rt.metadata["host"] = resolver.configured_host = host
        if identifier and identifier != info["id"]:
            rt.metadata["identifier"] = resolver.configured_id = identifier
        if name:
            rt.metadata["name"] = name
        write_private(rt.state.root / "receiver.json", json.dumps(rt.metadata))
        return rt

    def reattach(self, host, identifier, name):
        """Return to service the one kept record that matches by stored identifier, else by name,
        else by address. An identifier rules out records that store other identifiers. A name rules
        out records that store another name, unless the stored identifier matches."""
        records = {}
        for key in self.kept or ():
            directory = self.controller.root / "tvs" / key
            meta = json.loads((directory / "receiver.json").read_text())
            target = State(directory, controller=self.controller).pairings.get("tv") or {}
            ids = {x.casefold() for x in [meta.get("identifier"), *target.get("identifiers", [])] if x}
            known = bool(identifier) and identifier.casefold() in ids
            if identifier and ids and not known:
                continue
            stored_name = (meta.get("name") or target.get("name") or "").casefold()
            if name and stored_name and stored_name != name.casefold() and not known:
                continue
            records[key] = meta, known, stored_name, {meta.get("host"), target.get("last_known_host")}
        for found in (
            [key for key, (_, known, _, _) in records.items() if known],
            [key for key, (_, _, stored, _) in records.items() if name and stored == name.casefold()],
            [key for key, (_, _, _, hosts) in records.items() if host and host in hosts],
        ):
            if len(found) == 1:
                self.kept.discard(found[0])
                self.registry["kept"] = sorted(self.kept)
                self.save_registry()
                _LOG.info("Reattached the kept records of TV %s", found[0])
                return self._build(records[found[0]][0])
        return None

    async def candidates(self):
        return [describe(c) for c in await self.primary.engine.adapter.resolver.scan()]

    async def enrol(self, identifier):
        async with self.enrol_lock:
            for candidate in await self.candidates():
                identifiers = {value.casefold() for value in candidate["identifiers"]}
                if identifier.casefold() in identifiers:
                    # An explicit Add Apple TV may restore a still-configured receiver.
                    def matches_option(entry):
                        if entry.get("id"):
                            return entry["id"].casefold() in identifiers
                        if entry.get("host"):
                            return entry["host"] == candidate["address"]
                        return entry["name"].casefold() == candidate["name"].casefold()

                    aliases = [self.option_key(entry) for entry in self.entries if matches_option(entry)]
                    suppressed = {
                        self.registry["configured"][alias] for alias in aliases
                        if self.registry["configured"][alias] in self.registry["removed"]
                    }
                    rt = self.register(
                        {"id": identifier, "name": candidate["name"], "host": candidate["address"]},
                        registry_id=next(iter(sorted(suppressed)), None),
                    )
                    if suppressed:
                        for alias in aliases:
                            self.registry["configured"][alias] = rt.metadata["id"]
                        self.registry["removed"] = sorted(set(self.registry["removed"]) - suppressed)
                        self.save_registry()
                    if self.started and rt not in self.runners:
                        await self.start_runtime(rt)
                    return self.info(rt)
        raise Fault(
            "tv_not_found", "The selected Apple TV is no longer advertised.", "Discover TVs again."
        )

    def select(self, selector=None):
        if selector is not None:
            found = [
                rt
                for rt in self.runtimes.values()
                if selector.casefold()
                in {
                    x.casefold()
                    for x in (
                        self.info(rt)["id"],
                        self.info(rt)["name"],
                        *self.info(rt)["identifiers"],
                    )
                }
            ]
            if len(found) == 1:
                return found[0]
            if not found:
                raise Fault(
                    "tv_not_found",
                    "Unknown Apple TV.",
                    "Read tv://tvs and supply its ID.",
                    tvs=self.inventory(),
                )
        else:
            paired = [rt for rt in self.runtimes.values() if rt.state.receiver("airplay")]
            if len(paired) == 1:
                return paired[0]
            if len(self.runtimes) == 1:
                return next(iter(self.runtimes.values()))
        raise Fault(
            "tv_required",
            "Select an Apple TV.",
            "Supply tv with the name or ID from tv://tvs.",
            tvs=self.inventory(),
        )

    async def reserve(self, rt, replace):
        key = rt.metadata["id"]
        if not rt.state.receiver("airplay"):
            raise Fault(
                "not_paired", "AirPlay is unpaired.", "Pair this Apple TV in Home Assistant."
            )
        for holder in list(self.mirrors.values()):
            if not holder.tools.lock.locked():
                self.reconcile(holder)
        if key in self.mirrors:
            return
        if len(self.mirrors) >= self.limit:
            other = next(iter(self.mirrors.values()))
            if not replace:
                raise Fault(
                    "mirror_busy",
                    "Mirror capacity is in use.",
                    "Hide the current page or use replace: true.",
                    holders=[self.info(r) for r in self.mirrors.values()],
                )
            # Reserve the victim while its active/in-flight call finishes. Never
            # evict a different TV if a concurrent replacement wins the race.
            try:
                await asyncio.wait_for(other.tools.lock.acquire(), 10)
            except TimeoutError as exc:
                raise Fault(
                    "busy", "The other TV is busy.", "Retry after its call finishes.", True
                ) from exc
            try:
                if len(self.mirrors) >= self.limit and other in self.mirrors.values():
                    await other.operations.tv_hide()
                    self.mirrors.pop(other.metadata["id"], None)
            finally:
                other.tools.lock.release()
            if len(self.mirrors) >= self.limit:
                raise Fault(
                    "mirror_busy",
                    "Mirror capacity changed.",
                    "Check tv_status and retry.",
                    holders=[self.info(r) for r in self.mirrors.values()],
                )
        self.mirrors[key] = rt

    def reconcile(self, rt):
        sender = getattr(rt.operations, "presenter", rt.sender)
        if not rt.stage.page or getattr(sender, "active", sender).mode == "play_url":
            self.mirrors.pop(rt.metadata["id"], None)

    async def call(self, name, args):
        args = canonical(name, args)
        spec = ROUTED_SCHEMAS.get(name, schema({"tv": TEXT}) if name == "tv_hide" else None)
        if not spec or not Draft202012Validator(spec).is_valid(args):
            result = self.primary.tools.audit_failure(
                name,
                args,
                Fault("invalid_arguments", "Invalid tool arguments.", "Read tools/list."),
            )
            if "screen" in result:
                result["screen"] = UNKNOWN | {"tv": None}
            return result
        args = args.copy()
        selector = args.pop("tv", None)
        if name == "tv_history":
            # History reads stored entries. It needs no default TV, no tool lock and no connection.
            try:
                chosen = [self.select(selector)] if selector else list(self.runtimes.values())
                sources = [
                    ({"tv": self.info(rt)["name"], "tv_id": rt.metadata["id"]}, rt.history) for rt in chosen
                ]
                result = await report(sources, **args)
            except Fault as fault:
                return self.primary.tools.audit_failure(name, args, fault)
            self.primary.state.audit(name, args, result)
            return result
        try:
            rt = self.select(selector)
        except Fault as fault:
            if (
                fault.code == "tv_required"
                and selector is None
                and name in ("tv_status", "tv_look")
            ):

                async def summary(runtime):
                    result = await self.call(name, {"tv": runtime.metadata["id"]})
                    row = self.info(runtime) | {"ok": result["ok"]}
                    if name == "tv_look":
                        row["screen"] = result.get("screen")
                    else:
                        row["status"] = result["data"]
                    if "error" in result:
                        row["error"] = result["error"]
                    return row

                rows = await asyncio.gather(*(summary(r) for r in self.runtimes.values()))
                return outcome("Apple TV summaries.", data={"tvs": rows}) | (
                    {"screen": None} if name == "tv_look" else {}
                )
            result = self.primary.tools.audit_failure(name, args, fault)
            if "screen" in result:
                result["screen"] = UNKNOWN | {"tv": None}
            return result
        replace = args.pop("replace", False)
        try:
            with rt.use():
                result = await rt.tools.call(name, args, replace=replace)
        except Fault as fault:
            result = self.primary.tools.audit_failure(name, args, fault)
        if "screen" in result:
            result["screen"].update(tv=self.info(rt)["name"], tv_id=rt.metadata["id"])
        if name == "tv_status":
            result["data"].setdefault("tv", {}).update(
                id=rt.metadata["id"], name=self.info(rt)["name"]
            )
        return result

    async def apps(self, tv=None):
        rt = self.select(tv)
        with rt.use():
            return await rt.tools.apps()

    async def start_runtime(self, rt, port=0):
        primary = rt is self.primary

        if not primary and self.public_receiver is None:
            self.public_receiver = rt

        def media_stage(key):
            receiver = (self.public_receiver or self.primary) if key is None else self.runtimes.get(key)
            return receiver.stage if receiver and not receiver.retired else None

        runner = None
        if rt is self.public_receiver:
            rt.stage.origin = self.primary.stage.origin
        else:
            app = rt.stage.app(media_stage if primary else None)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            await runner.setup()
            try:
                await web.TCPSite(runner, self.stage_host if primary else "127.0.0.1", port).start()
            except BaseException:
                await runner.cleanup()
                raise
            port = runner.addresses[0][1]
            rt.stage.origin = f"http://127.0.0.1:{port}"
            self.stage_ports.add(port)
        rt.stage.stage_ports = self.stage_ports
        rt.stage.page_network = self.page_network
        if rt in self.runtimes.values():
            rt.stage.media_origin = self.primary.stage.origin
            rt.stage.media_prefix = rt.metadata["id"]
        rt.stage.key = self.stage_key
        rt.operations.live_mirror.stage_url = rt.stage.url
        self.runners[rt] = runner
        if rt in self.runtimes.values():
            rt.engine.start()
            rt.history.start()

    async def start(self):
        try:
            await self.start_runtime(self.primary, self.stage_port)
            for rt in self.runtimes.values():
                await self.start_runtime(rt)
            self.started = True
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self.started = False
        self.stage_ports.clear()

        async def stop(rt, runner):
            try:
                await rt.close()
            finally:
                if runner:
                    await runner.cleanup()

        results = await asyncio.gather(
            *(stop(rt, runner) for rt, runner in self.runners.items()), return_exceptions=True
        )
        self.runners.clear()
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def app(self, authorize=None):
        return self.primary.app(self, authorize)
