from __future__ import annotations

import unittest

from pydantic import ValidationError

from client.errors import ClientError
from model.app_model import MqttPublisherSettings
from model.error_model import ClientErrorCode
from mqtt_publisher.publisher import MqttPublisher, dumps_payload
from mqtt_publisher.topics import health_topic, require_topic, value_topic


class MqttTopicTest(unittest.TestCase):
    def test_sample_topic(self) -> None:
        self.assertEqual(
            value_topic("robot", "robot-1", "S0004", root="edge"),
            "edge/robot/robot-1/S0004",
        )

    def test_socket_field_topic(self) -> None:
        self.assertEqual(
            value_topic("robot", "robot-1", "hw", "cpu_temperature", root="factory/line01"),
            "factory/line01/robot/robot-1/hw/cpu_temperature",
        )

    def test_wildcard_rejected(self) -> None:
        with self.assertRaises(ClientError) as ctx:
            value_topic("plc", "plc-1", "D100/#", root="edge")
        self.assertEqual(ctx.exception.code, ClientErrorCode.INVALID_CONFIG)

    def test_require_topic_rejects_empty_segment(self) -> None:
        with self.assertRaises(ClientError):
            require_topic("edge//plc")


    def test_health_topic(self) -> None:
        self.assertEqual(health_topic("robot", "robot-1", root="edge"), "edge/robot/robot-1/health")


class MqttPublisherTest(unittest.TestCase):
    def test_publish_before_connect(self) -> None:
        pub = MqttPublisher(MqttPublisherSettings())
        with self.assertRaises(ClientError) as ctx:
            pub.publish("edge/plc/plc-1/D100", b"1")
        self.assertEqual(ctx.exception.code, ClientErrorCode.CONNECT_FAILED)

    def test_password_requires_username(self) -> None:
        pub = MqttPublisher(MqttPublisherSettings(password="secret"))
        with self.assertRaises(ClientError) as ctx:
            pub.connect()
        self.assertEqual(ctx.exception.code, ClientErrorCode.INVALID_CONFIG)

    def test_dumps_payload_keeps_unicode(self) -> None:
        self.assertEqual(dumps_payload({"v": 1, "name": "온도"}), '{"v":1,"name":"온도"}'.encode())

    def test_qos_range(self) -> None:
        with self.assertRaises(ValidationError):
            MqttPublisherSettings(qos=3)

    def test_close_before_connect(self) -> None:
        MqttPublisher(MqttPublisherSettings()).close()
