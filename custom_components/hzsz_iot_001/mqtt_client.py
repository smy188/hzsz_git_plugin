"""MQTT client — subscribes to HZSZ Bluetooth gateway uplink, register, and heartbeat data.

When a Register message arrives for a supported model, the thing model
is fetched from the Java API and the device is registered.

UplinkData is routed by the routing_field (default: deviceId).

Command messages can be sent to /hzsz/gateway/{gatewaySn}/cmd for
controlling devices (e.g. led_on / led_off).
"""

from __future__ import annotations

import json
import logging
import uuid

from homeassistant.core import HomeAssistant, callback

from homeassistant.config_entries import ConfigEntry

from .const import (
    DEFAULT_MQTT_BROKER,
    DEFAULT_MQTT_PASSWORD,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_USERNAME,
    DOMAIN,
    QOS,
    TOPIC_HEARTBEAT,
    TOPIC_REGISTER,
    TOPIC_UPLINK,
)
from .hub import MODEL_PREFIXES, DeviceHub
from .thing_model_api import ThingModelApi

_LOGGER = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt

    HAS_PAHO = True
    try:
        from paho.mqtt.enums import CallbackAPIVersion

        PAHO_V2 = True
    except ImportError:
        PAHO_V2 = False
except ImportError:
    HAS_PAHO = False
    PAHO_V2 = False


def _extract_gateway_sn(topic: str) -> str:
    """Extract gateway SN from MQTT topic.

    Handles both formats:
      "hzsz/gateway/GW001/UplinkData"   → "GW001"
      "/hzsz/gateway/GW001/UplinkData"  → "GW001"  (leading slash)
    """
    # Strip leading slash so split indices are consistent
    topic = topic.lstrip("/")
    parts = topic.split("/")
    # After stripping: parts = ["hzsz", "gateway", "GW001", "UplinkData"]
    if len(parts) >= 3:
        return parts[2]
    return ""


def _topic_type(topic: str) -> str:
    """Determine the topic type from its suffix."""
    if topic.endswith("/UplinkData"):
        return "uplink"
    if topic.endswith("/Register"):
        return "register"
    if topic.endswith("/Heartbeat"):
        return "heartbeat"
    return "unknown"


