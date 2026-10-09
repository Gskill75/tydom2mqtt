import asyncio
import json
from unittest.mock import MagicMock

import pytest

from sensors.Opening import Opening
from sensors.Sensor import Sensor
from tydom import MessageHandler as message_handler_module
from tydom.MessageHandler import MessageHandler

DEVICE_ID = 1789228300
ENDPOINT_ID = 1789228300


def _published(mqtt):
    """Map of topic -> payload published through the mocked MQTT client."""
    return {
        call.args[0]: call.args[1] for call in mqtt.mqtt_client.publish.call_args_list
    }


def _parse_window(last_usage, data, name="Fenetre Cuisine"):
    mqtt = MagicMock()
    handler = MessageHandler(b"", MagicMock(cmd_prefix=""), mqtt)
    config = {
        "endpoints": [
            {
                "id_endpoint": ENDPOINT_ID,
                "id_device": DEVICE_ID,
                "name": name,
                "last_usage": last_usage,
            }
        ]
    }
    endpoint = {
        "id": ENDPOINT_ID,
        "error": 0,
        "data": [
            {"name": key, "validity": "upToDate", "value": value}
            for key, value in data.items()
        ],
    }
    asyncio.run(handler.parse_config_data(config))
    asyncio.run(handler.parse_endpoint_data(endpoint, DEVICE_ID))
    return _published(mqtt)


@pytest.fixture(autouse=True)
def _clean_device_cache():
    yield
    for cache in (
        "device_name",
        "device_type",
        "device_endpoint",
        "device_parent_name",
    ):
        getattr(message_handler_module, cache).clear()


# Regression: only the last element of a window used to be published, so the
# opening state could be missing in Home Assistant.
def test_kline_window_publishes_every_element_and_opening():
    published = _parse_window(
        "klineWindowFrench",
        {"openState": "OPEN_FRENCH", "intrusionDetect": True, "battDefect": False},
    )
    uid = f"{DEVICE_ID}_{ENDPOINT_ID}"

    for element in ("openstate", "intrusiondetect", "battdefect"):
        assert any(
            topic.endswith(f"/{element}_tydom_{uid}/config") for topic in published
        ), element

    opening_config = json.loads(
        published[f"homeassistant/binary_sensor/tydom/opening_tydom_{uid}/config"]
    )
    assert opening_config["device_class"] == "window"
    assert opening_config["device"]["name"] == "Fenetre Cuisine"
    assert published[f"binary_sensor/tydom/opening_tydom_{uid}/state"] == "ON"


# K-Line: the opening detector is a child endpoint of the bay (parent device,
# whose main endpoint is the shutter); it must join the parent HA device.
def test_kline_child_detector_attached_to_parent_device():
    mqtt = MagicMock()
    handler = MessageHandler(b"", MagicMock(cmd_prefix=""), mqtt)
    config = {
        "endpoints": [
            {
                "id_endpoint": DEVICE_ID,
                "id_device": DEVICE_ID,
                "name": "Baie Salon",
                "last_usage": "klineShutter",
            },
            {
                "id_endpoint": 2,
                "id_device": DEVICE_ID,
                "name": "Detecteur",
                "last_usage": "klineWindowSliding",
            },
        ]
    }
    endpoint = {
        "id": 2,
        "error": 0,
        "data": [
            {"name": "openState", "validity": "upToDate", "value": "LOCKED"},
            {"name": "battDefect", "validity": "upToDate", "value": False},
        ],
    }
    asyncio.run(handler.parse_config_data(config))
    asyncio.run(handler.parse_endpoint_data(endpoint, DEVICE_ID))
    published = _published(mqtt)

    parent = f"{DEVICE_ID}_{DEVICE_ID}"
    child = f"{DEVICE_ID}_2"
    opening = json.loads(
        published[f"homeassistant/binary_sensor/tydom/opening_tydom_{child}/config"]
    )
    batt = json.loads(
        published[f"homeassistant/binary_sensor/tydom/battdefect_tydom_{child}/config"]
    )
    for config_payload in (opening, batt):
        assert config_payload["device"]["identifiers"] == parent
        assert config_payload["device"]["name"] == "Baie Salon"
        assert "model" not in config_payload["device"]
    # Named after its device class, next to the shutter (main feature)
    assert "name" not in opening
    assert opening["device_class"] == "window"


def test_door_opening_uses_door_class():
    published = _parse_window("klineDoor", {"openState": "LOCKED"})
    uid = f"{DEVICE_ID}_{ENDPOINT_ID}"
    opening_config = json.loads(
        published[f"homeassistant/binary_sensor/tydom/opening_tydom_{uid}/config"]
    )
    assert opening_config["device_class"] == "door"
    assert published[f"binary_sensor/tydom/opening_tydom_{uid}/state"] == "OFF"


# Names were only kept for known usages, so unsupported devices lost theirs.
def test_name_kept_for_unsupported_usage():
    _parse_window("someNewUsage", {}, name="Detecteur Garage")
    handler = MessageHandler(b"", MagicMock(cmd_prefix=""), None)
    assert handler.get_name_from_id(f"{ENDPOINT_ID}_{DEVICE_ID}") == "Detecteur Garage"


@pytest.mark.parametrize(
    "open_state,expected",
    [("LOCKED", "OFF"), ("UNLOCKED", "ON"), ("OPEN_HOPPER", "ON")],
)
def test_opening_state(open_state, expected):
    opening = Opening(
        tydom_attributes={"id": "1_1", "name": "Baie", "openState": open_state}
    )
    assert opening.state() == expected


def test_batt_defect_is_diagnostic_battery():
    payload = {"id": "1_1", "name": "Baie", "battDefect": False}
    sensor = Sensor(elem_name="battDefect", tydom_attributes_payload=payload)
    asyncio.run(sensor.setup())
    assert sensor.binary is True
    assert sensor.config["device_class"] == "battery"
    assert sensor.config["entity_category"] == "diagnostic"


def test_element_meta_overrides_device_wide_class():
    payload = {
        "id": "1_1",
        "name": "Sonde",
        "outTemperature": 12.5,
        "lightPower": 300,
        "element_meta": {
            "outTemperature": {
                "device_class": "temperature",
                "unit_of_measurement": "°C",
            },
            "lightPower": {"device_class": "power", "unit_of_measurement": "W"},
        },
    }
    temperature = Sensor(elem_name="outTemperature", tydom_attributes_payload=payload)
    power = Sensor(elem_name="lightPower", tydom_attributes_payload=payload)
    asyncio.run(temperature.setup())
    asyncio.run(power.setup())
    assert temperature.config["device_class"] == "temperature"
    assert power.config["device_class"] == "power"
    assert power.config["unit_of_measurement"] == "W"
