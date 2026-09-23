"""Qwen Token Plan's documented Messages contract, separate from Coding/PAYG."""
from __future__ import annotations

import copy

PLAN_DOCS = "https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-personal-overview"
TEAM_DOCS = "https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-team-overview"
API_DOCS = "https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages"
FLASH_DOCS = "https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-8-flash"
BASE_URL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"
DESCRIPTOR = {
    "id": "qwen-token-plan", "name": "Qwen Token Plan", "protocol": "anthropic",
    "default_base_url": BASE_URL, "default_region": "singapore",
    "regions": {"singapore": BASE_URL},
    "auth_header": {"name": "x-api-key", "prefix": ""},
    "credential_account": "QWEN_TOKEN_PLAN_API_KEY", "credential_env": "QWEN_TOKEN_PLAN_API_KEY",
    "setup_url": "https://modelstudio.console.alibabacloud.com/",
    "reasoning_store_cap": 16384,
    "capabilities": {"streaming": True, "tools": True, "thinking": True,
                     "vision": "model_dependent", "model_discovery": "documentation", "reasoning_history": "native"},
}
OFFICIAL_PATHS = {"", "/apps/anthropic", "/apps/anthropic/v1/messages",
                  "/compatible-mode/v1", "/compatible-mode/v1/chat/completions"}

# Current native Qwen text models in the plan documentation, checked 2026-09-13.
# No preview aliases, media-generation models, or PAYG-only model snapshots.
# Exact limits come from the matching Token Plan catalogue and Alibaba's
# first-party model card for Qwen 3.8 Flash, which is in the Token Plan roster.
MODEL_ROWS = (
    ("qwen3.8-max", "Qwen 3.8 Max", True, ["none", "low", "medium", "xhigh"]),
    ("qwen3.8-flash", "Qwen 3.8 Flash", True, ["none", "low", "medium", "xhigh"]),
    ("qwen3.7-max", "Qwen 3.7 Max", False, ["none", "high"]),
    ("qwen3.7-plus", "Qwen 3.7 Plus", True, ["none", "high"]),
    ("qwen3.6-plus", "Qwen 3.6 Plus", True, ["none", "high"]),
    ("qwen3.6-flash", "Qwen 3.6 Flash", True, ["none", "high"]),
)
#: Alibaba publishes a COMBINED input+output window for these routes, so the
#: first figure is not an input budget: qwen3.8-max is 1,000,000 combined with
#: 991,808 available to input. The combined number stays the model's context
#: because that is what it is, and the input ceiling is recorded beside it for
#: anything that needs to state what the model can actually be given.
CATALOGUE_INPUT_LIMITS = {
    "qwen3.8-max": 991808,
    "qwen3.7-max": 991808,
}
CATALOGUE_LIMITS = {
    "qwen3.8-max": (1000000, 131072),
    "qwen3.8-flash": (1000000, 131072),
    "qwen3.7-max": (1000000, 131072),
    "qwen3.7-plus": (1000000, 65536),
    "qwen3.6-plus": (1000000, 65536),
    "qwen3.6-flash": (1000000, 65536),
}
LIMIT_EVIDENCE = {
    "source": "TaskWraith PiModels.ts and @earendil-works/pi-ai 0.84.2",
    "file": "dist/providers/data/qwen-token-plan.json",
    "sha256": "3fa3afccf5577c73abeb2e4eccd19f07ad4c59c34299b6d6fc284b77da24cd21",
    "endpoint": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    "kind": "catalogue_snapshot; not a new inference test",
}


class QwenError(ValueError):
    pass


