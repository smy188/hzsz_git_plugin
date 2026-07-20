"""Select entities for HZSZ_IOT_001 devices — v2.0 entity-centric."""

from __future__ import annotations

import json
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[SelectEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_selects(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_selects(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_selects(device: DynamicDevice) -> list["PushedSelect"]:
    result: list[PushedSelect] = []
    for entity_def in device.get_entities_by_type("select"):
        result.append(PushedSelect(device, entity_def))
    return result


class PushedSelect(SelectEntity):
    should_poll = False

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
        icon = config.get("icon")
        if icon:
            self._attr_icon = icon

        # Parse options from the property's enumValues or entityConfig
        self._options_map: dict[str, str] = {}
        props = entity_def.get("properties", [])
        self._prop = props[0] if props else {}
        self._attr_identifier = self._prop.get("identifier", eid)
        self._command_topic: str | None = None
        self._command_template: str | None = None
        self._gateway_sn: str = device.gateway_sn

        enum_str = self._prop.get("enumValues", "") or ""
        if enum_str:
            try:
                enum_list = json.loads(enum_str)
                for item in enum_list:
                    if ":" in item:
                        k, v = item.split(":", 1)
                        self._options_map[k] = v
                    else:
                        self._options_map[item] = item
            except (json.JSONDecodeError, TypeError):
                pass

        ctrl = self._prop.get("control") or {}
        if ctrl:
            self._command_topic = ctrl.get("commandTopic") or ""
            self._command_template = ctrl.get("commandTemplate") or ""

        self._access_mode = self._prop.get("accessMode", "rw")
        self._default_value = self._prop.get("defaultValue")

    @property
    def options(self) -> list[str]:
        return list(self._options_map.keys())

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
    def current_option(self) -> str | None:
        raw = self._device.get(self._attr_identifier)
        if raw is not None:
            return str(raw)
        if self._default_value is not None:
            return str(self._default_value)
        return None

    async def async_select_option(self, option: str) -> None:
        from .mqtt_client import MQTTHandler

        if self._access_mode != "rw":
            _LOGGER.warning("Select %s is read-only (accessMode=%s), ignoring select_option", self._attr_name, self._access_mode)
            return

        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if not handler:
            return

        if self._command_template:
            payload = self._command_template.replace("{{ value }}", option)
            payload = payload.replace("${deviceId}", self._device.device_id).replace("${devEUI}", self._device.device_id)
            if self._command_topic:
                cmd_topic = self._command_topic.replace("${gatewayId}", self._gateway_sn)
                await self.hass.async_add_executor_job(lambda: handler._client.publish(cmd_topic, payload, qos=2))
        elif self._command_topic:
            cmd_topic = self._command_topic.replace("${gatewayId}", self._gateway_sn)
            await self.hass.async_add_executor_job(lambda: handler._client.publish(cmd_topic, option, qos=2))
        else:
            handler.publish_command(self._gateway_sn, self._device.device_id, option)
            _LOGGER.info("Set select %s to %s via publish_command", self._attr_name, option)
