"""Provider-neutral model responses and tool definitions."""

from dataclasses import dataclass, field
import json
import re


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict
    call_id: str = ""


@dataclass(frozen=True)
class ModelCompletion:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


def parse_tool_arguments(value):
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    if not isinstance(value, str):
        raise ValueError("tool arguments must be a JSON object")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"tool arguments are not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must decode to a JSON object")
    return parsed


def _field_schema(type_spec):
    if isinstance(type_spec, dict):
        return dict(type_spec)
    raw = str(type_spec).strip()
    base = raw.split("=", 1)[0].strip()
    array_match = re.fullmatch(r"list\[(.+)]", base)
    if array_match:
        return {"type": "array", "items": _field_schema(array_match.group(1))}
    return {
        "str": {"type": "string"},
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "dict": {"type": "object"},
        "list": {"type": "array"},
    }.get(base, {"type": "string"})


def tool_parameters(schema):
    properties = {}
    required = []
    for name, type_spec in schema.items():
        properties[str(name)] = _field_schema(type_spec)
        # Strict function calling requires every declared property to be
        # required. Runtime defaults remain available to the text fallback.
        required.append(str(name))
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def tool_definitions(tools):
    return [
        {
            "name": name,
            "description": str(tool.get("description", "")),
            "parameters": tool_parameters(tool.get("schema", {})),
        }
        for name, tool in sorted(tools.items())
    ]
