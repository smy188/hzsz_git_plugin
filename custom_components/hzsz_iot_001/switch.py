"""Switch entities for HZSZ_IOT_001 devices — v2.0 entity-centric.

Reads state from MQTT data, sends commands via MQTT to /hzsz/gateway/{gatewaySn}/cmd.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[SwitchEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_switches(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_switches(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_switches(device: DynamicDevice) -> list["PushedSwitch"]:
    """Build switch entities from thing model entities."""
    result: list[PushedSwitch] = []
    for entity_def in device.get_entities_by_type("switch"):
        props = entity_def.get("properties", [])
        if props:
            result.append(PushedSwitch(device, entity_def, props[0]))
    return result


class PushedSwitch(SwitchEntity):
    should_poll = False

    def __init__(self, device: DynamicDevice, entity_def: dict, prop: dict) -> None:
        self._device = device
        ident = prop.get("identifier", "")
        eid = entity_def.get("entityIdentifier", ident)
        name = entity_def.get("entityName", prop.get("name", ident))
        self._attr_identifier = ident
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {name}"

        ctrl = prop.get("control") or {}
        self._command_template = ctrl.get("commandTemplate") or ""
        self._command_topic = (ctrl.get("commandTopic") or "").replace("${gatewayId}", device.gateway_sn)
        self._payload_on = ctrl.get("payloadOn", "1")
        self._payload_off = ctrl.get("payloadOff", "0")
        self._state_on = ctrl.get("stateOn", "1")
        self._state_off = ctrl.get("stateOff", "0")
        self._access_mode = prop.get("accessMode", "rw")
        self._default_value = prop.get("defaultValue")

        dc = prop.get("deviceClass")
        if dc:
            try:
                self._attr_device_class = SwitchDeviceClass(dc)
            except ValueError:
                pass
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
    def is_on(self) -> bool:
        raw = self._device.get(self._attr_identifier)
        if raw is None:
            if self._default_value is not None:
                return str(self._default_value) == str(self._state_on)
            return False
        return str(raw) == str(self._state_on) or raw is True or raw == 1

    async def async_turn_on(self, **kwargs) -> None:
        if self._access_mode != "rw":
            _LOGGER.warning("Switch %s is read-only (accessMode=%s), ignoring turn_on", self._attr_name, self._access_mode)
            return
        await self._publish(self._payload_on)

    async def async_turn_off(self, **kwargs) -> None:
        if self._access_mode != "rw":
            _LOGGER.warning("Switch %s is read-only (accessMode=%s), ignoring turn_off", self._attr_name, self._access_mode)
            return
        await self._publish(self._payload_off)

    async def _publish(self, payload: str) -> None:
        from .mqtt_client import MQTTHandler

        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if not handler:
            return

        # Use commandTemplate if available (fully Java-driven payload)
        if self._command_template:
            cmd_payload = self._command_template.replace("{{ value }}", payload)
            cmd_payload = cmd_payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)
            if self._command_topic:
                await self.hass.async_add_executor_job(lambda: handler._client.publish(self._command_topic, cmd_payload, qos=2))
        elif self._command_topic:
            # Fallback: direct payload publish with placeholder replacement
            payload = payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)
            await self.hass.async_add_executor_job(lambda: handler._client.publish(self._command_topic, payload, qos=2))
        else:
            # No command topic: use the convenience method (hzsz cmd format)
            handler.publish_command(self._device.gateway_sn, self._device.device_id, payload)
