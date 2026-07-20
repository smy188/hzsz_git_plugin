"""Water heater entities for Milesight IoT devices — v2.0 entity-centric.

A water heater aggregates properties with roles:
  power, current_temperature, target_temperature, operation_mode
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)

_OP_MODE_MAP = {
    "off": "off",
    "eco": "eco",
    "electric": "electric",
    "gas": "gas",
    "heat_pump": "heat_pump",
    "high_demand": "high_demand",
    "performance": "performance",
}


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[WaterHeaterEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_water_heaters(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_water_heaters(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_water_heaters(device: DynamicDevice) -> list["PushedWaterHeater"]:
    result: list[PushedWaterHeater] = []
    for entity_def in device.get_entities_by_type("water_heater"):
        result.append(PushedWaterHeater(device, entity_def))
    return result


class PushedWaterHeater(WaterHeaterEntity):
    should_poll = False

    def __init__(self, device: DynamicDevice, entity_def: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"
        self._gateway_id: str = device.gateway_sn

        config = entity_def.get("entityConfig") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, TypeError):
                config = {}

        self._props: dict[str, dict] = {}
        for prop in entity_def.get("properties", []):
            role = prop.get("role", "value")
            self._props[role] = prop

        self._identifiers: set[str] = {
            prop.get("identifier", "")
            for prop in entity_def.get("properties", [])
            if prop.get("identifier")
        }

        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_min_temp = float(config.get("minTemp", 20))
        self._attr_max_temp = float(config.get("maxTemp", 80))
        op_modes = config.get("operationModes", ["off", "eco", "electric"])
        self._attr_operation_list = op_modes

        self._attr_supported_features = (
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.OPERATION_MODE
        )
        if "power" in self._props:
            self._attr_supported_features |= WaterHeaterEntityFeature.ON_OFF

    def _get_raw(self, role: str) -> any:
        prop = self._props.get(role)
        if not prop:
            return None
        return self._device.get(prop.get("identifier", ""))

    def _publish_cmd(self, role: str, value: str) -> None:
        from .mqtt_client import MQTTHandler
        prop = self._props.get(role)
        if not prop:
            return
        ctrl = prop.get("control") or {}
        topic = (ctrl.get("commandTopic") or "").replace("${gatewayId}", self._gateway_id)
        template = ctrl.get("commandTemplate") or ""
        if not topic:
            return
        payload = template.replace("{{ value }}", value)
        payload = payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)
        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if handler:
            self.hass.async_add_executor_job(lambda: handler._client.publish(topic, payload, qos=2))

    async def async_added_to_hass(self) -> None:
        self._device.register_callback(self._on_device_update)

    async def async_will_remove_from_hass(self) -> None:
        self._device.remove_callback(self._on_device_update)

    @callback
    def _on_device_update(self, changed_fields: set[str] | None = None) -> None:
        if changed_fields is not None and not self._identifiers.intersection(changed_fields):
            return
        self.async_write_ha_state()

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._device.device_id)}, "name": self._device.device_name, "manufacturer": self._device.manufacturer, "model": self._device.model, "sw_version": self._device.sw_version}

    @property
    def available(self) -> bool:
        return self._device.online

    @property
    def current_temperature(self) -> float | None:
        raw = self._get_raw("current_temperature")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def target_temperature(self) -> float | None:
        raw = self._get_raw("target_temperature")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
        return None

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get("temperature")
        if temp is not None:
            self._publish_cmd("target_temperature", str(temp))

    @property
    def current_operation(self) -> str | None:
        raw = self._get_raw("operation_mode")
        return str(raw) if raw is not None else "off"

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        self._publish_cmd("operation_mode", operation_mode)
