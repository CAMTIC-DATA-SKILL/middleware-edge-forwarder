"""북향 MQTT publisher. 브로커 연결과 발행만 담당한다."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import paho.mqtt.client as mqtt

from client.errors import ClientError, to_client_error
from model.app_model import MqttPublisherSettings
from model.error_model import ClientErrorCode
from mqtt_publisher.topics import require_topic

logger = logging.getLogger(__name__)


def dumps_payload(body: object) -> bytes:
    """발행 payload. UTF-8 JSON."""
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise ClientError(ClientErrorCode.INVALID_CONFIG, f"MQTT payload JSON 변환 실패: {e}") from e


def value_message(value: object, sample_ms: int, error: str | None = None) -> bytes:
    """샘플·소켓 값. error 가 있으면 value 는 null."""
    body: dict[str, object] = {"value": value, "sample_ms": sample_ms}
    if error:
        body["value"] = None
        body["error"] = error
    return dumps_payload(body)


def health_message(*, device_ok: bool | None, stale: bool, reason: str) -> bytes:
    return dumps_payload({"device_ok": device_ok, "stale": stale, "reason": reason})


class MqttPublisher:
    """paho 네트워크 스레드로 브로커에 connect 하고 publish 한다."""

    def __init__(self, cfg: MqttPublisherSettings) -> None:
        self.cfg = cfg
        self._client: mqtt.Client | None = None
        self._connack = threading.Event()
        self._connack_rc: Any = None
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self._client is not None

    @property
    def is_connected(self) -> bool:
        client = self._client
        return client is not None and client.is_connected()

    def connect(self) -> None:
        """TCP 연결 후 CONNACK 까지 기다린다. 실패하면 ClientError."""
        self.close()
        cfg = self.cfg
        if not cfg.host.strip():
            raise ClientError(ClientErrorCode.INVALID_CONFIG, "MQTT 브로커 호스트가 비어 있습니다")
        if cfg.password and not cfg.username:
            raise ClientError(
                ClientErrorCode.INVALID_CONFIG,
                "MQTT_PASSWORD 는 MQTT_USERNAME 과 함께 설정합니다",
            )

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.client_id,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_timeout = float(cfg.connect_timeout_s)
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password or None)
        if cfg.tls:
            client.tls_set()

        self._connack.clear()
        self._connack_rc = None
        try:
            rc = client.connect(cfg.host, cfg.port, keepalive=cfg.keepalive_s)
            if int(rc) != 0:
                raise ClientError(
                    ClientErrorCode.CONNECT_FAILED,
                    f"MQTT connect rc={rc} {cfg.host}:{cfg.port}",
                )
            client.loop_start()
            if not self._connack.wait(timeout=cfg.connect_timeout_s):
                raise ClientError(
                    ClientErrorCode.TIMEOUT,
                    f"MQTT CONNACK 없음: {cfg.host}:{cfg.port}",
                )
            reason = self._connack_rc
            if getattr(reason, "is_failure", True):
                raise ClientError(ClientErrorCode.CONNECT_FAILED, f"MQTT 연결 거부: {reason}")
        except ClientError:
            self._stop_client(client)
            raise
        except Exception as e:
            self._stop_client(client)
            raise to_client_error(e, default=ClientErrorCode.CONNECT_FAILED) from e

        self._client = client
        logger.info(
            "MQTT publisher ready %s:%s client_id=%s tls=%s",
            cfg.host,
            cfg.port,
            cfg.client_id or "(auto)",
            cfg.tls,
        )

    def publish(self, topic: str, payload: bytes | str) -> None:
        """한 건 발행. QoS 0 은 브로커 ack 를 기다리지 않는다."""
        client = self._client
        if client is None:
            raise ClientError(
                ClientErrorCode.CONNECT_FAILED,
                f"MQTT 미연결: {self.cfg.host}:{self.cfg.port}",
            )
        topic = require_topic(topic)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        try:
            info = client.publish(topic, payload, qos=self.cfg.qos, retain=self.cfg.retain)
        except ValueError as e:
            raise ClientError(ClientErrorCode.INVALID_CONFIG, str(e)) from e
        except Exception as e:
            raise to_client_error(e, default=ClientErrorCode.WRITE_FAILED) from e
        if int(info.rc) != 0:
            raise ClientError(
                ClientErrorCode.WRITE_FAILED,
                f"MQTT publish rc={info.rc} topic={topic}",
            )
        logger.debug("mqtt pub [%s] %d bytes", topic, len(payload))

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._stop_client(client)

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._connack_rc = reason_code
        self._connack.set()
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT 연결 거부: %s", reason_code)
            return
        logger.info("MQTT 연결됨: %s", reason_code)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if self._closing:
            logger.info("MQTT publisher 종료")
            return
        logger.warning("MQTT 연결 끊김: %s", reason_code)

    def _stop_client(self, client: mqtt.Client) -> None:
        self._closing = True
        try:
            try:
                client.disconnect()
            except Exception as e:
                logger.debug("mqtt disconnect failed: %s", e)
            try:
                client.loop_stop()
            except Exception as e:
                logger.debug("mqtt loop_stop failed: %s", e)
        finally:
            self._closing = False
