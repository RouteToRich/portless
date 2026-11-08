"""Build valid Pydantic records for tests: only the fields a test cares about are named."""
from __future__ import annotations

import enum
import types
import typing
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel

NOW = datetime.now(UTC)


def _value(name: str, annotation):
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        return args[0]
    if origin in (typing.Union, types.UnionType):
        return None if type(None) in args else _value(name, args[0])
    if origin is list:
        return []
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return make(annotation)
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation))
        for kind, sample in (
            (bool, True), (int, 1), (float, 1.0), (Decimal, Decimal(1)), (datetime, NOW), (date, NOW.date()),
        ):
            if issubclass(annotation, kind):
                return sample
        if issubclass(annotation, str):
            return f"{name}-1"
    raise TypeError(f"no sample value for {name}: {annotation!r}")


def make(model: type[BaseModel], **values):
    """A valid *model* with every required field filled in, overridden by *values*."""
    hints = typing.get_type_hints(model)
    filled = {
        name: _value(name, hints[name])
        for name, field in model.model_fields.items()
        if field.is_required() and name not in values
    }
    return model(**filled, **values)
