"""Device hub — manages HZSZ Bluetooth devices dynamically using the thing model.

All devices are registered dynamically via MQTT Register or UplinkData.
There is no static hardcoded device list.

Dynamic devices are cached to .storage/hzsz_iot_001_devices.json.

Each device stores MQTT data and notifies entity callbacks on update.

Key differences from milesight_iot:
  - Routing field defaults to ``deviceId`` (not ``devEUI``).
  - MQTT data is nested under a ``data`` sub-object and is flattened
    before being stored on the device.
  - Device names are parsed from the ``/register`` topic payload which
    explicitly provides ``model``, ``deviceId``, and ``firmwareVersion``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

if TYPE_CHECKING:
    from .thing_model_api import ThingModelApi

_LOGGER = logging.getLogger(__name__)

# Dispatcher signal for new-device notification
SIGNAL_NEW_DEVICE = "hzsz_iot_001_new_device"

# Keys that are considered message metadata, not sensor field values.
METADATA_KEYS = frozenset(
    {"deviceId", "model", "firmwareVersion", "gatewaySn", "timestamp", "data"}
)

# Model prefixes used to match incoming MQTT device model.
# Populated dynamically from the Java API at startup; static list is a fallback.
MODEL_PREFIXES: list[str] = []

CACHE_DIR = ".storage"
CACHE_FILE = "hzsz_iot_001_devices.json"


# ---------------------------------------------------------------------------
#  Dynamic Device — reads all properties from the thing model
# ---------------------------------------------------------------------------


class DynamicDevice:
    """A device whose shape is defined by a thing model dict.

    Properties are not hardcoded — they come from the thing model's
    ``properties`` list, each entry providing identifier / dataType / etc.
    """

    def __init__(
        self,
        device_id: str,
        device_name: str,
        model: str,
        thing_model: dict[str, Any],
        gateway_sn: str = "",
        version: str | None = None,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.model = model
        self.version = version
        self.gateway_sn = gateway_sn

        # Full thing model dict (from Java API)
        self._thing_model = thing_model

        # MQTT data store (flattened: data sub-object merged to top level)
        self._data: dict[str, Any] = {}

        # Entity callbacks
        self._callbacks: set[Callable[[set[str] | None], None]] = set()

        # Online state
        self._online = False
        self.last_data_time: float = 0.0

        # Extract routing field (default to deviceId for HZSZ)
        self.routing_field: str = thing_model.get("routingField", "deviceId")

        # Build property lookup from entity properties
        self._props: dict[str, dict[str, Any]] = {}
        self._entities: dict[str, dict[str, Any]] = {}
        self._entity_type_index: dict[str, list[dict[str, Any]]] = {}
        for entity in thing_model.get("entities", []):
            eid = entity.get("entityIdentifier", "")
            etype = entity.get("entityType", "")
            if eid:
                self._entities[eid] = entity
                self._entity_type_index.setdefault(etype, []).append(entity)
                for prop in entity.get("properties", []):
                    ident = prop.get("identifier", "")
                    if ident and ident not in self._props:
                        self._props[ident] = prop

    def refresh_thing_model(self, new_model: dict[str, Any]) -> bool:
        """Replace the thing model with a freshly fetched version.

        Rebuilds all internal indexes (_props, _entities, _entity_type_index)
        so that entity names, units, device classes, etc. reflect the latest
        Java backend definitions.  Returns True if anything changed.
        """
        old_model_name = self._thing_model.get("modelName", "")
        new_model_name = new_model.get("modelName", "")

        old_entities = self._thing_model.get("entities", [])
        new_entities = new_model.get("entities", [])

        # Quick identity check
        if old_model_name == new_model_name and old_entities == new_entities:
            _LOGGER.debug(
                "%s (%s): thing model unchanged, skipping refresh",
                self.model, self.device_id,
            )
            return False

        self._thing_model = new_model
        self.routing_field = new_model.get("routingField", "deviceId")

        # Rebuild indexes
        self._props.clear()
        self._entities.clear()
        self._entity_type_index.clear()
        for entity in new_entities:
            eid = entity.get("entityIdentifier", "")
            etype = entity.get("entityType", "")
            if eid:
                self._entities[eid] = entity
                self._entity_type_index.setdefault(etype, []).append(entity)
                for prop in entity.get("properties", []):
                    ident = prop.get("identifier", "")
                    if ident and ident not in self._props:
                        self._props[ident] = prop

        _LOGGER.info(
            "%s (%s): thing model refreshed — modelName: %r → %r, entities: %d → %d",
            self.model, self.device_id,
            old_model_name, new_model_name,
            len(old_entities), len(new_entities),
        )
        self._publish()
        return True

    # ------ online state ------

    @property
    def online(self) -> bool:
        return self._online

    def mark_online(self) -> None:
        self._online = True
        self.last_data_time = time.monotonic()
        self._publish()

    def mark_offline(self) -> None:
        self._online = False
        self._publish()

    def update_from_mqtt(self, payload: dict[str, Any]) -> None:
        """Handle incoming MQTT data for this device.

        HZSZ devices nest their sensor values under a ``data`` sub-object.
        We flatten that sub-object into the top level so property lookups
        (which use the identifier directly) work correctly.

        Only the fields present in the payload are overwritten; previous
        values for missing fields are preserved. After updating, entities
        whose identifiers appear in the payload are notified.
        """
        # Flatten: merge nested 'data' object to top level
        if "data" in payload and isinstance(payload["data"], dict):
            payload = {**payload, **payload["data"]}

        changed_fields = set(payload.keys()) - METADATA_KEYS
        self._data.update(payload)
        self.last_data_time = time.monotonic()
        was_offline = not self._online
        self._online = True
        _LOGGER.debug(
            "%s (%s) got MQTT data: %s (changed fields: %s)",
            self.model, self.device_id, list(payload.keys()), sorted(changed_fields),
        )
        if was_offline:
            _LOGGER.info("%s (%s) back online via uplink", self.model, self.device_id)
        self._publish(changed_fields)

    # ------ data access ------

    def get(self, field: str, default: Any = None) -> Any:
        """Get a stored MQTT value, or default if not yet received."""
        return self._data.get(field, default)

    def get_property(self, identifier: str) -> dict[str, Any] | None:
        """Get the thing model property definition for an identifier."""
        return self._props.get(identifier)

    @property
    def manufacturer(self) -> str:
        return self._thing_model.get("manufacturer") or "HZSZ"

    @property
    def sw_version(self) -> str:
        base = self._thing_model.get("protocol") or "Bluetooth"
        if self.version:
            return f"{base} / {self.version}"
        return base

    # ------ ★ v2.0 entity access ------

    def get_entities(self) -> dict[str, dict[str, Any]]:
        return dict(self._entities)

    def get_entities_by_type(self, entity_type: str) -> list[dict[str, Any]]:
        return self._entity_type_index.get(entity_type, [])

    # ------ entity callbacks ------

    def register_callback(self, callback: Callable[[set[str] | None], None]) -> None:
        self._callbacks.add(callback)

    def remove_callback(self, callback: Callable[[set[str] | None], None]) -> None:
        self._callbacks.discard(callback)

    def _publish(self, changed_fields: set[str] | None = None) -> None:
        for cb in self._callbacks:
            try:
                cb(changed_fields)
            except Exception:
                _LOGGER.exception("Callback error for %s (%s)", self.model, self.device_id)


# ---------------------------------------------------------------------------
#  Hub
# ---------------------------------------------------------------------------


class DeviceHub:
    """Manages all HZSZ Bluetooth devices.

    All devices are registered dynamically via MQTT Register or UplinkData.
    There is no static hardcoded device list.
    """

    manufacturer = "HZSZ"
    sw_version = "Bluetooth"

    def __init__(self, hass: HomeAssistant, offline_minutes: int = 10) -> None:
        self._hass = hass
        self._devices: dict[str, DynamicDevice] = {}
        self._device_id_index: dict[str, str] = {}  # deviceId → dict key
        self._offline_seconds = offline_minutes * 60

    # ---- device lookup ----

    def get_device(self, device_id: str) -> DynamicDevice | None:
        if device_id in self._devices:
            return self._devices[device_id]
        key = self._device_id_index.get(device_id)
        if key:
            return self._devices.get(key)
        return None

    def all_devices(self) -> list[DynamicDevice]:
        return list(self._devices.values())

    # ---- static pre-registration (called from setup) ----

    def setup_static_devices(
        self,
        known: dict[str, dict[str, str]],
        thing_model_api: ThingModelApi,
    ) -> None:
        """Pre-create static devices from KNOWN_DEVICES config.

        KNOWN_DEVICES is now intentionally empty; all devices are discovered
        dynamically via MQTT. This method is kept for compatibility and
        future use.
        """
        for device_id, info in known.items():
            model = info["model"]
            name = info["name"]
            version = info.get("version")

            tm = thing_model_api.get_cached(model, version=version)
            if tm is None:
                _LOGGER.warning(
                    "Thing model for %s (version=%s) not cached — "
                    "static device %s (%s) will have no entities",
                    model, version or "<default>", device_id, name,
                )

            device = DynamicDevice(
                device_id=device_id,
                device_name=name,
                model=model,
                thing_model=tm or {},
                gateway_sn="",
                version=version,
            )
            self._devices[device_id] = device
            _LOGGER.info(
                "Created static device %s (version=%s, %s) — %s, %d properties",
                model, version or "<default>", device_id, name,
                len(tm.get("properties", [])) if tm else 0,
            )

    def _remove_device(self, key: str) -> DynamicDevice | None:
        """Remove a device from hub indexes by its internal key."""
        device = self._devices.pop(key, None)
        if device is None:
            return None
        # Only drop the device_id index if it still points to this key
        if self._device_id_index.get(device.device_id) == key:
            self._device_id_index.pop(device.device_id, None)
        _LOGGER.info(
            "Removed cached device %s (model=%s) to allow re-registration",
            key, device.model,
        )
        return device

    # ---- dynamic registration (from /register topic or UplinkData) ----

    def register_device(
        self,
        gateway_sn: str,
        device_id: str,
        model: str,
        device_name: str,
        thing_model: dict[str, Any],
        notify: bool = True,
        version: str | None = None,
    ) -> DynamicDevice:
        """Register a device from Register topic or UplinkData, using its thing model.

        If a device with the same gateway/deviceId already exists but its model
        has changed, the old device is removed and a fresh device is created so
        that entities, names and the cache reflect the new thing model.
        """
        # For HZSZ, the key is gateway_sn__device_id (device_id is unique per gateway)
        key = _make_device_key(gateway_sn, device_id)

        # If model changed for the device under the same gateway key, remove it.
        existing = self._devices.get(key)
        if existing is not None and existing.model != model:
            _LOGGER.info(
                "Device %s model changed: %s -> %s; removing old registration",
                key, existing.model, model,
            )
            self._remove_device(key)
            existing = None

        # If model changed for the same deviceId under a different gateway key, remove it.
        existing_by_id = self.get_device(device_id)
        if existing_by_id is not None and existing_by_id.model != model:
            for _k, _v in list(self._devices.items()):
                if _v is existing_by_id:
                    _LOGGER.info(
                        "Device deviceId=%s model changed: %s -> %s; removing old registration '%s'",
                        device_id, existing_by_id.model, model, _k,
                    )
                    self._remove_device(_k)
                    break
            existing_by_id = None

        if existing is not None:
            # Re-register — update metadata AND refresh thing model
            existing.device_id = device_id
            existing.version = version
            existing.device_name = device_name
            if device_id not in self._device_id_index:
                self._device_id_index[device_id] = key
            # Apply updated thing model (may contain changed entity names etc.)
            if thing_model:
                existing.refresh_thing_model(thing_model)
            existing.mark_online()
            _LOGGER.info(
                "Device re-registered: %s (%s, version=%s, deviceId=%s)",
                key, model, version or "<default>", device_id,
            )
            return existing

        # Avoid duplicate: check if device with same deviceId already exists
        if existing_by_id is not None:
            for _k, _v in self._devices.items():
                if _v is existing_by_id:
                    self._device_id_index[device_id] = _k
                    break
            existing_by_id.gateway_sn = gateway_sn
            existing_by_id.device_name = device_name

            # Refresh thing model if a non-empty one is provided (covers both
            # first-time population and subsequent updates from the Java backend)
            if thing_model:
                changed = existing_by_id.refresh_thing_model(thing_model)
                if changed and notify:
                    async_dispatcher_send(self._hass, SIGNAL_NEW_DEVICE, existing_by_id)

            existing_by_id.mark_online()
            _LOGGER.info(
                "Device deviceId=%s already registered as '%s' — updated, not duplicated",
                device_id, _k,
            )
            return existing_by_id

        # Brand-new device
        device = DynamicDevice(
            device_id=device_id,
            device_name=device_name,
            model=model,
            thing_model=thing_model,
            gateway_sn=gateway_sn,
            version=version,
        )
        self._devices[key] = device
        self._device_id_index[device_id] = key

        _LOGGER.info(
            "New device: %s (%s, version=%s, deviceId=%s) — %d properties",
            key, model, version or "<default>", device_id,
            sum(len(e.get("properties", [])) for e in thing_model.get("entities", [])),
        )

        if notify:
            self._hass.async_create_background_task(
                self._save_cache(), f"{DOMAIN}_save_cache"
            )
            async_dispatcher_send(self._hass, SIGNAL_NEW_DEVICE, device)

        return device

    # ---- offline timeout ----

    def check_offline_devices(self) -> None:
        now = time.monotonic()
        for device in self._devices.values():
            if not device._online:
                continue
            if device.last_data_time <= 0:
                continue
            if now - device.last_data_time > self._offline_seconds:
                _LOGGER.info(
                    "%s (%s) timed out — marking offline",
                    device.model, device.device_name,
                )
                device.mark_offline()

    # ---- cache ----

    async def load_cache(self, thing_model_api: ThingModelApi | None = None) -> None:
        """Load previously registered dynamic devices from disk."""
        path = self._cache_path()

        def _read():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError):
                return None

        data = await self._hass.async_add_executor_job(_read)
        if data is None:
            return

        devices = data.get("devices", []) if isinstance(data, dict) else []
        for item in devices:
            try:
                model = item["model"]
                device_id = item["device_id"]
                gateway_sn = item["gateway_sn"]
                version = item.get("version")

                # Reconstruct device_name using current format: model_gatewaySN_deviceId
                device_name = f"{model}_{gateway_sn}_{device_id[-6:]}" if len(device_id) > 6 else f"{model}_{gateway_sn}_{device_id}"

                tm: dict[str, Any] = {}
                if thing_model_api is not None:
                    fetched = await thing_model_api.async_fetch_thing_model(
                        model, version=version
                    )
                    if fetched is not None:
                        tm = fetched
                    else:
                        _LOGGER.warning(
                            "Failed to fetch thing model for cached device %s (%s, version=%s) — "
                            "will retry on next UplinkData",
                            device_id, model, version or "<default>",
                        )

                self.register_device(
                    gateway_sn=gateway_sn,
                    device_id=device_id,
                    model=model,
                    device_name=device_name,
                    thing_model=tm,
                    notify=False,
                    version=version,
                )
            except (ValueError, KeyError) as exc:
                _LOGGER.warning("Skipping cached device %s: %s", item, exc)

        _LOGGER.info("Loaded %d devices from cache", len(devices))

    async def _save_cache(self) -> None:
        """Persist dynamically-registered devices to disk."""
        dynamic_devices = [
            {
                "key": key,
                "gateway_sn": dev.gateway_sn,
                "device_id": dev.device_id,
                "model": dev.model,
                "version": dev.version,
                "device_name": dev.device_name,
            }
            for key, dev in self._devices.items()
            if dev.gateway_sn
        ]

        path = self._cache_path()

        def _write():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"devices": dynamic_devices}, fh, indent=2)

        await self._hass.async_add_executor_job(_write)
        _LOGGER.debug("Cache saved: %d devices", len(dynamic_devices))

    def _cache_path(self) -> str:
        return self._hass.config.path(CACHE_DIR, CACHE_FILE)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _make_device_key(gateway_sn: str, device_id: str) -> str:
    return f"{gateway_sn}__{device_id}"
