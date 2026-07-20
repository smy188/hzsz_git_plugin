"""Sensor entities for HZSZ_IOT_001 devices — v2.0 entity-centric."""

from __future__ import annotations

import json
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)

_UNIT_REQUIRED_DEVICE_CLASSES = {
    SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
    SensorDeviceClass.HUMIDITY,
    SensorDeviceClass.TEMPERATURE,
    SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    SensorDeviceClass.CO2,
    SensorDeviceClass.BATTERY,
    SensorDeviceClass.PRESSURE,
}


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
    entities: list[SensorEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_sensors(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_sensors(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_sensors(device: DynamicDevice) -> list["PushedSensor"]:
    result: list[PushedSensor] = []
    for entity_def in device.get_entities_by_type("sensor"):
        props = entity_def.get("properties", [])
        if props:
            result.append(PushedSensor(device, entity_def, props[0]))
    # Always append a synthetic diagnostic sensor for the gateway SN
    result.append(GatewaySnSensor(device))
    result.append(DeviceIdSensor(device))
    return result


class PushedSensor(SensorEntity):
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
        unit = config.get("unit") or prop.get("unit")
        if dc:
            try:
                device_class = SensorDeviceClass(dc)
            except ValueError:
                device_class = None
            if device_class is not None and not unit and device_class in _UNIT_REQUIRED_DEVICE_CLASSES:
                device_class = None
            self._attr_device_class = device_class
        if unit:
            self._attr_native_unit_of_measurement = unit

        sc = config.get("stateClass") or prop.get("stateClass")
        if sc:
            try:
                self._attr_state_class = SensorStateClass(sc)
            except ValueError:
                pass

        ec = config.get("entityCategory") or prop.get("entityCategory")
        if ec == "diagnostic":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        elif ec == "config":
            self._attr_entity_category = EntityCategory.CONFIG

        icon = config.get("icon") or prop.get("icon")
        if icon:
            self._attr_icon = icon

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
    def native_value(self):
        raw = self._device.get(self._attr_identifier)
        prop = self._device.get_property(self._attr_identifier)
        if raw is None:
            # Fallback to configured default value from Java thing model
            if prop:
                default = prop.get("defaultValue")
                if default is not None:
                    return self._cast_value(default, prop)
            return None
        return self._cast_value(raw, prop)

    @staticmethod
    def _cast_value(value, prop: dict | None):
        """Cast value according to the property's dataType."""
        data_type = prop.get("dataType", "") if prop else ""
        if data_type == "float":
            try:
                return float(value)
            except (ValueError, TypeError):
                return value
        if data_type in ("int", "bool"):
            try:
                return int(value)
            except (ValueError, TypeError):
                return value
        return value


class GatewaySnSensor(SensorEntity):
    """Synthetic diagnostic sensor showing the gateway SN for a device."""

    should_poll = False

    def __init__(self, device: DynamicDevice) -> None:
        self._device = device
        gw_sn = device.gateway_sn or "-"
        self._attr_unique_id = f"{device.device_id}_gateway_sn"
        self._attr_name = f"{device.device_name} 网关SN"
        self._attr_native_value = gw_sn
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:router-network"

    async def async_added_to_hass(self) -> None:
        self._device.register_callback(self._on_device_update)

    async def async_will_remove_from_hass(self) -> None:
        self._device.remove_callback(self._on_device_update)

    @callback
    def _on_device_update(self, changed_fields: set[str] | None = None) -> None:
        # Gateway SN does not come from field reports; update only on device-level events.
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


class DeviceIdSensor(SensorEntity):
    """Synthetic diagnostic sensor showing the device ID for a device."""

    should_poll = False

    def __init__(self, device: DynamicDevice) -> None:
        self._device = device
        self._attr_unique_id = f"{device.device_id}_device_id"
        self._attr_name = f"{device.device_name} 设备ID"
        self._attr_native_value = device.device_id
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:barcode"

    async def async_added_to_hass(self) -> None:
        self._device.register_callback(self._on_device_update)

    async def async_will_remove_from_hass(self) -> None:
        self._device.remove_callback(self._on_device_update)

    @callback
    def _on_device_update(self, changed_fields: set[str] | None = None) -> None:
        # Device ID does not come from field reports; update only on device-level events.
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
