from __future__ import annotations

import string
from typing import Any, Iterable, Mapping


class TemplateError(ValueError):
    """Raised when a template cannot be rendered safely."""


_FORMATTER = string.Formatter()


def referenced_fields(value: str) -> set[str]:
    fields: set[str] = set()
    for _, field_name, _, _ in _FORMATTER.parse(value):
        if field_name is None:
            continue
        if not field_name.isidentifier():
            raise TemplateError(
                f"Invalid template field '{field_name}'. Only simple identifiers are allowed."
            )
        fields.add(field_name)
    return fields


def render_template(value: str, context: Mapping[str, Any]) -> str:
    missing = [field for field in referenced_fields(value) if field not in context]
    if missing:
        joined = ", ".join(sorted(missing))
        raise TemplateError(f"Missing template values for: {joined}")
    safe_context = {key: "" if raw is None else str(raw) for key, raw in context.items()}
    try:
        return value.format_map(safe_context)
    except Exception as exc:  # pragma: no cover - defensive
        raise TemplateError(f"Failed to render template '{value}': {exc}") from exc


def render_sequence(values: Iterable[str], context: Mapping[str, Any]) -> list[str]:
    return [render_template(value, context) for value in values]


def render_mapping(values: Mapping[str, str], context: Mapping[str, Any]) -> dict[str, str]:
    return {key: render_template(value, context) for key, value in values.items()}

