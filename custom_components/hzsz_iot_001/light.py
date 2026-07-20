"""Light entities for HZSZ_IOT_001 devices — v2.0 entity-centric.

Properties with roles: power, brightness, color_temp, rgb_color, effect.
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.light import (
    LightEntity,
    ColorMode,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[LightEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_lights(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_lights(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_lights(device: DynamicDevice) -> list["PushedLight"]:
    result: list[PushedLight] = []
    for entity_def in device.get_entities_by_type("light"):
        result.append(PushedLight(device, entity_def))
    return result


class PushedLight(LightEntity):
    should_poll = False

    def __init__(self, device: DynamicDevice, entity_def: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"
        self._gateway_sn: str = device.gateway_sn

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

        # Determine color modes
        color_modes = config.get("supportedColorModes", [])
        if not color_modes:
            # Infer from available roles
            if "color_temp" in self._props:
                color_modes.append("color_temp")
            if "rgb_color" in self._props:
                color_modes.append("rgb")
            if "brightness" in self._props:
                color_modes.append("brightness")
            if not color_modes:
                color_modes = ["onoff"]

        self._attr_supported_color_modes = {ColorMode(m) for m in color_modes}
        self._attr_min_mireds = config.get("minMireds", 153)
        self._attr_max_mireds = config.get("maxMireds", 500)

        if "effect" in self._props:
            self._attr_supported_features = LightEntityFeature.EFFECT
        else:
            self._attr_supported_features = LightEntityFeature(0)

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
        if prop.get("accessMode", "rw") != "rw":
            _LOGGER.warning("Light %s role=%s is read-only, ignoring command", self._attr_name, role)
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
    def is_on(self) -> bool:
        raw = self._get_raw("power")
        if raw is not None:
            return raw == 1 or raw is True or str(raw).lower() in ("1", "true", "on")
        return False

    async def async_turn_on(self, **kwargs) -> None:
        if "rgb_color" in self._props and kwargs.get("rgb_color"):
            r, g, b = kwargs["rgb_color"]
            self._publish_cmd("rgb_color", f"{r},{g},{b}")
        if "color_temp" in self._props and kwargs.get("color_temp"):
            self._publish_cmd("color_temp", str(kwargs["color_temp"]))
        if "brightness" in self._props and kwargs.get("brightness") is not None:
            self._publish_cmd("brightness", str(kwargs["brightness"]))
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

    async def async_turn_off(self, **kwargs) -> None:
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

    @property
    def brightness(self) -> int | None:
        raw = self._get_raw("brightness")
        if raw is not None:
            try:
                return max(0, min(255, int(float(raw))))
            except (ValueError, TypeError):
                pass
        return None

    @property
    def color_temp(self) -> int | None:
        raw = self._get_raw("color_temp")
        if raw is not None:
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                pass
        return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        raw = self._get_raw("rgb_color")
        if raw:
            try:
                if isinstance(raw, str):
                    parts = raw.split(",")
                    if len(parts) == 3:
                        return tuple(int(p) for p in parts)
                elif isinstance(raw, (list, tuple)) and len(raw) == 3:
                    return tuple(int(raw[0])), int(raw[1]), int(raw[2])
            except (ValueError, TypeError):
                pass
        return None

    @property
    def effect_list(self) -> list[str] | None:
        if "effect" in self._props:
            return ["effect_1", "effect_2"]
        return None

    @property
    def effect(self) -> str | None:
        raw = self._get_raw("effect")
        return str(raw) if raw is not None else None
