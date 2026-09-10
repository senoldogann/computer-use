"""OODA decision contract as a backend-strict JSON schema (pure).

Generated from the Pydantic :class:`AgentTurn` model, so a schema a CLI
enforces can never drift from the contract ``parse_decision`` validates:
one source of truth, two enforcers. Shared by every subscription-CLI
transport that can enforce a response shape (Codex ``--output-schema``,
Claude ``--json-schema``).
"""

from __future__ import annotations

import logging
from typing import Final, cast

LOGGER: Final = logging.getLogger(__name__)

#: Keys the strict-schema normalizer strips. Shape is what the backend
#: enforces; semantics stay with ``parse_decision``. A stripped constraint
#: only loosens the schema (fewer rejections), never the validation.
_STRIPPED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "title",
        "description",
        "default",
        "discriminator",
        "format",
        "examples",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
    }
)

#: Action variant the strict schema cannot express. ``call_tool.arguments``
#: is a free-form map, and strict objects allow no free-form maps
#: (``additionalProperties: true`` is rejected, a bare object is rejected).
#: The variant is excluded from the *schema*, not the contract: if the model
#: emits one anyway, ``parse_decision`` still validates it. MCP tools are
#: therefore degraded on schema-enforcing transports (never offered by
#: shape), never silently mis-shaped.
_UNSCHEMATIZABLE_VARIANTS: Final[frozenset[str]] = frozenset({"call_tool"})


def as_dict(value: object) -> dict[str, object]:
    """Narrow an unknown JSON node to a fully-typed dict (Law 6 strict typing).

    ``isinstance`` narrows to ``dict[Unknown, Unknown]`` under strict mode,
    which poisons every downstream ``.get`` — the same narrowing the OpenAI
    transport handles with an explicit cast, because the value came from
    ``json.loads`` and its keys are strings by construction.
    """
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, object], value)


def as_list(value: object, what: str) -> list[object]:
    """Narrow an unknown JSON node to a list (strict-mode cast companion)."""
    if not isinstance(value, list):
        raise TypeError(f"{what} is not a list")
    return cast(list[object], value)


def _variant_name(member: dict[str, object]) -> str:
    """The action type a normalized variant selects on (pure)."""
    type_node = as_dict(member.get("properties")).get("type")
    marker = as_dict(type_node)
    const = marker.get("const")
    if isinstance(const, str):
        return const
    enum = as_list(marker["enum"], "enum") if "enum" in marker else []
    if len(enum) == 1 and isinstance(enum[0], str):
        return enum[0]
    return ""


def _strict_node(node: object, defs: dict[str, object]) -> dict[str, object]:
    """Normalize one JSON-schema node to the strict subset (pure).

    Rules, each measured against the live backend: every object is fully
    specified (``properties`` + ``required`` covering all of them +
    ``additionalProperties: false``); ``oneOf`` becomes ``anyOf``;
    single-value ``const`` becomes a one-element ``enum``; ``$ref`` is
    inlined (no remote or local references survive); everything in
    :data:`_STRIPPED_KEYS` is dropped. Anything unrecognized raises: a
    schema that cannot be proven strict must fail at startup, never as a
    rejected turn mid-run.
    """
    mapping = as_dict(node)
    if "$ref" in mapping:
        ref = mapping["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            raise ValueError(f"unsupported schema reference: {ref!r}")
        target = defs.get(ref[len("#/$defs/"):])
        if target is None:
            raise ValueError(f"schema reference names unknown def: {ref!r}")
        return _strict_node(target, defs)
    if "oneOf" in mapping:
        members = as_list(mapping["oneOf"], "oneOf")
        normalized = [_strict_node(member, defs) for member in members]
        kept = [
            strict
            for strict in normalized
            if _variant_name(strict) not in _UNSCHEMATIZABLE_VARIANTS
        ]
        if len(kept) != len(normalized):
            LOGGER.info(
                "strict schema excludes %d inexpressible variant(s): %s",
                len(normalized) - len(kept),
                sorted(_UNSCHEMATIZABLE_VARIANTS),
            )
        return {"anyOf": kept}
    if "anyOf" in mapping:
        members = as_list(mapping["anyOf"], "anyOf")
        return {"anyOf": [_strict_node(member, defs) for member in members]}
    node_type = mapping.get("type")
    if node_type == "object" or "properties" in mapping:
        properties = as_dict(mapping.get("properties"))
        strict_properties = {
            key: _strict_node(value, defs) for key, value in properties.items()
        }
        return {
            "type": "object",
            "properties": strict_properties,
            "required": sorted(strict_properties),
            "additionalProperties": False,
        }
    if node_type == "array":
        return {"type": "array", "items": _strict_node(mapping.get("items"), defs)}
    if "const" in mapping:
        return {"enum": [mapping["const"]]}
    if "enum" in mapping:
        enum = as_list(mapping["enum"], "enum")
        if "type" in mapping:
            return {"type": mapping["type"], "enum": enum}
        return {"enum": enum}
    if node_type in ("string", "integer", "number", "boolean", "null"):
        return {"type": node_type}
    raise ValueError(f"cannot express in the strict subset: {str(mapping)[:200]}")


def strict_decision_schema() -> dict[str, object]:
    """The OODA decision contract as a backend-strict JSON schema (pure).

    Built once per transport (fail fast at startup, not mid-run).
    """
    from computeruse.orchestrator.schemas import AgentTurn

    raw = cast(dict[str, object], AgentTurn.model_json_schema())
    defs = as_dict(raw.get("$defs"))
    top = {key: value for key, value in raw.items() if key not in ("$defs",)}
    return _strict_node(top, defs)
