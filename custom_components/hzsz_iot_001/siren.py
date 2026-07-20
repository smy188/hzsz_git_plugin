"""Siren entities for Milesight IoT devices — v2.0 entity-centric.

A siren entity supports turning on/off an alarm sound via MQTT.
"""

from __future__ import annotations

import logging

from homeassistant.components.siren import SirenEntity, SirenEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[SirenEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_sirens(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_sirens(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_sirens(device: DynamicDevice) -> list["PushedSiren"]:
    result: list[PushedSiren] = []
    for entity_def in device.get_entities_by_type("siren"):
        props = entity_def.get("properties", [])
        if props:
            result.append(PushedSiren(device, entity_def, props[0]))
    return result


class PushedSiren(SirenEntity):
    should_poll = False
    _attr_supported_features = SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF

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
        self._payload_on = ctrl.get("payloadOn", "ON")
        self._payload_off = ctrl.get("payloadOff", "OFF")
        self._state_on = ctrl.get("stateOn", "ON")

        icon = prop.get("icon") or "mdi:alarm-bell"
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
    def is_on(self) -> bool:
        raw = self._device.get(self._attr_identifier)
        if raw is not None:
            return str(raw).upper() == str(self._state_on).upper()
        return False

    async def async_turn_on(self, **kwargs) -> None:
        await self._publish(self._payload_on)

    async def async_turn_off(self, **kwargs) -> None:
        await self._publish(self._payload_off)

    async def _publish(self, payload: str) -> None:
        from .mqtt_client import MQTTHandler
        if not self._command_topic:
            _LOGGER.warning("Siren %s has no command topic", self._attr_name)
            return
        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if handler:
            await self.hass.async_add_executor_job(lambda: handler._client.publish(self._command_topic, payload, qos=2))
