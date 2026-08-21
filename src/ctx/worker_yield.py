"""Dependency-free validation for typed orchestrator worker yields.

This is intentionally a documented JSON-Schema subset rather than an optional
dependency changing runtime behavior.  Unsupported assertion keywords are
rejected when a route is built, so strict validation never silently weakens.
"""

from __future__ import annotations

from typing import Any


class WorkerYieldSchemaError(ValueError):
    pass


_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_ASSERTIONS = {
    "type", "required", "properties", "additionalProperties", "items",
    "enum", "const", "minItems", "maxItems", "minLength", "maxLength",
    "minimum", "maximum",
}
_ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples"}


def check_schema(schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise WorkerYieldSchemaError(f"{path}: schema must be an object")
    unsupported = sorted(set(schema) - _ASSERTIONS - _ANNOTATIONS)
    if unsupported:
        raise WorkerYieldSchemaError(f"{path}: unsupported keywords: {', '.join(unsupported)}")
    declared = schema.get("type")
    if declared is not None:
        values = [declared] if isinstance(declared, str) else declared
        if not isinstance(values, list) or not values or any(v not in _TYPES for v in values):
            raise WorkerYieldSchemaError(f"{path}.type: unsupported type declaration")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(v, str) for v in required):
        raise WorkerYieldSchemaError(f"{path}.required: must be an array of strings")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise WorkerYieldSchemaError(f"{path}.properties: must be an object")
    for name, child in properties.items():
        check_schema(child, f"{path}.properties.{name}")
    if isinstance(schema.get("additionalProperties"), dict):
        check_schema(schema["additionalProperties"], f"{path}.additionalProperties")
    elif "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise WorkerYieldSchemaError(f"{path}.additionalProperties: must be boolean or schema")
    if "items" in schema:
        check_schema(schema["items"], f"{path}.items")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise WorkerYieldSchemaError(f"{path}.enum: must be an array")


def _matches_type(value: Any, name: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[name]


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Return deterministic validation errors (empty means valid)."""
    declared = schema.get("type")
    types = [declared] if isinstance(declared, str) else (declared or [])
    if types and not any(_matches_type(value, name) for name in types):
        return [f"{path}: expected {' or '.join(types)}"]
    if "const" in schema and value != schema["const"]:
        return [f"{path}: value does not match const"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: value is not in enum"]
    errors: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required property is missing")
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in properties:
                errors.extend(validate(child, properties[key], f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}.{key}: additional property is not allowed")
            elif isinstance(additional, dict):
                errors.extend(validate(child, additional, f"{path}.{key}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: more than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: longer than maxLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    return errors
