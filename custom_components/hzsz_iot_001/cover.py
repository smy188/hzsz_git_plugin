"""Cover entities for HZSZ_IOT_001 devices — v2.0 entity-centric.

A cover entity aggregates properties with roles: position, tilt.
Sends commands via MQTT command_template.
"""

from __future__ import annotations

import logging

from homeassistant.components.cover import CoverEntity, CoverDeviceClass, CoverEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[CoverEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_covers(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_covers(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_covers(device: DynamicDevice) -> list["PushedCover"]:
    result: list[PushedCover] = []
    for entity_def in device.get_entities_by_type("cover"):
        result.append(PushedCover(device, entity_def))
    return result


class PushedCover(CoverEntity):
    should_poll = False

    def __init__(self, device: DynamicDevice, entity_def: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"

        dc = (entity_def.get("entityConfig") or {}).get("deviceClass", "") if isinstance(entity_def.get("entityConfig"), dict) else ""
        if dc:
            try:
                self._attr_device_class = CoverDeviceClass(dc)
            except ValueError:
                pass

        self._gateway_sn: str = device.gateway_sn
        self._props_by_role: dict[str, dict] = {}
        self._identifiers: set[str] = set()
        self._features: int = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        props = entity_def.get("properties", [])
        for prop in props:
            role = prop.get("role", "value")
            self._props_by_role[role] = prop
            ident = prop.get("identifier", "")
            if ident:
                self._identifiers.add(ident)

        if "position" in self._props_by_role:
            self._features |= CoverEntityFeature.SET_POSITION

    @property
    def _position_prop(self) -> dict | None:
        return self._props_by_role.get("position")

    @property
    def _cmd_topic(self) -> str:
        prop = self._position_prop
        if prop:
            ctrl = prop.get("control") or {}
            return (ctrl.get("commandTopic") or "").replace("${gatewayId}", self._gateway_sn)
        return ""

    @property
    def _cmd_template(self) -> str:
        prop = self._position_prop
        if prop:
            ctrl = prop.get("control") or {}
            return ctrl.get("commandTemplate") or ""
        return ""

    @property
    def supported_features(self) -> int:
        return self._features

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
    def current_cover_position(self) -> int | None:
        prop = self._position_prop
        if not prop:
            return None
        raw = self._device.get(prop.get("identifier", ""))
        if raw is not None:
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                pass
        # Fallback to defaultValue
        default = prop.get("defaultValue")
        if default is not None:
            try:
                return int(float(default))
            except (ValueError, TypeError):
                pass
        return None

    @property
    def is_closed(self) -> bool:
        pos = self.current_cover_position
        return pos == 0 if pos is not None else False

    @property
    def is_opening(self) -> bool:
        return False

    @property
    def is_closing(self) -> bool:
        return False

    async def async_open_cover(self, **kwargs) -> None:
        await self._publish_cmd("OPEN")

    async def async_close_cover(self, **kwargs) -> None:
        await self._publish_cmd("CLOSE")

    async def async_stop_cover(self, **kwargs) -> None:
        await self._publish_cmd("STOP")

    async def async_set_cover_position(self, **kwargs) -> None:
        pos = kwargs.get("position")
        if pos is not None:
            await self._publish_cmd(str(pos))

    async def _publish_cmd(self, value: str) -> None:
        from .mqtt_client import MQTTHandler

        topic = self._cmd_topic
        template = self._cmd_template

        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if not handler:
            return

        if template:
            payload = template.replace("{{ value }}", value)
            payload = payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)
            if topic:
                await self.hass.async_add_executor_job(lambda: handler._client.publish(topic, payload, qos=2))
                _LOGGER.info("Cover %s → %s topic=%s", self._attr_name, value, topic)
        elif topic:
            await self.hass.async_add_executor_job(lambda: handler._client.publish(topic, value, qos=2))
            _LOGGER.info("Cover %s → %s topic=%s", self._attr_name, value, topic)
        else:
            handler.publish_command(self._gateway_sn, self._device.device_id, value)
            _LOGGER.info("Cover %s → %s via publish_command", self._attr_name, value)
