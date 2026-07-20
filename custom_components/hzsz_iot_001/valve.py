"""Valve entities for Milesight IoT devices — v2.0 entity-centric.

A valve entity controls water/gas valves with open/close/stop commands via MQTT.
"""

from __future__ import annotations

import logging

from homeassistant.components.valve import ValveEntity, ValveEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[ValveEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_valves(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_valves(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_valves(device: DynamicDevice) -> list["PushedValve"]:
    result: list[PushedValve] = []
    for entity_def in device.get_entities_by_type("valve"):
        props = entity_def.get("properties", [])
        if props:
            result.append(PushedValve(device, entity_def, props[0]))
    return result


class PushedValve(ValveEntity):
    should_poll = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(self, device: DynamicDevice, entity_def: dict, prop: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"
        self._gateway_id: str = device.gateway_sn

        ident = prop.get("identifier", eid)
        self._attr_identifier = ident
        ctrl = prop.get("control") or {}
        self._command_topic = (ctrl.get("commandTopic") or "").replace("${gatewayId}", self._gateway_id)
        self._payload_open = ctrl.get("payloadOn", "open")
        self._payload_close = ctrl.get("payloadOff", "close")
        self._state_open = ctrl.get("stateOn", "open")
        self._state_closed = ctrl.get("stateOff", "closed")

        icon = prop.get("icon") or "mdi:valve"
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
        return {"identifiers": {(DOMAIN, self._device.device_id)}, "name": self._device.device_name, "manufacturer": self._device.manufacturer, "model": self._device.model, "sw_version": self._device.sw_version}

    @property
    def available(self) -> bool:
        return self._device.online

    @property
    def is_opening(self) -> bool:
        return False

    @property
    def is_closing(self) -> bool:
        return False

    @property
    def is_closed(self) -> bool | None:
        raw = self._device.get(self._attr_identifier)
        if raw is None:
            return None
        return str(raw).lower() == str(self._state_closed).lower()

    @property
    def valve_position(self) -> int | None:
        raw = self._device.get(self._attr_identifier)
        if raw is not None:
            if str(raw).lower() == str(self._state_open).lower():
                return 100
            if str(raw).lower() == str(self._state_closed).lower():
                return 0
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                pass
        return None

    async def async_open_valve(self) -> None:
        await self._publish(self._payload_open)

    async def async_close_valve(self) -> None:
        await self._publish(self._payload_close)

    async def _publish(self, payload: str) -> None:
        from .mqtt_client import MQTTHandler
        if not self._command_topic:
            _LOGGER.warning("Valve %s has no command topic", self._attr_name)
            return
        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if handler:
            await self.hass.async_add_executor_job(lambda: handler._client.publish(self._command_topic, payload, qos=2))
