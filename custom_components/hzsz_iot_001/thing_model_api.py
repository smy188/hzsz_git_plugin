"""HTTP client to fetch thing model definitions from the Java backend."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import DEFAULT_THING_MODEL_URL, THING_MODEL_API

_LOGGER = logging.getLogger(__name__)


class ThingModelApi:
    """Fetch and cache thing model definitions from the Java IoT backend."""

    def __init__(self, base_url: str = DEFAULT_THING_MODEL_URL) -> None:
        self._base_url = base_url.rstrip("/")
        # Cache key: (model, version). version may be None for the default thing model.
        self._cache: dict[tuple[str, str | None], dict[str, Any]] = {}

    # ------------------------------------------------------------------
    #  Cache management
    # ------------------------------------------------------------------

    def clear_cache(self, model: str | None = None) -> None:
        """Clear the in-memory thing model cache.

        If model is None, clears ALL cached thing models.
        Otherwise, clears only entries for the given model.
        """
        if model is None:
            count = len(self._cache)
            self._cache.clear()
            _LOGGER.info("Cleared entire thing model cache (%d entries)", count)
        else:
            keys = [k for k in self._cache if k[0] == model]
            for k in keys:
                del self._cache[k]
            _LOGGER.info("Cleared thing model cache for model=%s (%d entries)", model, len(keys))

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    async def async_fetch_thing_model(
        self, model: str, version: str | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        """Get the full thing model for a given device model and version.

        Calls GET {base_url}/iot/thing-model/get-by-model?model={model}&version={version}
        When version is None/empty, the Java backend returns the default thing model
        for the given model.

        Returns the parsed JSON dict, or None on failure.
        Results are cached in memory unless force_refresh=True.
        """
        cache_key = (model, version)
        if not force_refresh and cache_key in self._cache:
            _LOGGER.debug("Thing model %s (version=%s) hit cache", model, version)
            return self._cache[cache_key]

        if force_refresh:
            _LOGGER.info("Force-refreshing thing model for %s (version=%s), bypassing cache", model, version)

        url = f"{self._base_url}{THING_MODEL_API}"
        params: dict[str, Any] = {"model": model}
        if version:
            params["version"] = version
        _LOGGER.info(
            "Fetching thing model: GET %s?model=%s&version=%s",
            url,
            model,
            version or "<default>",
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    _LOGGER.info(
                        "Thing model API response: HTTP %d for model=%s version=%s",
                        resp.status,
                        model,
                        version or "<default>",
                    )
                    resp.raise_for_status()
                    body = await resp.json()
                    _LOGGER.debug(
                        "Thing model API raw body for %s (version=%s): %s",
                        model,
                        version or "<default>",
                        body,
                    )
        except aiohttp.ClientError as exc:
            _LOGGER.error(
                "Failed to fetch thing model for %s (version=%s) from %s (HTTP/network error): %s",
                model,
                version or "<default>",
                url,
                exc,
            )
            return None
        except asyncio.TimeoutError:
            _LOGGER.error(
                "Timeout fetching thing model for %s (version=%s) from %s (10s)",
                model,
                version or "<default>",
                url,
            )
            return None
        except ValueError as exc:
            _LOGGER.error(
                "Invalid JSON from thing model API for %s (version=%s): %s",
                model,
                version or "<default>",
                exc,
            )
            return None

        # Extract data from CommonResult wrapper
        # Response format: {"code": 0, "data": {...}, "msg": "..."}
        if not isinstance(body, dict):
            _LOGGER.error(
                "Thing model API returned unexpected type for %s (version=%s): %s",
                model,
                version or "<default>",
                type(body),
            )
            return None

        code = body.get("code")
        msg = body.get("msg", "")
        data: dict[str, Any] | None = body.get("data")

        if code != 0 or data is None:
            _LOGGER.error(
                "Thing model API business error for model=%s version=%s: code=%s msg=%s data=%s",
                model,
                version or "<default>",
                code,
                msg,
                data,
            )
            return None

        self._cache[cache_key] = data
        entities = data.get("entities", [])
        prop_count = sum(len(e.get("properties", [])) for e in entities)
        _LOGGER.info(
            "Fetched thing model for %s (version=%s, modelName=%s): %d entities, %d properties",
            model,
            data.get("version", "<default>"),
            data.get("modelName", "?"),
            len(entities),
            prop_count,
        )
        for entity in entities:
            props = entity.get("properties", [])
            _LOGGER.debug(
                "  Entity: type=%s id=%s name=%s — %d properties",
                entity.get("entityType", "?"),
                entity.get("entityIdentifier", "?"),
                entity.get("entityName", "?"),
                len(props),
            )
            for prop in props:
                _LOGGER.debug(
                    "    Property: identifier=%s role=%s dataType=%s deviceClass=%s",
                    prop.get("identifier", "?"),
                    prop.get("role", ""),
                    prop.get("dataType", "?"),
                    prop.get("deviceClass", "?"),
                )
        return data

    def get_cached(self, model: str, version: str | None = None) -> dict[str, Any] | None:
        """Return the cached thing model without making an HTTP request."""
        return self._cache.get((model, version))

    async def async_fetch_model_list(self) -> list[str]:
        """Fetch the list of all enabled model names from the Java backend.

        Calls GET {base_url}/admin-api/iot/thing-model/list-models
        Returns a list of model strings (e.g. ["AM307-470M", "VS121-470M"]).
        """
        url = f"{self._base_url}/admin-api/iot/thing-model/list-models"
        _LOGGER.info("Fetching model list from %s", url)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    body = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            _LOGGER.error("Failed to fetch model list: %s", exc)
            return []

        if not isinstance(body, dict):
            return []

        code = body.get("code")
        data = body.get("data")
        if code != 0 or not isinstance(data, list):
            _LOGGER.error("Model list API error: code=%s", code)
            return []

        models = [item.get("model", "") for item in data if isinstance(item, dict) and item.get("model")]
        _LOGGER.info("Fetched %d enabled models: %s", len(models), models)
        return models
