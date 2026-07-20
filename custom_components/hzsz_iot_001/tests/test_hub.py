"""Unit tests for batch field reporting in DynamicDevice."""

from __future__ import annotations

import pytest

try:
    from custom_components.hzsz_iot_001.hub import DeviceHub, DynamicDevice
except ImportError:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from hzsz_iot_001.hub import DeviceHub, DynamicDevice


class _MockHass:
    """Minimal HomeAssistant mock for DeviceHub tests."""

    def __init__(self) -> None:
        self.tasks: list[object] = []

    def async_create_task(self, coro: object) -> object:
        self.tasks.append(coro)
        return coro


def _make_thing_model(field_ids: list[str]) -> dict:
    """Build a minimal thing model with one sensor entity per field."""
    return {
        "model": "TEST-6FIELDS",
        "modelName": "Test Six Fields",
        "routingField": "deviceId",
        "entities": [
            {
                "entityType": "sensor",
                "entityIdentifier": f"sensor_{fid}",
                "entityName": f"Sensor {fid.upper()}",
                "properties": [
                    {
                        "identifier": fid,
                        "name": f"Field {fid.upper()}",
                        "dataType": "float",
                    }
                ],
            }
            for fid in field_ids
        ],
    }


@pytest.fixture
def device() -> DynamicDevice:
    return DynamicDevice(
        device_id="dev001",
        device_name="TEST dev001",
        model="TEST-6FIELDS",
        thing_model=_make_thing_model(["a", "b", "c", "d", "e", "f"]),
        gateway_sn="GW001",
    )


def test_partial_update_notifies_only_changed_fields(device: DynamicDevice) -> None:
    """A payload with one field should invoke only that field's callback."""
    calls: dict[str, set[str] | None] = {}

    for fid in ["a", "b", "c", "d", "e", "f"]:
        device.register_callback(
            lambda changed_fields, fid=fid: calls.setdefault(fid, changed_fields)
        )

    device.update_from_mqtt({"deviceId": "dev001", "data": {"a": 1}})

    assert set(calls.keys()) == {"a"}
    assert calls["a"] == {"a"}


def test_partial_update_multiple_fields(device: DynamicDevice) -> None:
    """A payload with several fields invokes only those callbacks."""
    calls: dict[str, set[str] | None] = {}

    for fid in ["a", "b", "c", "d", "e", "f"]:
        device.register_callback(
            lambda changed_fields, fid=fid: calls.setdefault(fid, changed_fields)
        )

    device.update_from_mqtt({"deviceId": "dev001", "data": {"a": 1, "c": 3, "f": 6}})

    assert set(calls.keys()) == {"a", "c", "f"}
    assert calls["a"] == {"a", "c", "f"}
    assert calls["c"] == {"a", "c", "f"}
    assert calls["f"] == {"a", "c", "f"}


def test_full_update_notifies_all(device: DynamicDevice) -> None:
    """A payload with all fields invokes every callback."""
    calls: set[str] = set()

    for fid in ["a", "b", "c", "d", "e", "f"]:
        device.register_callback(
            lambda changed_fields, fid=fid: calls.add(fid)
        )

    device.update_from_mqtt({"deviceId": "dev001", "data": {fid: i for i, fid in enumerate(["a", "b", "c", "d", "e", "f"])}})

    assert calls == {"a", "b", "c", "d", "e", "f"}


def test_empty_payload_notifies_no_field_callbacks(device: DynamicDevice) -> None:
    """An empty payload should not invoke field callbacks, only mark online."""
    calls: set[str] = set()

    for fid in ["a", "b", "c", "d", "e", "f"]:
        device.register_callback(
            lambda changed_fields, fid=fid: calls.add(fid)
        )

    device.update_from_mqtt({"deviceId": "dev001"})

    assert calls == set()
    assert device.online is True


def test_online_offline_notifies_all(device: DynamicDevice) -> None:
    """mark_online / mark_offline should notify all callbacks with changed_fields=None."""
    online_calls: list[set[str] | None] = []
    offline_calls: list[set[str] | None] = []

    device.register_callback(lambda changed_fields: online_calls.append(changed_fields))
    device.register_callback(lambda changed_fields: offline_calls.append(changed_fields))

    device.mark_online()
    device.mark_offline()

    assert online_calls == [None]
    assert offline_calls == [None]


def test_previous_values_preserved_across_partial_updates(device: DynamicDevice) -> None:
    """Fields not included in a batch keep their previous values."""
    device.update_from_mqtt({"deviceId": "dev001", "data": {"a": 1, "b": 2}})
    device.update_from_mqtt({"deviceId": "dev001", "data": {"c": 3}})

    assert device.get("a") == 1
    assert device.get("b") == 2
    assert device.get("c") == 3
    assert device.get("d") is None


def test_metadata_keys_excluded_from_changed_fields(device: DynamicDevice) -> None:
    """deviceId, model, firmwareVersion etc. should not be treated as changed fields."""
    received: set[str] | None = None

    device.register_callback(lambda changed_fields: (received := changed_fields))

    device.update_from_mqtt({
        "deviceId": "dev001",
        "model": "TEST-6FIELDS",
        "firmwareVersion": "1.0",
        "timestamp": 123456,
        "data": {"a": 1},
    })

    assert received == {"a"}


def test_register_device_re_registers_on_model_change() -> None:
    """If a device reports with a different model, the old registration is replaced."""
    hass = _MockHass()
    hub = DeviceHub(hass)

    old_device = hub.register_device(
        gateway_sn="GW001",
        device_id="dev001",
        model="OLD-MODEL",
        device_name="OLD-MODEL_GW001_dev001",
        thing_model=_make_thing_model(["a", "b"]),
        version="1.0",
    )

    assert hub.get_device("dev001") is old_device
    assert old_device.model == "OLD-MODEL"

    new_device = hub.register_device(
        gateway_sn="GW001",
        device_id="dev001",
        model="NEW-MODEL",
        device_name="NEW-MODEL_GW001_dev001",
        thing_model=_make_thing_model(["x", "y", "z"]),
        version="2.0",
    )

    assert new_device is not old_device
    assert hub.get_device("dev001") is new_device
    assert new_device.model == "NEW-MODEL"
    assert new_device.device_name == "NEW-MODEL_GW001_dev001"
    # Cache save task should have been scheduled for the new registration
    assert len(hass.tasks) >= 1

