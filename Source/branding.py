"""Portable provider presentation and user-branding overrides.

The field names intentionally mirror TaskWraith's camelCase presentation
contract.  Every returned value is display-only: callers must continue to use
their separately selected provider record for routing, credentials, and bills.
"""
from __future__ import annotations

from collections.abc import Mapping
import copy
import json
import math
from pathlib import Path, PurePosixPath
import re


BRANDING_PATH = Path(__file__).with_name("provider_branding.json")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}\Z")
SHORT_CODE = re.compile(r"[A-Z0-9]{2,4}\Z")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,199}\Z")
ASSET_PART = re.compile(r"[A-Za-z0-9_.@+-]+\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")

CATALOG_KEYS = {
    "schemaVersion", "fieldConvention", "sourceContract", "fallbackHueKey",
    "accents", "accentAliases", "providers", "modelLabels", "modelBrandOverrides",
}
PROVIDER_KEYS = {"displayProvider", "hueKey", "shortCode", "logo"}
USER_OVERRIDE_KEYS = {
    "displayProvider", "hueKey", "accent", "shortCode", "modelLabels", "logo",
}
LOGO_KEYS = {"light", "dark", "scale", "leadingMarkAspectRatio", "trailingMarkAspectRatio", "template"}
RULE_KEYS = {
    "runtimeProvider", "id", "providerLabel", "providerClass", "needles",
    "fallbackModelLabel",
}


class BrandingError(ValueError):
    """A branding catalogue or override failed closed validation."""


def _object(value, label: str) -> dict:
    if not isinstance(value, Mapping):
        raise BrandingError(f"{label} must be an object.")
    return dict(value)


