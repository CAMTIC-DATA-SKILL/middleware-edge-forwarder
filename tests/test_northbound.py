from __future__ import annotations

import json
import unittest
from uuid import uuid4

from model.app_model import (
    AppSettings,
    EmptySettingError,
    IpcLogSettings,
    MqttPublisherSettings,
    NorthboundMode,
    OpcUaServerSettings,
    Settings,
    parse_northbound_mode,
)
from model.ipc_model import IpcMessage
from model.protocol_model import MsgTypeEnum, ProtocolHeaderDTO
from model.tag_model import TagSettings
from forwarder import Forwarder
from utils.message_log import MessageLog


class _FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic: str, payload: bytes | str) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        self.published.append((topic, payload))


class _FakeServer:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object, object]] = []

    def has_tag(self, tag_id: str) -> bool:
        return False

    async def add_tag(self, spec: object) -> bool:
        return False

    async def update(self, tag_id: str, value: object, status: object = None, source_timestamp: object = None) -> bool:
        self.updates.append((tag_id, value, status))
        return True

    async def ensure_source(self, sid: str) -> None:
        return None

    async def add_identification(self, segments: object, fields: object) -> bool:
        return False

    async def set_source_state(self, *args: object, **kwargs: object) -> None:
        return None

    async def set_counters(self, **kwargs: object) -> None:
        return None

    async def mark_source_tags(self, sid: str, status: object) -> int:
        return 0


def _settings(mode: NorthboundMode, *tags: dict, period_ms: int = 0) -> Settings:
    return Settings(
        app=AppSettings(
            northbound=mode,
            opcua=OpcUaServerSettings(auto_create_nodes=False, update_period_ms=period_ms),
            mqtt=MqttPublisherSettings(topic_root="edge"),
            ipc_log=IpcLogSettings(summary=False, file_enable=False),
        ),
        tags=[TagSettings.model_validate(tag) for tag in tags],
    )


def _forwarder(mode: NorthboundMode, mqtt: _FakeMqtt, server: _FakeServer, *tags: dict, period_ms: int = 0) -> Forwarder:
    return Forwarder(
        _settings(mode, *tags, period_ms=period_ms),
        ipc=object(),  # type: ignore[arg-type]
        server=server,  # type: ignore[arg-type]
        mqtt=mqtt,  # type: ignore[arg-type]
        message_log=MessageLog(IpcLogSettings(summary=False, file_enable=False)),
    )


def _msg(msg_type: MsgTypeEnum, body: dict) -> IpcMessage:
    return IpcMessage(
        kind="robot",
        topic=f"collector.robot.robot-1.{msg_type.value}",
        header=ProtocolHeaderDTO(
            msg_id=uuid4(),
            gateway_address="gw",
            collector_address="robot-1",
            msg_type=msg_type,
            timestamp_ms=1,
            msg_body=body,
        ),
    )


_MAPPED = {
    "tag_id": "robot1.temp",
    "source": {"kind": "robot", "device_key": "robot-1", "addr": "D100"},
    "target": {"path": "Line", "name": "Temp", "data_type": "Double"},
    "conversion": {"scale": 0.1, "offset": 0},
}


class NorthboundModeTest(unittest.TestCase):
    def test_parse_aliases(self) -> None:
        self.assertEqual(parse_northbound_mode("OPC-UA"), NorthboundMode.OPCUA)
        self.assertEqual(parse_northbound_mode("mqtt"), NorthboundMode.MQTT)

    def test_parse_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            parse_northbound_mode("both")

    def test_empty_raises(self) -> None:
        for raw in (None, "", "   "):
            with self.assertRaises(EmptySettingError) as ctx:
                parse_northbound_mode(raw)
            self.assertEqual(ctx.exception.key, "NORTHBOUND_MODE")


