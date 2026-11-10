"""앱 설정 스키마 (ZeroMQ 수신, 북향 OPC UA 또는 MQTT)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from model.tag_model import CollectorHeader, TagSettings


class NorthboundMode(StrEnum):
    """북향은 한 번에 하나만 사용한다."""

    OPCUA = "opcua"
    MQTT = "mqtt"


class EmptySettingError(Exception):
    """필수 설정이 비어 있다. 기본값으로 대체하지 않는다."""

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"{key} 가 비어 있습니다")


def parse_northbound_mode(value: str | None) -> NorthboundMode:
    """opcua / mqtt. opc-ua, OPC_UA 같은 표기도 opcua 로 받는다. 비어 있으면 예외."""
    if value is None or not value.strip():
        raise EmptySettingError("NORTHBOUND_MODE")
    key = value.strip().lower().replace("-", "").replace("_", "")
    if key == NorthboundMode.OPCUA.value:
        return NorthboundMode.OPCUA
    if key == NorthboundMode.MQTT.value:
        return NorthboundMode.MQTT
    raise ValueError(f"NORTHBOUND_MODE 는 opcua 또는 mqtt 입니다: {value!r}")


class ZmqSettings(BaseModel):
    """collector PUB 을 구독하는 ZeroMQ SUB 설정."""

    # collector 는 PUB 을 bind 하므로 기본은 connect. collector 가 여럿이면 엔드포인트도 여럿
    sub_endpoints: list[str] = Field(default_factory=lambda: ["tcp://127.0.0.1:5556"])
    sub_bind: bool = False
    linger_ms: int = 0
    topic_root: str = "collector"
    # 비우면 collector.* 전체 구독
    collector_kinds: list[str] = Field(default_factory=lambda: ["plc"])
    # 비우면 kind 의 모든 device
    device_keys: list[str] = Field(default_factory=list)


class OpcUaServerSettings(BaseModel):
    """상위 시스템에 데이터를 제공하는 북향 OPC UA 서버 설정."""

    endpoint: str = "opc.tcp://0.0.0.0:4840/edge/"
    server_name: str = "middleware_edge_forwarder"
    application_uri: str = "urn:middleware:edge:forwarder"
    namespace_uri: str = "urn:middleware:edge:forwarder:tags"
    # tag map 에 없는 샘플도 {auto_root}/{kind}/{device_key}/{addr} 로 노출
    auto_create_nodes: bool = True
    auto_root: str = "Collectors"
    # 북향 노드 값 반영 주기. collector 수신이 더 빨라도 이 간격으로만 갱신 (0 = 수신 즉시)
    update_period_ms: int = Field(default=0, ge=0)


class MqttPublisherSettings(BaseModel):
    """NORTHBOUND_MODE=mqtt 일 때 쓰는 브로커 설정."""

    host: str = "127.0.0.1"
    port: int = Field(default=1883, ge=1, le=65535)
    client_id: str = "middleware_edge_forwarder"
    username: str = ""
    password: str = ""
    keepalive_s: int = Field(default=60, ge=1)
    qos: int = Field(default=0, ge=0, le=2)
    retain: bool = False
    # {topic_root}/{kind}/{device_key}/{name}
    topic_root: str = "edge"
    tls: bool = False
    # TCP 연결과 CONNACK 대기 (초)
    connect_timeout_s: int = Field(default=5, ge=1)


class AdminSettings(BaseModel):
    """같은 프로세스에서 여는 관리 페이지. 수집원 상태를 읽기만 한다."""

    enable: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8088, ge=1, le=65535)
    # 비어 있으면 검사하지 않는다. 루프백 밖으로 열 때 둔다
    token: str = ""


class IpcLogSettings(BaseModel):
    """IPC 수신 메시지 로그: 콘솔 요약 + device_key 별 일별 파일 전문 ({프로젝트 루트}/data/logs/{device_key})."""

    summary: bool = True
    file_enable: bool = True
    # 파일명 날짜 기준. 오늘 포함 최근 N일만 남긴다 (0 = 삭제 안 함)
    retention_days: int = 7
    # 매일 이 시각(로컬, 0~23시)에 정리
    clean_hour: int = Field(default=3, ge=0, le=23)
    # 기동 후 device 폴더를 처음 기록할 때도 정리
    clean_on_start: bool = True


class AppSettings(BaseModel):
    log_level: str = "INFO"
    reconnect_period_ms: int = 5000
    # 이 시간 동안 DATA 가 없으면 수집원 태그를 UncertainLastUsableValue 로 표시 (0 = 끔)
    source_stale_ms: int = 10000
    # opcua 면 OPC UA 서버만, mqtt 면 MQTT publisher 만 연다
    northbound: NorthboundMode = NorthboundMode.OPCUA
    zmq: ZmqSettings = Field(default_factory=ZmqSettings)
    opcua: OpcUaServerSettings = Field(default_factory=OpcUaServerSettings)
    mqtt: MqttPublisherSettings = Field(default_factory=MqttPublisherSettings)
    ipc_log: IpcLogSettings = Field(default_factory=IpcLogSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)


class Settings(BaseModel):
    app: AppSettings
    # 파일이 없으면 None (auto 노드만 사용)
    tag_map_file: Path | None = None
    tags: list[TagSettings] = Field(default_factory=list)
    collectors: list[CollectorHeader] = Field(default_factory=list)
