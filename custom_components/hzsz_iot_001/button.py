"""Button entities for HZSZ_IOT_001 devices — v2.0 entity-centric."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[ButtonEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_buttons(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_buttons(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_buttons(device: DynamicDevice) -> list["PushedButton"]:
    result: list[PushedButton] = []
    for entity_def in device.get_entities_by_type("button"):
        result.append(PushedButton(device, entity_def))
    return result


class PushedButton(ButtonEntity):
    """Button that triggers a command via MQTT."""

    should_poll = False

    def __init__(self, device: DynamicDevice, entity_def: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"

        props = entity_def.get("properties", [])
        self._prop = props[0] if props else {}
        self._attr_identifier = self._prop.get("identifier", eid)
        self._gateway_sn: str = device.gateway_sn
        self._access_mode = self._prop.get("accessMode", "rw")
        ctrl = self._prop.get("control") or {}
        self._command_topic = ctrl.get("commandTopic") or ""
        self._command_template = ctrl.get("commandTemplate") or ""
        self._payload = ctrl.get("payloadOn") or ""

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

    async def async_press(self) -> None:
        from .mqtt_client import MQTTHandler

        if self._access_mode != "rw":
            _LOGGER.warning("Button %s is read-only (accessMode=%s), ignoring press", self._attr_name, self._access_mode)
            return

        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if not handler:
            return

        # Build payload: commandTemplate > payloadOn > raw "press"
        payload = self._command_template or self._payload or "press"
        payload = payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)

        if self._command_topic:
            cmd_topic = self._command_topic.replace("${gatewayId}", self._gateway_sn)
            await self.hass.async_add_executor_job(lambda: handler._client.publish(cmd_topic, payload, qos=2))
            _LOGGER.info("Button %s pressed → topic=%s", self._attr_name, cmd_topic)
        else:
            handler.publish_command(self._gateway_sn, self._device.device_id, payload)
            _LOGGER.info("Button %s pressed via publish_command", self._attr_name)
