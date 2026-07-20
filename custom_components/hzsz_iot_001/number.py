"""Number entities for HZSZ_IOT_001 devices — v2.0 entity-centric.

Each entityType="number" in the thing model entities list becomes a PushedNumber.
Reads from MQTT data, writes commands back via MQTT.

Values for min/max/step are read from the Java property fields first,
falling back to entityConfig for backward compatibility.
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.number import NumberEntity, NumberDeviceClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[NumberEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_numbers(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_numbers(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_numbers(device: DynamicDevice) -> list["PushedNumber"]:
    """Build number entities from the device's thing model entities."""
    result: list[PushedNumber] = []
    for entity_def in device.get_entities_by_type("number"):
        result.append(PushedNumber(device, entity_def))
    return result


class PushedNumber(NumberEntity):
    should_poll = False
    _attr_native_min_value: float = 0
    _attr_native_max_value: float = 100
    _attr_native_step: float = 1

    def __init__(self, device: DynamicDevice, entity_def: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"

        config = entity_def.get("entityConfig") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, TypeError):
                config = {}

        # Bind to the first property
        props = entity_def.get("properties", [])
        self._prop = props[0] if props else {}
        self._attr_identifier = self._prop.get("identifier", eid)
        self._access_mode = self._prop.get("accessMode", "rw")
        self._default_value = self._prop.get("defaultValue")

        dc = config.get("deviceClass")
        if dc:
            try:
                self._attr_device_class = NumberDeviceClass(dc)
            except ValueError:
                pass
        if config.get("unit"):
            self._attr_native_unit_of_measurement = config["unit"]

        # Min/max/step: Java property fields first, entityConfig as fallback
        min_v = self._prop.get("minValue") or config.get("min")
        if min_v is not None:
            self._attr_native_min_value = float(min_v)
        max_v = self._prop.get("maxValue") or config.get("max")
        if max_v is not None:
            self._attr_native_max_value = float(max_v)
        step_v = self._prop.get("stepValue") or config.get("step")
        if step_v is not None:
            self._attr_native_step = float(step_v)

        icon = config.get("icon")
        if icon:
            self._attr_icon = icon

        self._command_topic: str | None = None
        self._command_template: str | None = None
        self._gateway_sn: str = device.gateway_sn
        ctrl = self._prop.get("control") or {}
        if ctrl:
            self._command_topic = ctrl.get("commandTopic") or ""
            self._command_template = ctrl.get("commandTemplate") or ""

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
        return {"identifiers": {(DOMAIN, self._device.device_id)}, "name": self._device.device_name, "manufacturer": self._device.manufacturer, "model": self._device.model, "sw_version": self._device.sw_version}

    @property
    def available(self) -> bool:
        return self._device.online

    @property
    def native_value(self) -> float | None:
        raw = self._device.get(self._attr_identifier)
        if raw is None:
            if self._default_value is not None:
                try:
                    return float(self._default_value)
                except (ValueError, TypeError):
                    return None
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Publish value change via MQTT."""
        from .mqtt_client import MQTTHandler

        if self._access_mode != "rw":
            _LOGGER.warning("Number %s is read-only (accessMode=%s), ignoring set_value", self._attr_name, self._access_mode)
            return

        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if not handler:
            return

        val_str = str(int(value) if isinstance(value, float) and value == int(value) else value)

        if self._command_template:
            cmd_topic = self._command_topic.replace("${gatewayId}", self._gateway_sn) if self._command_topic else ""
            payload = self._command_template.replace("{{ value }}", val_str)
            payload = payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)
            if cmd_topic:
                await self.hass.async_add_executor_job(lambda: handler._client.publish(cmd_topic, payload, qos=2))
                _LOGGER.info("Set number %s to %s → topic=%s", self._attr_name, value, cmd_topic)
        elif self._command_topic:
            cmd_topic = self._command_topic.replace("${gatewayId}", self._gateway_sn)
            await self.hass.async_add_executor_job(lambda: handler._client.publish(cmd_topic, val_str, qos=2))
            _LOGGER.info("Set number %s to %s → topic=%s", self._attr_name, value, cmd_topic)
        else:
            handler.publish_command(self._gateway_sn, self._device.device_id, val_str)
            _LOGGER.info("Set number %s to %s via publish_command", self._attr_name, value)
