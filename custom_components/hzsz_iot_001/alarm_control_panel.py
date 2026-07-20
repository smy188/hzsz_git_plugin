"""Alarm control panel entities for Milesight IoT — v2.0 entity-centric.

An alarm panel aggregates properties with roles:
  state → arm/disarm/trigger state
Properties bind to the panel state machine.
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HzszConfigEntry
from .const import DOMAIN
from .hub import SIGNAL_NEW_DEVICE, DynamicDevice

_LOGGER = logging.getLogger(__name__)

_STATE_MAP: dict[str, AlarmControlPanelState] = {
    "disarmed": AlarmControlPanelState.DISARMED,
    "armed_home": AlarmControlPanelState.ARMED_HOME,
    "armed_away": AlarmControlPanelState.ARMED_AWAY,
    "armed_night": AlarmControlPanelState.ARMED_NIGHT,
    "triggered": AlarmControlPanelState.TRIGGERED,
    "pending": AlarmControlPanelState.PENDING,
    "arming": AlarmControlPanelState.ARMING,
}


async def async_setup_entry(hass: HomeAssistant, config_entry: HzszConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = config_entry.runtime_data
    entities: list[AlarmControlPanelEntity] = []
    for device in hub.all_devices():
        entities.extend(_build_alarms(device))
    async_add_entities(entities)

    @callback
    def _on_new_device(device: DynamicDevice) -> None:
        new_entities = _build_alarms(device)
        if new_entities:
            async_add_entities(new_entities)
    config_entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _on_new_device))


def _build_alarms(device: DynamicDevice) -> list["PushedAlarm"]:
    result: list[PushedAlarm] = []
    for entity_def in device.get_entities_by_type("alarm_control_panel"):
        result.append(PushedAlarm(device, entity_def))
    return result


class PushedAlarm(AlarmControlPanelEntity):
    should_poll = False

    def __init__(self, device: DynamicDevice, entity_def: dict) -> None:
        self._device = device
        eid = entity_def.get("entityIdentifier", "")
        self._attr_unique_id = f"{device.device_id}_{eid}"
        self._attr_name = f"{device.device_name} {entity_def.get('entityName', eid)}"
        self._gateway_id: str = device.gateway_sn

        config = entity_def.get("entityConfig") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, TypeError):
                config = {}

        supported_states = config.get("supportedStates", ["disarmed", "armed_away", "armed_home"])
        self._armed_states = [s for s in supported_states if s.startswith("armed_")]

        # Map properties by role
        self._props: dict[str, dict] = {}
        for prop in entity_def.get("properties", []):
            role = prop.get("role", "value")
            self._props[role] = prop

        # State property
        self._state_prop = self._props.get("value") or self._props.get("state")
        self._attr_identifier = ""
        if self._state_prop:
            self._attr_identifier = self._state_prop.get("identifier", "")
            ctrl = self._state_prop.get("control") or {}
            self._command_topic = (ctrl.get("commandTopic") or "").replace("${gatewayId}", self._gateway_id)

        # Features
        self._attr_supported_features = (
            AlarmControlPanelEntityFeature.ARM_HOME
            | AlarmControlPanelEntityFeature.ARM_AWAY
            | AlarmControlPanelEntityFeature.TRIGGER
        )

        icon = config.get("icon") or "mdi:shield-home"
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
    def state(self) -> AlarmControlPanelState | None:
        if not self._state_prop:
            return None
        raw = self._device.get(self._state_prop.get("identifier", ""))
        if raw is None:
            return None
        state_str = str(raw).lower()
        return _STATE_MAP.get(state_str)

    async def _publish_cmd(self, cmd: str) -> None:
        from .mqtt_client import MQTTHandler
        if not self._command_topic:
            return
        payload = json.dumps({"cmd": cmd})
        instances = self.hass.data.get("hzsz_iot_001_instances", {}) if self.hass else {}
        handler: MQTTHandler | None = next(iter(instances.values()), None)
        if handler:
            await self.hass.async_add_executor_job(lambda: handler._client.publish(self._command_topic, payload, qos=2))

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._publish_cmd("disarm")

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._publish_cmd("arm_home")

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._publish_cmd("arm_away")

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._publish_cmd("arm_night")

    async def async_alarm_trigger(self, code: str | None = None) -> None:
        await self._publish_cmd("trigger")
