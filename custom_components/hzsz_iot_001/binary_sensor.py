"""Binary sensor entities for HZSZ_IOT_001 devices — v2.0 entity-centric."""

from __future__ import annotations

import json
import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


def _parse_config(raw: str | dict | None) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_binary_sensors(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_binary_sensors(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_binary_sensors(device: DynamicDevice) -> list["PushedBinarySensor"]:
    result: list[PushedBinarySensor] = []
    for entity_def in device.get_entities_by_type("binary_sensor"):
        props = entity_def.get("properties", [])
        if props:
            result.append(PushedBinarySensor(device, entity_def, props[0]))
    return result


class PushedBinarySensor(BinarySensorEntity):
    should_poll = False

    def __init__(self, device: DynamicDevice, entity_def: dict, prop: dict) -> None:
        self._device = device
        ident = prop.get("identifier", "")
        eid = entity_def.get("entityIdentifier", ident)

        config = _parse_config(entity_def.get("entityConfig"))
        name = entity_def.get("entityName") or prop.get("name", ident)

        self._attr_identifier = ident
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {name}"

        dc = config.get("deviceClass") or prop.get("deviceClass")
        if dc:
            try:
                self._attr_device_class = BinarySensorDeviceClass(dc)
            except ValueError:
                pass

        icon = config.get("icon") or prop.get("icon")
        if icon:
            self._attr_icon = icon

        self._default_value = prop.get("defaultValue")

    async def async_added_to_hass(self) -> None:
        self._device.register_callback(self._on_device_update)

    async def async_will_remove_from_hass(self) -> None:
        self._device.remove_callback(self._on_device_update)

    @callback
    def _on_device_update(self, changed_fields: set[str] | None = None) -> None:
        if changed_fields is not None and self._attr_identifier not in changed_fields:
            return
        self.async_write_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device.device_id)},
            "name": self._device.device_name,
            "manufacturer": self._device.manufacturer,
            "model": self._device.model,
            "sw_version": self._device.sw_version,
        }

    @property
    def available(self) -> bool:
        return self._device.online

    @property
    def is_on(self) -> bool:
        raw = self._device.get(self._attr_identifier)
        if raw is None:
            if self._default_value is not None:
                return str(self._default_value).lower() in ("1", "true", "on")
            return False
        return raw == 1 or raw is True or str(raw).lower() in ("1", "true", "on")
