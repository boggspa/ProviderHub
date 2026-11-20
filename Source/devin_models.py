"""Tolerant extraction of model rows from ``devin models list --format json``.

Shared by ``devin_cli_agent`` (the route's live catalogue) and
``cli_auth_probe`` (the settings pane's read-only probe) so the two can never
disagree about what the CLI's listing means. Stdlib-only by design: the probe
module's import contract allows nothing heavier.

The exact schema is the CLI's own and has moved between releases, so the
walker classifies each dict by the keys it carries rather than assuming a
fixed shape: family rows (``slug`` and friends) are preferred over variant
rows (``model_uid`` and friends) because 200+ variants would swamp a picker
where ~27 families do not, and a bare family slug is itself a valid
``devin acp --model`` argument.
"""
from __future__ import annotations

from typing import Any

#: Family-level keys in `devin models list --format json` output; variant-level
#: keys identify rows that belong to a family rather than being one.
_FAMILY_ID_KEYS = ("slug", "family_slug", "familySlug", "family")
_FAMILY_LABEL_KEYS = ("family_label", "familyLabel", "label", "name", "display_name")
_MODEL_ID_KEYS = ("model_uid", "uid", "model", "id", "model_id")
_MODEL_LABEL_KEYS = ("label", "name", "display_name", "title")


def _first_str(entry: dict, keys) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _looks_like_family(entry: dict) -> bool:
    return _first_str(entry, _FAMILY_ID_KEYS) is not None


def catalogue_rows(payload: Any) -> list[dict]:
    """Pull family (preferred) or model rows out of the models-list JSON.

    Walks whatever container lists the payload holds — a bare list, a
    ``{"families": [...]}`` envelope, or deeper nesting — and dedupes by id,
    preserving first-seen order.
    """
    families: list[dict] = []
    models: list[dict] = []
    seen: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            is_family = _looks_like_family(node)
            identifier = _first_str(node, _FAMILY_ID_KEYS if is_family else _MODEL_ID_KEYS)
            label = _first_str(node, _FAMILY_LABEL_KEYS if is_family else _MODEL_LABEL_KEYS) or identifier
            if identifier and identifier not in seen:
                seen.add(identifier)
                (families if is_family else models).append(
                    {"id": identifier, "display_name": label})
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return families or models