def catalogue():
    models = [{
        "id": identifier, "canonical_id": identifier, "display_name": name,
        "aliases": [identifier], "context": CATALOGUE_LIMITS.get(identifier, (None, None))[0],
        "max_output": CATALOGUE_LIMITS.get(identifier, (None, None))[1],
        "max_input": CATALOGUE_INPUT_LIMITS.get(identifier),
        "context_kind": "verified_documentation" if identifier == "qwen3.8-flash" else "catalogue_snapshot",
        "limit_evidence": ({"source": FLASH_DOCS, "roster": PLAN_DOCS, "checked_at": "2026-09-19",
                            "kind": "published_model_limits; not an account capacity test"}
                           if identifier == "qwen3.8-flash" else copy.deepcopy(LIMIT_EVIDENCE)),
        "tools": True, "vision": vision,
        "reasoning": True, "streaming": True, "effort_modes": list(efforts),
        "default_effort": "xhigh" if "xhigh" in efforts else "high",
        "fast_mode": False, "reasoning_history": "native", "complete_tool_cycles": True,
        "source": "provider_documentation", "evidence": TEAM_DOCS if identifier == "qwen3.6-plus" else PLAN_DOCS,
        "metadata_evidence": API_DOCS, "metadata_checked_at": "2026-09-13",
        "plan_availability": "team" if identifier == "qwen3.6-plus" else "personal_and_team",
        "inference_status": "advertised",
    } for identifier, name, vision, efforts in MODEL_ROWS]
    return models, [
        "Documented Qwen Token Plan roster; account access has not been inference-tested.",
        "Qwen 3.6 Plus is documented for Team Edition; model access depends on the plan attached to your key.",
        "Exact limits are imported from TaskWraith/Pi's matching Token Plan catalogue (Pi 0.84.2), not a new inference measurement.",
        "Qwen 3.8 Flash's 1000000-token context and 131072-token output ceiling come from Alibaba's model card; account access is not inferred.",
    ], PLAN_DOCS


def normalize_controls(body, model_id, spec):
    """Mutate the already-copied native payload, preserving opaque thinking."""
    compatibility = {}
    if (body.get("speed") not in {None, "standard", "normal"}
            or body.get("service_tier") not in {None, "auto", "default", "standard"}):
        raise QwenError("Qwen Token Plan does not advertise a same-model Fast or alternate processing tier.")
    thinking = body.get("thinking")
    if thinking is not None and (not isinstance(thinking, dict) or thinking.get("type") not in {"enabled", "disabled"}):
        raise QwenError("Qwen supports enabled or disabled thinking.")
    output = body.get("output_config")
    if output is not None and not isinstance(output, dict):
        raise QwenError("output_config must be an object.")
    effort = (output or {}).get("effort")
    if effort is not None:
        aliases = {"none": "none", "minimal": "low", "low": "low", "medium": "medium",
                   "high": "xhigh", "xhigh": "xhigh", "max": "xhigh", "ultra": "xhigh"}
        if effort not in aliases:
            raise QwenError("Qwen received an unsupported reasoning effort.")
        target = "disabled" if effort == "none" else "enabled"
        if thinking and thinking["type"] != target:
            raise QwenError("Qwen received conflicting thinking and effort controls.")
        body["thinking"] = {"type": target}
        if thinking and "budget_tokens" in thinking:
            compatibility["thinking_budget"] = "replaced_by_effort"
        if target == "disabled":
            output.pop("effort", None)
        elif "xhigh" in (spec.get("effort_modes") or []):
            output["effort"] = aliases[effort]
            if output["effort"] != effort:
                compatibility["reasoning_effort"] = f"{effort}_normalized_to_{output['effort']}"
        else:
            # Earlier Qwen plan models advertise a thinking switch, not the
            # 3.8 effort ladder. Never invent named server-side levels.
            output.pop("effort", None)
            compatibility["reasoning_effort"] = "normalized_to_enabled"
        if not output:
            body.pop("output_config", None)
    elif thinking:
        body["thinking"] = {key: copy.deepcopy(value) for key, value in thinking.items()
                            if key in {"type", "budget_tokens"}}
    for field in ("speed", "service_tier"):
        body.pop(field, None)  # Non-default tiers are rejected by the shared planner.
    for message in body.get("messages", []):
        if not isinstance(message.get("content"), list):
            continue
        expanded = []
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                expanded.append(block)
                continue
            content = block.get("content")
            images = []
            if isinstance(content, list):
                text = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        text.append(part["text"])
                    elif isinstance(part, dict) and part.get("type") == "image":
                        images.append(part)
                    else:
                        raise QwenError("Qwen tool results require text; only image blocks can be moved to the user message.")
                if images:
                    text.append(f"[Provider Hub: {len(images)} image{'s' if len(images) != 1 else ''} "
                                "from this tool result follow immediately below.]")
                block["content"] = "\n".join(text)
            expanded.append(block)
            # Alibaba requires tool_result.content to be a string. Image blocks
            # are valid in the enclosing user message, including on replay.
            expanded.extend(images)
        message["content"] = expanded
    return compatibility
