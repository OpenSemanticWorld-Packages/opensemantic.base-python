"""URL-backed configuration for Panel apps using Pydantic models.

Provides :class:`UrlConfig`, a generic helper that serializes/deserializes a
Pydantic ``BaseModel`` to and from browser URL query parameters via
``pn.state.location``. Two encoding modes are supported:

- ``PLAIN_KEYS``: one URL parameter per (dot-flattened) field.
- ``COMPRESSED_BASE64``: a single parameter holding zlib-compressed,
  base64url-encoded JSON.

This is opt-in tooling: the dashboard views do not use it unless a caller
enables it, so a host app that owns its own persistence is unaffected. It works
for a single view config or for a host-composed parent model alike.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
import zlib
from enum import Enum
from typing import Any, Dict, Generic, Type, TypeVar

import panel as pn
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class UrlConfigMode(Enum):
    """Encoding mode for URL config parameters.

    - ``PLAIN_KEYS``: human-readable, one dot-flattened param per leaf (each a
      JSON-encoded value). Round-trips arbitrary typed content; the one caveat
      is dict *keys* that themselves contain a dot (e.g. an IRI used as a
      ``unit_selections`` key).
    - ``JSON``: the model's JSON as a single (URL-encoded) param - readable but
      compact, no key-with-dot caveat.
    - ``COMPRESSED_BASE64`` (default): zlib-compressed, base64url-encoded JSON in
      a single param - shortest, opaque.
    """

    PLAIN_KEYS = "plain_keys"
    JSON = "json"
    COMPRESSED_BASE64 = "compressed_base64"


# -- Flatten / unflatten ----------------------------------------------------


def _flatten_dict(data: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    """Flatten a nested dict into dot-separated keys with JSON-encoded leaves.

    Lists use numeric indices (e.g. ``items.0.name``). Each scalar leaf - and
    each *empty* container - is stored as ``json.dumps(value)`` so types
    (bool/int/float/None and empty ``[]`` / ``{}``) round-trip losslessly. This
    is what lets arbitrary ``Any`` content (e.g. a Wunderbaum tree source, whose
    nodes carry a bool ``selected``) survive PLAIN_KEYS, not just typed fields.
    """
    result: Dict[str, str] = {}

    def _walk(key: str, value: Any) -> None:
        if isinstance(value, dict) and value:
            for k, v in value.items():
                _walk(f"{key}.{k}", v)
        elif isinstance(value, list) and value:
            for i, item in enumerate(value):
                _walk(f"{key}.{i}", item)
        else:
            # Scalar, None, or empty container -> a typed JSON leaf.
            result[key] = json.dumps(value)

    for key, value in data.items():
        _walk(f"{prefix}.{key}" if prefix else key, value)
    return result


def _unflatten_dict(flat: Dict[str, str]) -> Dict[str, Any]:
    """Rebuild a nested dict from dot-separated flat keys.

    Numeric path segments produce lists; all others produce dicts. Leaves are
    ``json.loads``-decoded back to their original type (falling back to the raw
    string if a value is not valid JSON).
    """
    root: Dict[str, Any] = {}
    for compound_key in sorted(flat):
        raw = flat[compound_key]
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = raw
        parts = compound_key.split(".")
        current: Any = root
        for i, part in enumerate(parts[:-1]):
            next_part = parts[i + 1]
            next_is_index = next_part.isdigit()
            if isinstance(current, list):
                idx = int(part)
                while len(current) <= idx:
                    current.append([] if next_is_index else {})
                if current[idx] is None:
                    current[idx] = [] if next_is_index else {}
                current = current[idx]
            else:
                if part not in current:
                    current[part] = [] if next_is_index else {}
                current = current[part]

        # Assign the leaf value.
        leaf = parts[-1]
        if isinstance(current, list):
            idx = int(leaf)
            while len(current) <= idx:
                current.append(None)
            current[idx] = value
        else:
            current[leaf] = value
    return root


# -- Compression ------------------------------------------------------------


def _compress_config(config: BaseModel) -> str:
    """Serialize a Pydantic model to zlib-compressed, URL-safe base64."""
    json_bytes = config.model_dump_json().encode("utf-8")
    compressed = zlib.compress(json_bytes)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


def _decompress_config(encoded: str, model_class: Type[T]) -> T:
    """Deserialize a compressed base64url string back into a Pydantic model."""
    try:
        compressed = base64.urlsafe_b64decode(encoded)
        json_bytes = zlib.decompress(compressed)
        data = json.loads(json_bytes)
        return model_class.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Failed to decompress config: {exc}") from exc


# -- URL parameter I/O ------------------------------------------------------


def _read_query_params() -> Dict[str, str]:
    """Read current URL query parameters as a flat string dict."""
    location = pn.state.location
    if location is None or not location.search:
        return {}
    parsed = urllib.parse.parse_qs(location.search.lstrip("?"), keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def _write_query_params(params: Dict[str, str]) -> None:
    """Write query parameters to the URL, replacing the full query string."""
    location = pn.state.location
    if location is None:
        logger.warning("pn.state.location is None; cannot write URL params.")
        return
    if params:
        location.search = "?" + urllib.parse.urlencode(params)
    else:
        location.search = ""


class UrlConfig(Generic[T]):
    """Generic URL-backed configuration manager for Pydantic models.

    Serializes and deserializes a Pydantic ``BaseModel`` to/from browser URL
    query parameters via Panel's ``pn.state.location``. The type parameter
    ``T`` must be a Pydantic ``BaseModel`` subclass.

    Example::

        class MySettings(BaseModel):
            theme: str = "light"
            font_size: int = 14

        url_cfg = UrlConfig(MySettings, param_name="settings")
        settings = url_cfg.get_config()   # reads from URL or returns defaults
        url_cfg.set_config(settings)      # writes to URL
    """

    def __init__(self, model_class: Type[T], param_name: str = "config") -> None:
        self.model_class = model_class
        self.param_name = param_name

    # -- Public API ---------------------------------------------------------

    def has_config(self) -> bool:
        """Whether the URL currently carries params for this config."""
        params = _read_query_params()
        prefix = f"{self.param_name}."
        return self.param_name in params or any(k.startswith(prefix) for k in params)

    def get_config(self) -> T:
        """Read configuration from URL query parameters.

        Auto-detects the encoding mode:

        1. A key matching ``param_name`` -> ``COMPRESSED_BASE64``.
        2. Keys starting with ``{param_name}.`` -> ``PLAIN_KEYS``.
        3. Otherwise -> a default model instance.
        """
        params = _read_query_params()
        if not params:
            return self.model_class()

        # Single-param modes: try COMPRESSED_BASE64, then JSON.
        if self.param_name in params:
            raw = params[self.param_name]
            try:
                return _decompress_config(raw, self.model_class)
            except ValueError:
                pass
            try:
                return self.model_class.model_validate_json(raw)
            except Exception:
                logger.debug(
                    "Key '%s' found but single-param decode failed; "
                    "trying PLAIN_KEYS.",
                    self.param_name,
                )

        # Attempt 2: PLAIN_KEYS
        prefix = f"{self.param_name}."
        plain_params = {
            k[len(prefix) :]: v for k, v in params.items() if k.startswith(prefix)
        }
        if plain_params:
            nested = _unflatten_dict(plain_params)
            try:
                return self.model_class.model_validate(nested)
            except Exception as exc:
                logger.warning("PLAIN_KEYS decode failed: %s. Returning default.", exc)
                return self.model_class()

        # Attempt 3: no matching params
        return self.model_class()

    def set_config(
        self,
        config: T,
        mode: UrlConfigMode = UrlConfigMode.COMPRESSED_BASE64,
    ) -> None:
        """Write configuration to URL query parameters.

        Preserves existing query parameters that do not belong to this config.
        """
        existing = _read_query_params()
        prefix = f"{self.param_name}."
        preserved = {
            k: v
            for k, v in existing.items()
            if k != self.param_name and not k.startswith(prefix)
        }

        if mode is UrlConfigMode.COMPRESSED_BASE64:
            preserved[self.param_name] = _compress_config(config)
        elif mode is UrlConfigMode.JSON:
            preserved[self.param_name] = config.model_dump_json()
        elif mode is UrlConfigMode.PLAIN_KEYS:
            # mode="json" so enums serialize to their values and datetimes to
            # ISO strings (str() on an enum would emit "Cls.MEMBER").
            flat = _flatten_dict(config.model_dump(mode="json"), prefix=self.param_name)
            preserved.update(flat)
        else:
            raise ValueError(f"Unknown UrlConfigMode: {mode}")

        _write_query_params(preserved)

    def clear_config(self) -> None:
        """Remove this config's parameters from the URL, preserving others."""
        existing = _read_query_params()
        prefix = f"{self.param_name}."
        preserved = {
            k: v
            for k, v in existing.items()
            if k != self.param_name and not k.startswith(prefix)
        }
        _write_query_params(preserved)

    def bind(
        self, mode: UrlConfigMode = UrlConfigMode.COMPRESSED_BASE64
    ) -> "BoundConfig[T]":
        """Read config from the URL and return an auto-syncing proxy.

        The returned :class:`BoundConfig` wraps the model instance; any field
        assignment on the proxy writes the updated model back to the URL. The
        model class should use ``ConfigDict(validate_assignment=True)`` so that
        Pydantic allows field mutation.
        """
        model = self.get_config()
        return BoundConfig(model, self, mode)


class BoundConfig(Generic[T]):
    """Proxy that auto-syncs Pydantic model field changes to URL params.

    Attribute reads are forwarded to the wrapped model; attribute writes to
    model fields trigger :meth:`UrlConfig.set_config` automatically.
    """

    def __init__(self, model: T, url_config: UrlConfig[T], mode: UrlConfigMode) -> None:
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_url_config", url_config)
        object.__setattr__(self, "_mode", mode)

    def __getattr__(self, name: str):
        return getattr(self._model, name)

    def __setattr__(self, name: str, value):
        setattr(self._model, name, value)
        if name in type(self._model).model_fields:
            self._url_config.set_config(self._model, self._mode)