class NorthboundForwardTest(unittest.IsolatedAsyncioTestCase):
    async def test_opcua_mode_does_not_publish(self) -> None:
        mqtt = _FakeMqtt()
        server = _FakeServer()
        fwd = _forwarder(NorthboundMode.OPCUA, mqtt, server, _MAPPED)
        await fwd.handle(
            _msg(MsgTypeEnum.DATA, {"device_key": "robot-1", "sample_ms": 1000, "samples": [{"addr": "D100", "value": 10}]})
        )
        self.assertIsNone(fwd.mqtt)
        self.assertEqual(server.updates, [("robot1.temp", 1.0, None)])
        self.assertEqual(mqtt.published, [])

    async def test_mqtt_mode_publishes_converted_value(self) -> None:
        mqtt = _FakeMqtt()
        server = _FakeServer()
        fwd = _forwarder(NorthboundMode.MQTT, mqtt, server, _MAPPED)
        await fwd.handle(
            _msg(
                MsgTypeEnum.DATA,
                {
                    "device_key": "robot-1",
                    "sample_ms": 1000,
                    "samples": [{"addr": "D100", "value": 10}, {"addr": "S0004", "value": True}],
                },
            )
        )
        self.assertEqual(server.updates, [])
        published = {topic: json.loads(payload) for topic, payload in mqtt.published}
        self.assertEqual(published["edge/robot/robot-1/D100"], {"value": 1.0, "sample_ms": 1000})
        self.assertEqual(published["edge/robot/robot-1/S0004"], {"value": True, "sample_ms": 1000})

    async def test_mqtt_socket_and_health(self) -> None:
        mqtt = _FakeMqtt()
        fwd = _forwarder(NorthboundMode.MQTT, mqtt, _FakeServer())
        await fwd.handle(
            _msg(
                MsgTypeEnum.DATA,
                {
                    "device_key": "robot-1",
                    "sample_ms": 1000,
                    "samples": [],
                    "sockets": [{"name": "hw", "value": {"cpu_temperature": 36.5}}],
                },
            )
        )
        await fwd.handle(
            _msg(
                MsgTypeEnum.HEALTH,
                {
                    "device_key": "robot-1",
                    "ipc_ok": True,
                    "device_ok": False,
                    "reason": "E-1001",
                    "observed_ms": 1000,
                },
            )
        )
        published = {topic: json.loads(payload) for topic, payload in mqtt.published}
        self.assertEqual(published["edge/robot/robot-1/hw/cpu_temperature"], {"value": 36.5, "sample_ms": 1000})
        self.assertEqual(
            published["edge/robot/robot-1/health"],
            {"device_ok": False, "stale": False, "reason": "E-1001"},
        )

    async def test_mqtt_period_keeps_latest(self) -> None:
        mqtt = _FakeMqtt()
        fwd = _forwarder(NorthboundMode.MQTT, mqtt, _FakeServer(), period_ms=1000)
        await fwd.handle(_msg(MsgTypeEnum.DATA, {"device_key": "robot-1", "sample_ms": 1000, "samples": [{"addr": "S0004", "value": 1}]}))
        await fwd.handle(_msg(MsgTypeEnum.DATA, {"device_key": "robot-1", "sample_ms": 2000, "samples": [{"addr": "S0004", "value": 2}]}))
        self.assertEqual(mqtt.published, [])
        await fwd._flush_pending()
        topic, payload = mqtt.published[0]
        self.assertEqual(topic, "edge/robot/robot-1/S0004")
        self.assertEqual(json.loads(payload), {"value": 2, "sample_ms": 2000})

    async def test_mqtt_error_payload(self) -> None:
        mqtt = _FakeMqtt()
        fwd = _forwarder(NorthboundMode.MQTT, mqtt, _FakeServer())
        await fwd.handle(
            _msg(
                MsgTypeEnum.DATA,
                {"device_key": "robot-1", "sample_ms": 1000, "samples": [{"addr": "D100", "error": "E-2203"}]},
            )
        )
        topic, payload = mqtt.published[0]
        self.assertEqual(topic, "edge/robot/robot-1/D100")
        self.assertEqual(json.loads(payload), {"value": None, "sample_ms": 1000, "error": "E-2203"})
