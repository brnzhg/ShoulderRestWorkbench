"""Plain-data helpers: merging TOML tables and converting them to parameter dataclasses.

Version files store only what differs from their parent, so tables merge
recursively and everything else replaces. ``false`` in place of a table turns
an optional parameter group off (it becomes ``None``).
"""

import dataclasses
import types
from collections.abc import Mapping
from difflib import get_close_matches
from typing import Any, Union, get_args, get_origin, get_type_hints

Table = dict[str, Any]


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Table:
    """A new table with ``override`` merged into ``base``; nested tables merge, values replace."""
    merged: Table = {key: _copy(value) for key, value in base.items()}
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = _copy(value)
    return merged


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


def to_dataclass[T](cls: type[T], data: Mapping[str, Any], where: str = "") -> T:
    """Build a frozen parameter dataclass from a table, recursing into nested groups.

    Missing keys keep the dataclass defaults. Unknown keys raise with a suggestion.
    """
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")
    hints = get_type_hints(cls)
    names = [field.name for field in dataclasses.fields(cls) if field.init]
    kwargs: Table = {}
    for key, value in data.items():
        path = f"{where}.{key}" if where else key
        if key not in names:
            close = get_close_matches(key, names, n=1)
            hint = f"; did you mean {close[0]!r}?" if close else f"; expected one of {names}"
            raise ValueError(f"Unknown parameter {path!r} for {cls.__name__}{hint}")
        kwargs[key] = _convert(hints[key], value, path)
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{where or cls.__name__}: {error}") from error


def _convert(hint: Any, value: Any, path: str) -> Any:
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        options = [arg for arg in get_args(hint) if arg is not type(None)]
        if (value is False or value is None) and len(options) < len(get_args(hint)):
            return None
        if len(options) == 1:
            return _convert(options[0], value, path)
        return value
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        if isinstance(value, hint):
            return value
        if isinstance(value, Mapping):
            return to_dataclass(hint, value, path)
        if isinstance(value, list):  # Positional form, e.g. a stroke as [length, angle].
            names = [field.name for field in dataclasses.fields(hint) if field.init]
            return to_dataclass(hint, dict(zip(names, value, strict=False)), path)
        raise ValueError(f"{path} must be a table")
    if origin is tuple:
        args = get_args(hint)
        if not isinstance(value, list | tuple):
            raise ValueError(f"{path} must be an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_convert(args[0], item, f"{path}[{i}]") for i, item in enumerate(value))
        if len(args) != len(value):
            raise ValueError(f"{path} must have {len(args)} items")
        return tuple(_convert(arg, item, f"{path}[{i}]") for i, (arg, item) in enumerate(zip(args, value, strict=True)))
    if hint is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def to_table(value: Any) -> Any:
    """Plain TOML/JSON-friendly data from parameter dataclasses, tuples and None."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_table(getattr(value, field.name))
            for field in dataclasses.fields(value) if field.init
        }
    if isinstance(value, list | tuple):
        return [to_table(item) for item in value]
    if isinstance(value, Mapping):
        return {key: to_table(item) for key, item in value.items()}
    if value is None:
        return False
    return value


def flatten(table: Mapping[str, Any], prefix: str = "") -> Table:
    """Dotted keys for every leaf, with arrays indexed, e.g. ``rest.path.strokes.0.angle``."""
    flat: Table = {}
    for key, value in table.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flat.update(flatten(value, path))
        elif isinstance(value, list) and value:
            flat.update(flatten({str(i): v for i, v in enumerate(value)}, path))
        else:
            flat[path] = value
    return flat
