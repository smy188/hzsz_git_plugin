"""The HZSZ_IOT_001 Thing Model integration.

Fetches device definitions from the Java IoT backend at setup time.
All devices are registered dynamically via MQTT Register or
UplinkData — there is no static hardcoded device list.

Entities are created from the thing model properties — no hardcoded
device classes.

Key differences from milesight_iot:
  - MQTT topic prefix: hzsz/gateway/+/ (wildcard for gatewaySn)
  - Device routing field: deviceId (not devEUI)
  - MQTT data is nested under a ``data`` sub-object (flattened on arrival)
  - Device discovery via /Register topic (not JoinNotification)
  - Supports switch entities for device control (led_on/led_off via /cmd topic)
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DEFAULT_MQTT_BROKER,
    DEFAULT_MQTT_PASSWORD,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_USERNAME,
    KNOWN_DEVICES,
    OFFLINE_CHECK_INTERVAL,
)
from .hub import MODEL_PREFIXES, DeviceHub
from .mqtt_client import MQTTHandler
from .thing_model_api import ThingModelApi

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.FAN,
    Platform.COVER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.SIREN,
    Platform.VALVE,
    Platform.ALARM_CONTROL_PANEL,
    Platform.HUMIDIFIER,
    Platform.WATER_HEATER,
]

type HzszConfigEntry = ConfigEntry[DeviceHub]

# Time interval for offline device check
OFFLINE_CHECK = timedelta(seconds=OFFLINE_CHECK_INTERVAL)


async def async_setup_entry(
    hass: HomeAssistant, entry: HzszConfigEntry
) -> bool:
    """Set up HZSZ_IOT_001 Thing Model from a config entry."""
    hub = DeviceHub(hass)

    # 1. Fetch thing models for all supported models (warm cache).
    #    version=None means fetching the default thing model for each model.
    #    Model list is fetched dynamically from the Java API; if unreachable,
    #    devices will still be discovered via MQTT Register/UplinkData and
    #    their thing models fetched on-demand.
    #
    #    A fresh ThingModelApi instance starts with an empty in-memory cache,
    #    so a HA restart always re-fetches from the Java backend.
    thing_model_api = ThingModelApi()
    api_models = await thing_model_api.async_fetch_model_list()
    if api_models:
        models_to_load = api_models
        MODEL_PREFIXES.clear()
        MODEL_PREFIXES.extend(api_models)
        _LOGGER.info("Using dynamic model list from API: %s", models_to_load)
    else:
        models_to_load = []
        MODEL_PREFIXES.clear()
        _LOGGER.warning(
            "Model list API unavailable — devices will be discovered via MQTT "
            "Register/UplinkData, and their thing models fetched on-demand"
        )

    for model in models_to_load:
        tm = await thing_model_api.async_fetch_thing_model(model, version=None)
        if tm is not None:
            _LOGGER.info("Loaded thing model: %s", model)
        else:
            _LOGGER.warning(
                "Failed to load default thing model for %s — will retry on register", model
            )

    # Store the API client so other modules can access it
    hass.data.setdefault("hzsz_iot_001_thing_model_api", thing_model_api)

    # 2. Pre-register static devices with their thing models.
    #    KNOWN_DEVICES is now empty; kept for compatibility and future use.
    hub.setup_static_devices(KNOWN_DEVICES, thing_model_api)

    # 3. Load cached dynamic devices from previous sessions
    await hub.load_cache(thing_model_api)

    entry.runtime_data = hub

    # 4. Forward to platforms FIRST — their dispatcher listeners for
    #    SIGNAL_NEW_DEVICE must be registered before MQTT messages can
    #    trigger device auto-registration.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 5. Start MQTT client with user-configured connection details.
    #    Values come from the config entry (set during initial setup wizard);
    #    fall back to const defaults for backwards compatibility with entries
    #    created before the config-flow UI was added.
    mqtt_broker = entry.data.get(CONF_HOST, DEFAULT_MQTT_BROKER)
    mqtt_port = entry.data.get(CONF_PORT, DEFAULT_MQTT_PORT)
    mqtt_username = entry.data.get(CONF_USERNAME, DEFAULT_MQTT_USERNAME)
    mqtt_password = entry.data.get(CONF_PASSWORD, DEFAULT_MQTT_PASSWORD)

    mqtt_handler = MQTTHandler(
        hass,
        entry,
        hub,
        thing_model_api,
        broker_host=mqtt_broker,
        broker_port=mqtt_port,
        broker_username=mqtt_username,
        broker_password=mqtt_password,
    )
    mqtt_handler.connect()

    # 6. Periodic offline timeout check
    async def _check_offline(_now):
        hub.check_offline_devices()

    entry.async_on_unload(
        async_track_time_interval(hass, _check_offline, OFFLINE_CHECK)
    )

    # 7. Store for cleanup and for other modules (e.g. switch) to access
    hass.data.setdefault("hzsz_iot_001_instances", {})
    hass.data["hzsz_iot_001_instances"][entry.entry_id] = mqtt_handler

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload a config entry."""
    mqtt_handler: MQTTHandler | None = (
        hass.data.get("hzsz_iot_001_instances", {}).pop(entry.entry_id, None)
    )
    if mqtt_handler:
        mqtt_handler.disconnect()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