class MQTTHandler:
    """Manages MQTT connection and routes incoming data to hub devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        hub: DeviceHub,
        thing_model_api: ThingModelApi,
        broker_host: str = DEFAULT_MQTT_BROKER,
        broker_port: int = DEFAULT_MQTT_PORT,
        broker_username: str = DEFAULT_MQTT_USERNAME,
        broker_password: str = DEFAULT_MQTT_PASSWORD,
    ) -> None:
        if not HAS_PAHO:
            raise RuntimeError("paho-mqtt is not installed")

        self._hass = hass
        self._entry = entry
        self._hub = hub
        self._thing_model_api = thing_model_api
        self._broker_host = broker_host
        self._broker_port = broker_port

        client_id = f"ha_{DOMAIN}_{uuid.uuid4().hex[:8]}"
        if PAHO_V2:
            self._client = mqtt.Client(
                CallbackAPIVersion.VERSION1,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
        else:
            self._client = mqtt.Client(
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
        self._client.username_pw_set(broker_username, broker_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def connect(self) -> None:
        _LOGGER.info("Connecting to MQTT broker %s:%d", self._broker_host, self._broker_port)
        self._client.connect_async(self._broker_host, self._broker_port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        _LOGGER.info("Disconnecting MQTT")
        self._client.loop_stop()
        self._client.disconnect()

    def publish_command(self, gateway_sn: str, device_id: str, cmd: str) -> None:
        """Send a command to a device via MQTT.

        Publishes to /hzsz/gateway/{gatewaySn}/cmd with payload
        {"deviceId": "...", "cmd": "..."}
        """
        topic = f"/hzsz/gateway/{gateway_sn}/cmd"
        payload = json.dumps({"deviceId": device_id, "cmd": cmd})
        _LOGGER.info("Sending command to %s: topic=%s payload=%s", device_id, topic, payload)
        self._client.publish(topic, payload, qos=QOS)

    # ------------------------------------------------------------------
    #  MQTT callbacks (paho thread — use call_soon_threadsafe)
    # ------------------------------------------------------------------

    def _on_connect(self, client: mqtt.Client, userdata: None, flags: dict, rc: int) -> None:
        if rc == 0:
            _LOGGER.info("MQTT connected, subscribing to topics")
            client.subscribe(TOPIC_UPLINK, qos=QOS)
            client.subscribe(TOPIC_REGISTER, qos=QOS)
            client.subscribe(TOPIC_HEARTBEAT, qos=QOS)
        else:
            _LOGGER.error("MQTT connect failed, rc=%d", rc)

    def _on_disconnect(self, client: mqtt.Client, userdata: None, rc: int) -> None:
        if rc != 0:
            _LOGGER.warning("MQTT unexpected disconnect, rc=%d", rc)
        else:
            _LOGGER.info("MQTT disconnected cleanly")

    def _on_message(self, client: mqtt.Client, userdata: None, msg: mqtt.MQTTMessage) -> None:
        """Parse incoming MQTT message and dispatch by topic type."""
        _LOGGER.debug("MQTT message received on topic: %s (QoS=%d)", msg.topic, msg.qos)

        try:
            payload_str = msg.payload.decode("utf-8", errors="replace")
            payload = json.loads(payload_str)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _LOGGER.warning("MQTT payload parse error on %s: %s", msg.topic, exc)
            return

        if not isinstance(payload, dict):
            _LOGGER.debug("MQTT payload is not a dict on %s: %s", msg.topic, type(payload))
            return

        ttype = _topic_type(msg.topic)
        gateway_sn = _extract_gateway_sn(msg.topic)

        _LOGGER.debug(
            "MQTT dispatch: type=%s gateway_sn=%s deviceId=%s model=%s",
            ttype, gateway_sn,
            payload.get("deviceId", "?"),
            payload.get("model", "?"),
        )

        if ttype == "register":
            self._hass.loop.call_soon_threadsafe(self._handle_register, gateway_sn, payload)
        elif ttype == "uplink":
            self._hass.loop.call_soon_threadsafe(self._handle_uplink, gateway_sn, payload)
        elif ttype == "heartbeat":
            self._hass.loop.call_soon_threadsafe(self._handle_heartbeat, gateway_sn, payload)
        else:
            _LOGGER.warning("Unknown MQTT topic: %s", msg.topic)

    # ------------------------------------------------------------------
    #  HA main-thread handlers (@callback)
    # ------------------------------------------------------------------

    @callback
    def _handle_register(self, gateway_sn: str, payload: dict) -> None:
        """Handle Register message — register a new device.

        The /register topic payload contains ``model``, ``deviceId``,
        and ``firmwareVersion``. We use these to fetch the thing model
        and create the device.
        """
        _LOGGER.debug("_handle_register called: gateway_sn=%s", gateway_sn)

        device_id = payload.get("deviceId", "")
        if not device_id:
            _LOGGER.warning("Register message missing deviceId, ignoring payload: %s", payload)
            return

        model = payload.get("model", "")
        if not model:
            _LOGGER.warning("Register message missing model, ignoring: %s", payload)
            return

        # Check if model is supported
        if model not in MODEL_PREFIXES:
            _LOGGER.info(
                "Register unsupported model: %s (deviceId=%s, gateway_sn=%s) — "
                "supported: %s",
                model, device_id, gateway_sn, MODEL_PREFIXES,
            )
            # Still register it if we can get a thing model — the MODEL_PREFIXES
            # list is just a fast filter; the API decides what's really supported.
            # Fall through and try the API.

        version = payload.get("firmwareVersion", "")
        if not version:
            version = None

        # Derive a friendly name from model + deviceId (short suffix)
        friendly_name = f"{model}_{gateway_sn}_{device_id[-6:]}" if len(device_id) > 6 else f"{model}_{gateway_sn}_{device_id}"

        _LOGGER.info(
            "Device registered — gateway_sn=%s model=%s version=%s deviceId=%s name=%s",
            gateway_sn, model, version or "<default>", device_id, friendly_name,
        )

        self._hass.async_create_background_task(
            self._async_register_device(
                gateway_sn, device_id, model, friendly_name, version=version,
            ),
            f"{DOMAIN}_register_{device_id}",
        )

    @callback
    def _handle_uplink(self, gateway_sn: str, payload: dict) -> None:
        """Handle UplinkData — route sensor data to the matching device.

        If the device is not yet registered (e.g. UplinkData arrives before
        Register), auto-register it by fetching the thing model.
        """
        _LOGGER.debug("_handle_uplink called: gateway_sn=%s", gateway_sn)

        device_id = payload.get("deviceId", "")
        if not device_id:
            _LOGGER.warning("UplinkData missing deviceId, ignoring payload: %s", payload)
            return

        model = payload.get("model", "")
        if not model:
            _LOGGER.warning("UplinkData missing model, ignoring: %s", payload)
            return

        version = payload.get("firmwareVersion", "")
        if not version:
            version = None

        friendly_name = f"{model}_{gateway_sn}_{device_id[-6:]}" if len(device_id) > 6 else f"{model}_{gateway_sn}_{device_id}"

        _LOGGER.info(
            "UplinkData received: model=%s version=%s deviceId=%s name=%s gateway_sn=%s fields=%s",
            model, version or "<default>", device_id, friendly_name, gateway_sn,
            list(payload.keys()),
        )

        device = self._hub.get_device(device_id)
        if device is None:
            _LOGGER.info(
                "UplinkData from unknown device — auto-registering: model=%s deviceId=%s "
                "name=%s gateway_sn=%s",
                model, device_id, friendly_name, gateway_sn,
            )
            self._hass.async_create_background_task(
                self._async_register_device(
                    gateway_sn, device_id, model, friendly_name,
                    initial_payload=payload, version=version,
                ),
                f"{DOMAIN}_register_{device_id}",
            )
            return

        # Cached devices may be restored with an empty thing model.
        if not device._thing_model:
            _LOGGER.info(
                "UplinkData from cached device %s (%s, version=%s) with empty "
                "thing model — re-fetching",
                device_id, model, version or "<default>",
            )
            self._hass.async_create_background_task(
                self._async_register_device(
                    gateway_sn, device_id, model, friendly_name,
                    initial_payload=payload, version=version,
                ),
                f"{DOMAIN}_register_{device_id}",
            )
            return

        _LOGGER.debug(
            "UplinkData routed to existing device: %s (deviceId=%s) — %d fields",
            device.device_name, device_id, len(payload),
        )
        device.update_from_mqtt(payload)

    @callback
    def _handle_heartbeat(self, gateway_sn: str, payload: dict) -> None:
        """Handle Heartbeat message — mark gateway and its devices as online."""
        _LOGGER.debug(
            "Heartbeat from gateway_sn=%s deviceId=%s timestamp=%s",
            gateway_sn,
            payload.get("deviceId", "?"),
            payload.get("timestamp", "?"),
        )

        device_id = payload.get("deviceId", "")
        if device_id:
            device = self._hub.get_device(device_id)
            if device is not None:
                device.mark_online()

    # ------------------------------------------------------------------
    #  Async registration helper
    # ------------------------------------------------------------------

    async def _async_register_device(
        self,
        gateway_sn: str,
        device_id: str,
        model: str,
        friendly_name: str,
        initial_payload: dict | None = None,
        version: str | None = None,
    ) -> None:
        """Fetch thing model and register the device (async safe).

        If ``initial_payload`` is provided (auto-registration from UplinkData),
        the device is immediately updated with that data after registration.
        """
        try:
            # Determine whether this is a re-registration of an existing device.
            # If so, force-refresh from the Java API to pick up any changed
            # entity names, units, etc.  New devices can safely use the cache.
            existing_device = self._hub.get_device(device_id)
            force_refresh = existing_device is not None and bool(existing_device._thing_model)

            if force_refresh:
                _LOGGER.info(
                    "Re-register %s (version=%s, deviceId=%s) — force-refreshing "
                    "thing model from API to pick up backend changes",
                    model, version or "<default>", device_id,
                )
            else:
                cached = self._thing_model_api.get_cached(model, version=version)
                if cached is not None:
                    _LOGGER.info(
                        "Auto-register %s (version=%s, deviceId=%s) — thing model cache HIT "
                        "(%d entities)",
                        model, version or "<default>", device_id,
                        len(cached.get("entities", [])),
                    )
                else:
                    _LOGGER.info(
                        "Auto-register %s (version=%s, deviceId=%s) — thing model cache MISS, "
                        "sending HTTP request",
                        model, version or "<default>", device_id,
                    )

            thing_model = await self._thing_model_api.async_fetch_thing_model(
                model, version=version, force_refresh=force_refresh,
            )
            if thing_model is None:
                _LOGGER.warning(
                    "Cannot register %s (version=%s, deviceId=%s) — thing model not "
                    "available from API, will retry on next UplinkData",
                    model, version or "<default>", device_id,
                )
                return

            device = self._hub.register_device(
                gateway_sn, device_id, model, friendly_name, thing_model, version=version
            )

            # Apply the first UplinkData payload that triggered auto-registration
            if initial_payload:
                device.update_from_mqtt(initial_payload)

            _LOGGER.info(
                "Auto-registered device: %s (deviceId=%s, gateway_sn=%s) — "
                "%d properties, %d data fields",
                model, device_id, gateway_sn,
                sum(len(e.get("properties", [])) for e in thing_model.get("entities", [])),
                len(initial_payload) if initial_payload else 0,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to auto-register device %s (deviceId=%s) — will retry on next "
                "UplinkData",
                model, device_id,
            )
