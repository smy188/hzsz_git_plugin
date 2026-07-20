"""Climate entities for HZSZ_IOT_001 devices — v2.0 entity-centric.

A single ClimateEntity aggregates multiple properties with roles:
  power, current_temperature, target_temperature, target_temperature_low,
  target_temperature_high, mode, fan_mode, swing_mode, preset_mode,
  current_humidity, target_humidity, action
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[ClimateEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_climates(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_climates(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_climates(device: DynamicDevice) -> list["PushedClimate"]:
    result: list[PushedClimate] = []
    for entity_def in device.get_entities_by_type("climate"):
        result.append(PushedClimate(device, entity_def))
    return result


def _parse_config(config: str | dict | None) -> dict:
    if config is None:
        return {}
    if isinstance(config, str):
        try:
            return json.loads(config)
        except (json.JSONDecodeError, TypeError):
            return {}
    return config


class PushedClimate(ClimateEntity):
    should_poll = False
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, device: DynamicDevice, entity_def: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"
        self._gateway_sn: str = device.gateway_sn

        # Parse entity config
        config = _parse_config(entity_def.get("entityConfig"))

        # Map properties by role
        self._props: dict[str, dict] = {}
        self._identifiers: set[str] = set()
        props = entity_def.get("properties", [])
        for prop in props:
            role = prop.get("role", "value")
            self._props[role] = prop
            ident = prop.get("identifier", "")
            if ident:
                self._identifiers.add(ident)

        # Temperature
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_min_temp = float(config.get("minTemp", 16))
        self._attr_max_temp = float(config.get("maxTemp", 30))
        self._attr_target_temperature_step = float(config.get("tempStep", 1))

        # HVAC modes (from entity config or default)
        hvac_modes = config.get("hvacModes", ["off", "cool", "heat", "auto"])
        self._attr_hvac_modes = [HVACMode(m) for m in hvac_modes if m in {e.value for e in HVACMode}]

        # Fan modes
        fan_modes = config.get("fanModes", [])
        if fan_modes:
            self._attr_fan_modes = fan_modes
        else:
            self._attr_fan_modes = None

        # Features
        self._attr_supported_features = ClimateEntityFeature(0)
        if "target_temperature" in self._props:
            self._attr_supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if "target_temperature_low" in self._props and "target_temperature_high" in self._props:
            self._attr_supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if self._attr_fan_modes:
            self._attr_supported_features |= ClimateEntityFeature.FAN_MODE
        if "power" in self._props:
            self._attr_supported_features |= ClimateEntityFeature.TURN_OFF
            self._attr_supported_features |= ClimateEntityFeature.TURN_ON

        # Icon
        icon = config.get("icon")
        if icon:
            self._attr_icon = icon

    # ---- helpers ----

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
        # Check accessMode
        if prop.get("accessMode", "rw") != "rw":
            _LOGGER.warning("Climate %s role=%s is read-only, ignoring command", self._attr_name, role)
            return
        ctrl = prop.get("control") or {}
        topic = (ctrl.get("commandTopic") or "").replace("${gatewayId}", self._gateway_sn)
        template = ctrl.get("commandTemplate") or ""

        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if not handler:
            return

        if template:
            payload = template.replace("{{ value }}", value)
            payload = payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)
            if topic:
                self.hass.async_add_executor_job(lambda: handler._client.publish(topic, payload, qos=2))
        elif topic:
            self.hass.async_add_executor_job(lambda: handler._client.publish(topic, value, qos=2))
        else:
            handler.publish_command(self._gateway_sn, self._device.device_id, value)

    # ---- lifecycle ----

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

    # ---- HVAC mode ----

    @property
    def hvac_mode(self) -> HVACMode | None:
        raw = self._get_raw("mode")
        if raw:
            try:
                return HVACMode(str(raw))
            except ValueError:
                pass
        return HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._publish_cmd("mode", hvac_mode.value)

    # ---- power ----

    async def async_turn_on(self) -> None:
        if "power" in self._props:
            ctrl = self._props["power"].get("control") or {}
            payload = ctrl.get("payloadOn", "1")
            topic = (ctrl.get("commandTopic") or "").replace("${gatewayId}", self._gateway_sn)
            if topic:
                from .mqtt_client import MQTTHandler
                instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
                handler: MQTTHandler | None = next(iter(instances.values()), None)
                if handler:
                    await self.hass.async_add_executor_job(lambda: handler._client.publish(topic, payload, qos=2))

    async def async_turn_off(self) -> None:
        if "power" in self._props:
            ctrl = self._props["power"].get("control") or {}
            payload = ctrl.get("payloadOff", "0")
            topic = (ctrl.get("commandTopic") or "").replace("${gatewayId}", self._gateway_sn)
            if topic:
                from .mqtt_client import MQTTHandler
                instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
                handler: MQTTHandler | None = next(iter(instances.values()), None)
                if handler:
                    await self.hass.async_add_executor_job(lambda: handler._client.publish(topic, payload, qos=2))

    # ---- temperature ----

    @property
    def current_temperature(self) -> float | None:
        raw = self._get_raw("current_temperature")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
        return self._get_default_float("current_temperature")

    @property
    def target_temperature(self) -> float | None:
        raw = self._get_raw("target_temperature")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
        return self._get_default_float("target_temperature")

    def _get_default_float(self, role: str) -> float | None:
        """Fallback to property defaultValue if no MQTT data."""
        prop = self._props.get(role)
        if prop and prop.get("defaultValue") is not None:
            try:
                return float(prop["defaultValue"])
            except (ValueError, TypeError):
                pass
        return None

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get("temperature")
        if temp is not None and "target_temperature" in self._props:
            self._publish_cmd("target_temperature", str(temp))
        low = kwargs.get("target_temp_low")
        if low is not None and "target_temperature_low" in self._props:
            self._publish_cmd("target_temperature_low", str(low))
        high = kwargs.get("target_temp_high")
        if high is not None and "target_temperature_high" in self._props:
            self._publish_cmd("target_temperature_high", str(high))

    # ---- fan mode ----

    @property
    def fan_mode(self) -> str | None:
        raw = self._get_raw("fan_mode")
        return str(raw) if raw is not None else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._publish_cmd("fan_mode", fan_mode)

    # ---- action ----

    @property
    def hvac_action(self) -> HVACAction | None:
        raw = self._get_raw("action")
        if raw:
            try:
                return HVACAction(str(raw))
            except ValueError:
                pass
        return None
