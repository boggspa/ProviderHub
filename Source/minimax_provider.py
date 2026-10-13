"""MiniMax's native Messages route and current Token Plan model catalogue."""
from __future__ import annotations

from effort_map import nearest_effort

API_DOCS = "https://platform.minimax.io/docs/api-reference/text-anthropic-api"
MESSAGES_DOCS = "https://platform.minimax.io/docs/api-reference/text-chat-anthropic"
PLAN_DOCS = "https://platform.minimax.io/docs/token-plan/faq"
BASE_URL = "https://api.minimax.io/anthropic"
DESCRIPTOR = {
    "id": "minimax", "name": "MiniMax", "protocol": "anthropic",
    "default_base_url": BASE_URL, "default_region": "global",
    "regions": {"global": BASE_URL},
    "auth_header": {"name": "x-api-key", "prefix": ""},
    "credential_account": "MINIMAX_API_KEY", "credential_env": "MINIMAX_API_KEY",
    "setup_url": "https://platform.minimax.io/console/plan",
    "reasoning_store_cap": 16384,
    "capabilities": {"streaming": True, "tools": True, "thinking": True,
                     "vision": "model_dependent", "model_discovery": "documentation",
                     "reasoning_history": "native"},
}
OFFICIAL_PATHS = {"", "/anthropic", "/anthropic/v1/messages"}
#: (id, name, context, max output, vision[, checked, limit source]). Discovery
#: reads this documented list, not the account, so a new model appears only
#: once it is added here.
MODEL_ROWS = (
    ("MiniMax-M3", "MiniMax M3", 1000000, 128000, True),
    # Token Plan (Subscription Key) only while in preview. max_output clamps
    # max_tokens, so it carries the documented 512K ceiling, not the 128K
    # MiniMax recommends.
    ("MiniMax-M3.1-Flash-Preview", "MiniMax M3.1 Flash Preview", 1000000, 524288, True,
     "2026-10-13", MESSAGES_DOCS),
    ("MiniMax-M2.7", "MiniMax M2.7", 204800, 131072, False),
    ("MiniMax-M2.7-highspeed", "MiniMax M2.7 Highspeed", 204800, 131072, False),
)
#: Models that always think and take their depth as output_config.effort;
#: MiniMax refuses a disabled thinking switch on them with HTTP 400, and uses
#: max when effort is omitted, so that is their default here too.
EFFORT_LADDERS = {"MiniMax-M3.1-Flash-Preview": ("low", "medium", "high", "xhigh", "max")}


class MiniMaxError(ValueError):
    pass


def _model(identifier, name, context, maximum, vision, checked="2026-09-25",
           limits="TaskWraith Pi model catalogue"):
    return {
        "id": identifier, "canonical_id": identifier, "display_name": name,
        "aliases": [identifier], "context": context, "max_output": maximum,
        "context_kind": "catalogue_snapshot", "tools": True, "vision": vision,
        "reasoning": True, "streaming": True,
        "effort_modes": list(EFFORT_LADDERS.get(identifier) or (["none", "high"] if vision else ["high"])),
        "default_effort": "max" if identifier in EFFORT_LADDERS else "high", "fast_mode": False,
        "reasoning_history": "native", "complete_tool_cycles": True,
        "source": "provider_documentation", "evidence": API_DOCS,
        "metadata_checked_at": checked, "inference_status": "advertised",
        "limit_evidence": {"source": limits,
                           "kind": "catalogue_snapshot; not an account capacity test"},
    }


def catalogue():
    models = [_model(*row) for row in MODEL_ROWS]
    return models, [
        "Current MiniMax models; account access has not been inference-tested.",
        "Use a Subscription Key (sk-cp) for Token Plan usage or an API key for pay-as-you-go billing.",
        "M3.1 Flash Preview is Token Plan only for now; it always thinks, and effort sets how deeply.",
        "M2.7 Highspeed is a separate model ID; M2.x thinking is always on.",
    ], API_DOCS


def normalize_controls(body, model_id, spec):
    """Translate client effort controls without changing opaque reasoning history."""
    compatibility = {}
    speed = body.pop("speed", None)
    if speed not in (None, "standard", "normal"):
        raise MiniMaxError("Choose MiniMax-M2.7-highspeed for Highspeed output.")
    tier = body.get("service_tier")
    if tier in ("auto", "default"):
        body.pop("service_tier")
    elif tier not in (None, "standard", "priority"):
        raise MiniMaxError("MiniMax supports standard or priority admission tiers.")
    thinking = body.get("thinking")
    if thinking is not None and (
        not isinstance(thinking, dict)
        or thinking.get("type") not in ("enabled", "adaptive", "disabled")
    ):
        raise MiniMaxError("MiniMax thinking must be adaptive, enabled or disabled.")
    output = body.get("output_config")
    if output is not None and not isinstance(output, dict):
        raise MiniMaxError("output_config must be an object.")
    requested = (output or {}).get("effort")
    if requested not in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        raise MiniMaxError("Unsupported MiniMax reasoning effort.")
    kind = (thinking or {}).get("type")
    if requested is not None and kind is not None and (
        (requested == "none") != (kind == "disabled")
    ):
        raise MiniMaxError("MiniMax received conflicting thinking and effort controls.")
    # M3 publishes a switch, not token budgets or an effort ladder; M3.1
    # Flash always thinks and takes its depth from a five-rung ladder.
    level = None
    if model_id.startswith("MiniMax-M2"):
        body.pop("thinking", None)
        if requested is not None or thinking is not None:
            compatibility["reasoning_effort"] = "fixed_thinking_always_on"
    elif model_id in EFFORT_LADDERS:
        if thinking is not None:
            body["thinking"] = {"type": "adaptive"}
        if requested == "none" or kind == "disabled":
            compatibility["reasoning_effort"] = "fixed_thinking_always_on"
        elif requested is not None:
            level = nearest_effort(requested, EFFORT_LADDERS[model_id])
            if level != requested:
                compatibility["reasoning_effort"] = f"{requested}_normalized_to_{level}"
    elif requested is not None or thinking is not None:
        disabled = requested == "none" or kind == "disabled"
        body["thinking"] = {"type": "disabled" if disabled else "adaptive"}
        compatibility["reasoning_effort"] = "none" if disabled else "normalized_to_adaptive"
    if output is not None:
        output.pop("effort", None)
        if not output:
            body.pop("output_config", None)
    if level is not None:
        body.setdefault("output_config", {})["effort"] = level
    return compatibility
