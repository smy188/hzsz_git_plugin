"""Lock entities for Milesight IoT devices — v2.0 entity-centric.

A lock entity reads state (locked/unlocked) and sends lock/unlock commands via MQTT.
Properties with role=value are bound to the lock state.
"""

from __future__ import annotations

import logging

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[LockEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_locks(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_locks(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_locks(device: DynamicDevice) -> list["PushedLock"]:
    result: list[PushedLock] = []
    for entity_def in device.get_entities_by_type("lock"):
        props = entity_def.get("properties", [])
        if props:
            result.append(PushedLock(device, entity_def, props[0]))
    return result


class PushedLock(LockEntity):
    should_poll = False

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
        self._payload_lock = ctrl.get("payloadOn", "lock")
        self._payload_unlock = ctrl.get("payloadOff", "unlock")
        self._state_locked = ctrl.get("stateOn", "locked")
        self._state_unlocked = ctrl.get("stateOff", "unlocked")

        icon = prop.get("icon")
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
        return {"identifiers": {(DOMAIN, self._device.device_id)}, "name": self._device.device_name, "manufacturer": self._device.manufacturer, "model": self._device.model, "sw_version": self._device.sw_version}

    @property
    def available(self) -> bool:
        return self._device.online

    @property
    def is_locked(self) -> bool | None:
        raw = self._device.get(self._attr_identifier)
        if raw is None:
            return None
        return str(raw).lower() == str(self._state_locked).lower()

    async def async_lock(self, **kwargs) -> None:
        await self._publish(self._payload_lock)

    async def async_unlock(self, **kwargs) -> None:
        await self._publish(self._payload_unlock)

    async def _publish(self, payload: str) -> None:
        from .mqtt_client import MQTTHandler
        if not self._command_topic:
            _LOGGER.warning("Lock %s has no command topic", self._attr_name)
            return
        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if handler:
            await self.hass.async_add_executor_job(lambda: handler._client.publish(self._command_topic, payload, qos=2))
