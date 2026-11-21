"""Apple TV tools for the built-in Assist and Home Assistant APIs."""

import probatio
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm

from . import receivers
from .api import BridgeError
from .const import DOMAIN


def _loaded_entry(hass, context, api_id):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    entry = entries[0]
    if api_id == llm.LLM_API_ASSIST and not _exposed(hass, entry, context):
        return None
    return entry


def _exposed(hass, entry, context):
    if not context.assistant:
        return []
    registry = er.async_get(hass)
    return [
        c
        for c in receivers(entry)
        if (
            entity := registry.async_get_entity_id(
                "media_player", DOMAIN, f"{c.entity_prefix}_player"
            )
        )
        and async_should_expose(hass, context.assistant, entity)
    ]


class BridgeTool(llm.Tool):
    integration = DOMAIN

    def __init__(
        self, api_id: str, suffix: str, tool: str, description: str, fields: dict
    ) -> None:
        self.api_id = api_id
        self.name = f"{DOMAIN}__{suffix}"
        self.tool = tool
        self.description = description
        self.parameters = probatio.Schema(fields | {probatio.Optional("tv"):str})
        self.annotations = llm.ToolAnnotations(
            read_only=suffix == "status",
            destructive=False,
            idempotent=suffix in ("status", "power"),
            open_world=suffix in ("open", "play", "show"),
        )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        entry = _loaded_entry(hass, llm_context, self.api_id)
        if entry is None:
            raise BridgeError("Apple TV is unavailable to this assistant")
        args = self.parameters(tool_input.tool_args)
        if self.api_id == llm.LLM_API_ASSIST and any(c.tv for c in receivers(entry)):
            allowed = _exposed(hass, entry, llm_context)
            if args.get("replace") and len(allowed) != len(receivers(entry)):
                raise BridgeError(
                    "Replacing another room requires access to every receiver"
                )
            selector = args.get("tv")
            if selector:
                matches = [
                    c
                    for c in allowed
                    if selector.casefold()
                    in {
                        v.casefold()
                        for v in (
                            c.tv["id"],
                            c.tv["name"],
                            *c.tv.get("identifiers", []),
                        )
                    }
                ]
                if len(matches) != 1:
                    raise BridgeError("Select a TV exposed to this assistant")
                args["tv"] = matches[0].tv["id"]
            elif len(allowed) == 1:
                args["tv"] = allowed[0].tv["id"]
            elif len(allowed) != len(receivers(entry)):
                if self.tool != "tv_status":
                    raise BridgeError("Select a TV exposed to this assistant")
                results = [
                    await c.client.request("/api/tool/tv_status", {}, check=False)
                    for c in allowed
                ]
                return llm.ToolResult(data={"tvs":[r["data"] for r in results]}, error=False)
        # Core's MCP server serializes ToolResult.data as one JSON text content item.
        result = await entry.runtime_data.client.request(
            f"/api/tool/{self.tool}", args, check=False
        )
        return llm.ToolResult(data=result, error=not result["ok"])


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: llm.LLMContext, api_id: str
) -> LLMTools | None:
    if api_id not in (
        llm.LLM_API_ASSIST,
        llm.LLM_API_HOME_ASSISTANT,
    ) or not _loaded_entry(hass, llm_context, api_id):
        return None
    optional = probatio.Optional
    required = probatio.Required
    return LLMTools(
        tools=[
            BridgeTool(
                api_id,
                "show",
                "tv_show",
                "Present a live URL, HTML, files, or bundle. Use {} to hide.",
                {
                    optional("url"): str,
                    optional("html"): str,
                    optional("files"): {str: str},
                    optional("bundle"): str,
                    optional("title"): str,
                    optional("audio"): str,
                    optional("replace"): bool,
                },
            ),
            BridgeTool(
                api_id,
                "play",
                "tv_play",
                "Play YouTube, media, a link or a title in an app.",
                {
                    optional(k): str
                    for k in ("youtube", "media", "link", "app", "title")
                },
            ),
            BridgeTool(
                api_id,
                "open",
                "tv_open",
                "Open an installed app, optionally searching or following a menu path.",
                {
                    required("app"): str,
                    optional("search"): str,
                    optional("path"): [str],
                },
            ),
            BridgeTool(
                api_id,
                "power",
                "tv_power",
                "Turn the TV on or off.",
                {required("on"): bool},
            ),
            BridgeTool(api_id, "pair", "tv_pair", "Start a TV pairing, relay ask_user and submit the code as text.", {
                required("kind"): str, optional("code"): str, optional("pairing_id"): str,
                optional("notify"): bool, optional("cancel"): bool, optional("read_code_from_screen"): bool,
            }),
            BridgeTool(
                api_id,
                "status",
                "tv_status",
                "Check TV reachability, pairing and capabilities.",
                {},
            ),
        ]
    )