def _known_keys(value: Mapping, allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise BrandingError(f"{label} has unsupported field: {sorted(unknown)[0]}.")


def _slug(value, label: str) -> str:
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise BrandingError(f"{label} must be a lowercase provider slug.")
    return value


def _text(value, label: str, maximum: int = 80) -> str:
    if not isinstance(value, str):
        raise BrandingError(f"{label} must be text.")
    result = value.strip()
    if not result or len(result) > maximum or CONTROL.search(result):
        raise BrandingError(f"{label} must be non-empty printable text up to {maximum} characters.")
    return result


def _model_id(value, label: str) -> str:
    if not isinstance(value, str) or not MODEL_ID.fullmatch(value.strip()):
        raise BrandingError(f"{label} must be a safe model identifier.")
    return value.strip()


def _colour(value, label: str) -> str:
    if not isinstance(value, str) or not HEX_COLOR.fullmatch(value):
        raise BrandingError(f"{label} must be a #RRGGBB colour.")
    return value.upper()


def _short_code(value, label: str) -> str:
    if not isinstance(value, str) or not SHORT_CODE.fullmatch(value):
        raise BrandingError(f"{label} must contain 2-4 uppercase letters or digits.")
    return value


def _asset(value, label: str) -> str:
    path = _text(value, label, 200)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise BrandingError(f"{label} must be a relative bundled asset path.")
    if any(part in {"", "."} or not ASSET_PART.fullmatch(part) for part in pure.parts):
        raise BrandingError(f"{label} contains an unsafe asset path component.")
    return path


def _logo(value, label: str) -> dict:
    logo = _object(value, label)
    _known_keys(logo, LOGO_KEYS, label)
    if "light" not in logo:
        raise BrandingError(f"{label}.light is required.")
    result = {"light": _asset(logo["light"], f"{label}.light")}
    if "dark" in logo:
        result["dark"] = _asset(logo["dark"], f"{label}.dark")
    if "scale" in logo:
        scale = logo["scale"]
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            raise BrandingError(f"{label}.scale must be a finite number from 0.5 to 2.0.")
        scale = float(scale)
        if not math.isfinite(scale) or not 0.5 <= scale <= 2.0:
            raise BrandingError(f"{label}.scale must be a finite number from 0.5 to 2.0.")
        result["scale"] = scale
    for key in ("leadingMarkAspectRatio", "trailingMarkAspectRatio"):
        if key not in logo:
            continue
        ratio = logo[key]
        if (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                or not math.isfinite(ratio) or not 0.5 <= ratio <= 2.0):
            raise BrandingError(
                f"{label}.{key} must be a finite number from 0.5 to 2.0.")
        result[key] = float(ratio)
    if "leadingMarkAspectRatio" in result and "trailingMarkAspectRatio" in result:
        raise BrandingError(
            f"{label} may crop a leading or a trailing mark, not both.")
    if "template" in logo:
        if not isinstance(logo["template"], bool):
            raise BrandingError(f"{label}.template must be a boolean.")
        result["template"] = logo["template"]
    return result


def _model_labels(value, label: str) -> dict:
    labels = _object(value, label)
    result = {}
    for raw_model, raw_label in labels.items():
        model = _model_id(raw_model, f"{label} key")
        result[model] = _text(raw_label, f"{label}.{model}", 120)
    return result


def _resolved_hue_key(catalogue: Mapping, hue_key: str) -> str | None:
    accents = catalogue["accents"]
    aliases = catalogue["accentAliases"]
    seen = set()
    current = hue_key
    while current in aliases:
        if current in seen:
            return None
        seen.add(current)
        current = aliases[current]
    return current if current in accents else None


def _validate_catalogue(value) -> dict:
    catalogue = _object(value, "Branding catalogue")
    _known_keys(catalogue, CATALOG_KEYS, "Branding catalogue")
    missing = CATALOG_KEYS - set(catalogue)
    if missing:
        raise BrandingError(f"Branding catalogue is missing {sorted(missing)[0]}.")
    if catalogue["schemaVersion"] != 1:
        raise BrandingError("Unsupported branding schemaVersion.")
    if catalogue["fieldConvention"] != "TaskWraith camelCase":
        raise BrandingError("Branding fieldConvention must be TaskWraith camelCase.")

    source = _object(catalogue["sourceContract"], "sourceContract")
    required_source = {"presentation", "modelBrandOverride", "logo", "adaptation"}
    _known_keys(source, required_source, "sourceContract")
    if set(source) != required_source:
        raise BrandingError("sourceContract is incomplete.")
    source = {key: _text(source[key], f"sourceContract.{key}", 160) for key in required_source}

    accents_raw = _object(catalogue["accents"], "accents")
    if not accents_raw:
        raise BrandingError("accents must not be empty.")
    accents = {_slug(key, "Accent key"): _colour(colour, f"accents.{key}")
               for key, colour in accents_raw.items()}

    aliases_raw = _object(catalogue["accentAliases"], "accentAliases")
    aliases = {_slug(key, "Accent alias"): _slug(target, f"accentAliases.{key}")
               for key, target in aliases_raw.items()}
    if set(aliases) & set(accents):
        raise BrandingError("An accent alias must not replace a concrete accent.")

    interim = {"accents": accents, "accentAliases": aliases}
    for key in aliases:
        if _resolved_hue_key(interim, key) is None:
            raise BrandingError(f"accentAliases.{key} is cyclic or has no concrete accent.")

    fallback = _slug(catalogue["fallbackHueKey"], "fallbackHueKey")
    if _resolved_hue_key(interim, fallback) is None:
        raise BrandingError("fallbackHueKey has no concrete accent.")

    providers_raw = _object(catalogue["providers"], "providers")
    providers = {}
    for raw_id, raw_provider in providers_raw.items():
        provider_id = _slug(raw_id, "Provider id")
        provider = _object(raw_provider, f"providers.{provider_id}")
        _known_keys(provider, PROVIDER_KEYS, f"providers.{provider_id}")
        required = {"displayProvider", "hueKey", "shortCode"}
        if not required <= set(provider):
            raise BrandingError(f"providers.{provider_id} is missing a required presentation field.")
        hue = _slug(provider["hueKey"], f"providers.{provider_id}.hueKey")
        if _resolved_hue_key(interim, hue) is None:
            raise BrandingError(f"providers.{provider_id}.hueKey has no concrete accent.")
        normalized = {
            "displayProvider": _text(provider["displayProvider"], f"providers.{provider_id}.displayProvider"),
            "hueKey": hue,
            "shortCode": _short_code(provider["shortCode"], f"providers.{provider_id}.shortCode"),
        }
        if "logo" in provider:
            normalized["logo"] = _logo(provider["logo"], f"providers.{provider_id}.logo")
        providers[provider_id] = normalized

    model_labels = _model_labels(catalogue["modelLabels"], "modelLabels")
    rules_raw = catalogue["modelBrandOverrides"]
    if not isinstance(rules_raw, list):
        raise BrandingError("modelBrandOverrides must be an array.")
    rules = []
    rule_ids = set()
    for index, raw_rule in enumerate(rules_raw):
        label = f"modelBrandOverrides[{index}]"
        rule = _object(raw_rule, label)
        _known_keys(rule, RULE_KEYS, label)
        if set(rule) != RULE_KEYS:
            raise BrandingError(f"{label} is missing a required field.")
        runtime = _slug(rule["runtimeProvider"], f"{label}.runtimeProvider")
        rule_id = _slug(rule["id"], f"{label}.id")
        unique_id = (runtime, rule_id)
        if unique_id in rule_ids:
            raise BrandingError(f"{label} duplicates a runtime/id pair.")
        rule_ids.add(unique_id)
        provider_class = _slug(rule["providerClass"], f"{label}.providerClass")
        if _resolved_hue_key(interim, provider_class) is None:
            raise BrandingError(f"{label}.providerClass has no concrete accent.")
        needles = rule["needles"]
        if not isinstance(needles, list) or not needles:
            raise BrandingError(f"{label}.needles must be a non-empty array.")
        normalized_needles = []
        for needle_index, raw_needle in enumerate(needles):
            needle = _text(raw_needle, f"{label}.needles[{needle_index}]", 80)
            if needle != needle.lower():
                raise BrandingError(f"{label}.needles must already be lowercase.")
            if needle in normalized_needles:
                raise BrandingError(f"{label}.needles contains a duplicate.")
            normalized_needles.append(needle)
        rules.append({
            "runtimeProvider": runtime,
            "id": rule_id,
            "providerLabel": _text(rule["providerLabel"], f"{label}.providerLabel"),
            "providerClass": provider_class,
            "needles": normalized_needles,
            "fallbackModelLabel": _text(
                rule["fallbackModelLabel"], f"{label}.fallbackModelLabel", 120),
        })

    return {
        "schemaVersion": 1,
        "fieldConvention": "TaskWraith camelCase",
        "sourceContract": source,
        "fallbackHueKey": fallback,
        "accents": accents,
        "accentAliases": aliases,
        "providers": providers,
        "modelLabels": model_labels,
        "modelBrandOverrides": rules,
    }


def load_branding(path: Path | None = None) -> dict:
    """Load and strictly validate the portable branding catalogue."""
    source = path or BRANDING_PATH
    if source.is_symlink():
        raise BrandingError("Refusing to load a branding catalogue symlink.")
    try:
        value = json.loads(source.read_text())
    except (OSError, ValueError) as exc:
        raise BrandingError(f"Cannot read {source.name}.") from exc
    return _validate_catalogue(value)


def validate_overrides(value, catalogue: Mapping | None = None) -> dict:
    """Validate persisted sparse overrides keyed by the real runtime provider.

    The absence of a ``runtimeProvider`` field is deliberate.  A display
    override cannot redirect inference, credentials, quota attribution, or
    billing to a different provider.
    """
    catalog = _validate_catalogue(catalogue) if catalogue is not None else load_branding()
    if value is None:
        return {}
    overrides = _object(value, "branding_overrides")
    result = {}
    for raw_id, raw_override in overrides.items():
        provider_id = _slug(raw_id, "branding_overrides provider id")
        override = _object(raw_override, f"branding_overrides.{provider_id}")
        _known_keys(override, USER_OVERRIDE_KEYS, f"branding_overrides.{provider_id}")
        normalized = {}
        if "displayProvider" in override:
            normalized["displayProvider"] = _text(
                override["displayProvider"], f"branding_overrides.{provider_id}.displayProvider")
        if "hueKey" in override:
            hue = _slug(override["hueKey"], f"branding_overrides.{provider_id}.hueKey")
            if _resolved_hue_key(catalog, hue) is None:
                raise BrandingError(
                    f"branding_overrides.{provider_id}.hueKey has no concrete accent.")
            normalized["hueKey"] = hue
        if "accent" in override:
            normalized["accent"] = _colour(
                override["accent"], f"branding_overrides.{provider_id}.accent")
        if "shortCode" in override:
            normalized["shortCode"] = _short_code(
                override["shortCode"], f"branding_overrides.{provider_id}.shortCode")
        if "modelLabels" in override:
            normalized["modelLabels"] = _model_labels(
                override["modelLabels"], f"branding_overrides.{provider_id}.modelLabels")
        if "logo" in override:
            normalized["logo"] = _logo(
                override["logo"], f"branding_overrides.{provider_id}.logo")
        result[provider_id] = normalized
    return result


def _accent(catalogue: Mapping, hue_key: str) -> str:
    resolved = _resolved_hue_key(catalogue, hue_key)
    if resolved is None:
        resolved = _resolved_hue_key(catalogue, catalogue["fallbackHueKey"])
    return catalogue["accents"][resolved]


def _display_from_slug(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("-"))


def _derived_short_code(value: str) -> str:
    letters = "".join(character for character in value.upper() if character.isalnum())
    return (letters[:3] or "TW").ljust(3, "W")


def _resolved_logo(logo: Mapping | None) -> dict | None:
    if not logo:
        return None
    result = copy.deepcopy(dict(logo))
    result["dark"] = result.get("dark", result["light"])
    return result


def _model_label(labels: Mapping, runtime: str, model: str) -> str:
    """Accept both raw ids and ``<runtime>/<id>`` catalogue keys."""
    candidates = [model]
    runtime_prefix = f"{runtime}/"
    if model.startswith(runtime_prefix):
        candidates.append(model[len(runtime_prefix):])
    else:
        candidates.append(runtime_prefix + model)
    return next((labels[key] for key in candidates if key in labels), "")


def resolve_presentation(
    runtime_provider: str,
    model_id: str | None = None,
    overrides: Mapping | None = None,
    supplied_label: str | None = None,
    *,
    catalogue: Mapping | None = None,
) -> dict:
    """Resolve TaskWraith-style display data without changing provider identity.

    Model-label precedence is supplied label, exact user override, exact
    catalogue label, ordered brand-rule fallback, then the unmodified model id.
    ``modelBrandOverrides`` use TaskWraith's ordered, case-insensitive
    substring matcher.  The model id is authoritative; only when no rule
    matches it does the matcher consider the supplied label.  This prevents a
    stale label from moving a live model to a different visible brand.
    """
    catalog = _validate_catalogue(catalogue) if catalogue is not None else load_branding()
    runtime = _slug(runtime_provider.strip() if isinstance(runtime_provider, str) else runtime_provider,
                    "runtime_provider")
    model = _model_id(model_id, "model_id") if model_id not in (None, "") else ""
    supplied = _text(supplied_label, "supplied_label", 120) if supplied_label not in (None, "") else ""
    normalized_overrides = validate_overrides(overrides, catalog)
    user = normalized_overrides.get(runtime, {})

    provider = catalog["providers"].get(runtime, {})
    display_provider = provider.get("displayProvider", _display_from_slug(runtime))
    base_hue = runtime if _resolved_hue_key(catalog, runtime) else catalog["fallbackHueKey"]
    hue_key = provider.get("hueKey", base_hue)
    short_code = provider.get("shortCode", _derived_short_code(display_provider))
    logo = provider.get("logo")

    brand_rule = None
    runtime_rules = [rule for rule in catalog["modelBrandOverrides"]
                     if rule["runtimeProvider"] == runtime]
    for search_key in (model.lower(), supplied.lower()):
        if search_key:
            brand_rule = next((rule for rule in runtime_rules
                               if any(needle in search_key for needle in rule["needles"])), None)
        if brand_rule:
            break
    if brand_rule:
        display_provider = brand_rule["providerLabel"]
        hue_key = brand_rule["providerClass"]
        branded_provider = catalog["providers"].get(hue_key, {})
        short_code = branded_provider.get("shortCode", _derived_short_code(display_provider))
        # Once the visible brand changes, retaining the runtime's logo would
        # claim two different makers in one chip. TaskWraith falls back to its
        # branded mnemonic glyph when an upstream has no sourced mark; this
        # portable contract expresses that as no logo.
        logo = branded_provider.get("logo")

    model_label = ""
    if model:
        model_label = (supplied
                       or _model_label(user.get("modelLabels", {}), runtime, model)
                       or _model_label(catalog["modelLabels"], runtime, model)
                       or (brand_rule or {}).get("fallbackModelLabel", "")
                       or model)

    display_provider = user.get("displayProvider", display_provider)
    hue_key = user.get("hueKey", hue_key)
    short_code = user.get("shortCode", short_code)
    logo = user.get("logo", logo)
    accent = user.get("accent", _accent(catalog, hue_key))

    result = {
        "runtimeProvider": runtime,
        "displayProvider": display_provider,
        "hueKey": hue_key,
        "accent": accent,
        "shortCode": short_code,
    }
    if model:
        result["model"] = model
        result["modelLabel"] = model_label
    resolved_logo = _resolved_logo(logo)
    if resolved_logo:
        result["logo"] = resolved_logo
    return result
