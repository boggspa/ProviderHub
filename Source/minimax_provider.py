"""MiniMax's native Messages route and current Token Plan model catalogue."""
from __future__ import annotations

API_DOCS = "https://platform.minimax.io/docs/api-reference/text-anthropic-api"
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
MODEL_ROWS = (
    ("MiniMax-M3", "MiniMax M3", 1000000, 128000, True),
    ("MiniMax-M2.7", "MiniMax M2.7", 204800, 131072, False),
    ("MiniMax-M2.7-highspeed", "MiniMax M2.7 Highspeed", 204800, 131072, False),
)


class MiniMaxError(ValueError):
    pass


def catalogue():
    models = [{
        "id": identifier, "canonical_id": identifier, "display_name": name,
        "aliases": [identifier], "context": context, "max_output": maximum,
        "context_kind": "catalogue_snapshot", "tools": True, "vision": vision,
        "reasoning": True, "streaming": True,
        "effort_modes": ["none", "high"] if vision else ["high"],
        "default_effort": "high", "fast_mode": False,
        "reasoning_history": "native", "complete_tool_cycles": True,
        "source": "provider_documentation", "evidence": API_DOCS,
        "metadata_checked_at": "2026-09-25", "inference_status": "advertised",
        "limit_evidence": {"source": "TaskWraith Pi model catalogue",
                           "kind": "catalogue_snapshot; not an account capacity test"},
    } for identifier, name, context, maximum, vision in MODEL_ROWS]
    return models, [
        "Current MiniMax models; account access has not been inference-tested.",
        "Use a Subscription Key (sk-cp) for Token Plan usage or an API key for pay-as-you-go billing.",
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
    # MiniMax publishes a switch, not token budgets or an effort ladder.
    if model_id.startswith("MiniMax-M2"):
        body.pop("thinking", None)
        if requested is not None or thinking is not None:
            compatibility["reasoning_effort"] = "fixed_thinking_always_on"
    elif requested is not None or thinking is not None:
        disabled = requested == "none" or kind == "disabled"
        body["thinking"] = {"type": "disabled" if disabled else "adaptive"}
        compatibility["reasoning_effort"] = "none" if disabled else "normalized_to_adaptive"
    if output is not None:
        output.pop("effort", None)
        if not output:
            body.pop("output_config", None)
    return compatibility
