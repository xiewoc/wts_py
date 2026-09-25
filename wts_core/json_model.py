"""Snake_case JSON helpers for the shared v1 data contract.

The Rust core derives ``Serialize``/``Deserialize`` and controls the wire shape
with ``serde`` attributes. The Python port keeps the same wire shape with two
class-level switches that mirror the two attributes actually used:

* :attr:`JsonModel.omit_when_none` - ``skip_serializing_if = "Option::is_none"``
* :attr:`JsonModel.omit_when_empty` - ``skip_serializing_if = "String::is_empty"``

Fields absent from both sets are always emitted, so an unknown optional value
becomes JSON ``null`` exactly like ``serde`` does by default.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from types import UnionType
from typing import Any, ClassVar, Self, Union, get_args, get_origin, get_type_hints


class JsonModel:
    """Base class for contract v1 payloads."""

    omit_when_none: ClassVar[frozenset[str]] = frozenset()
    omit_when_empty: ClassVar[frozenset[str]] = frozenset()
    #: JSON key renames, for the few fields whose Rust name is a keyword.
    renames: ClassVar[dict[str, str]] = {}

    @classmethod
    def json_key(cls, name: str) -> str:
        return cls.renames.get(name, name)

    @classmethod
    def _encode(cls, value: Any) -> Any:
        if isinstance(value, JsonModel):
            return value.to_dict()
        if isinstance(value, list):
            return [cls._encode(item) for item in value]
        if isinstance(value, dict):
            return {key: cls._encode(item) for key, item in value.items()}
        return value

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field in fields(self):  # type: ignore[arg-type]
            value = getattr(self, field.name)
            if field.name in self.omit_when_none and value is None:
                continue
            if field.name in self.omit_when_empty and value == "":
                continue
            payload[self.json_key(field.name)] = self._encode(value)
        return payload

    @classmethod
    def _decode(cls, annotation: Any, value: Any) -> Any:
        if value is None:
            return None
        origin = get_origin(annotation)
        if origin is list:
            if not isinstance(value, list):
                raise TypeError("需要 JSON 数组。")
            item_type = get_args(annotation)[0]
            return [cls._decode(item_type, item) for item in value]
        if origin is dict:
            if not isinstance(value, dict):
                raise TypeError("需要 JSON 对象。")
            item_type = get_args(annotation)[1]
            return {key: cls._decode(item_type, item) for key, item in value.items()}
        if origin in (Union, UnionType):
            for member in get_args(annotation):
                if member is type(None):
                    continue
                return cls._decode(member, value)
            return value
        if isinstance(annotation, type) and issubclass(annotation, JsonModel):
            return annotation.from_dict(value)
        return value

    @classmethod
    def from_dict(cls, raw: Any) -> Self | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise TypeError(f"{cls.__name__} 需要 JSON 对象。")
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for field in fields(cls):  # type: ignore[arg-type]
            key = cls.json_key(field.name)
            if key in raw:
                kwargs[field.name] = cls._decode(hints.get(field.name, Any), raw[key])
                continue
            if field.default is not MISSING or field.default_factory is not MISSING:  # type: ignore[misc]
                continue
            raise TypeError(f"{cls.__name__} 缺少字段 {key}。")
        return cls(**kwargs)


def default_list() -> list:
    return []
