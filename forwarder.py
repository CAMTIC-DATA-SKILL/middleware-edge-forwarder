"""
IPC(ZeroMQ) 수신 → 북향 반영 루프.

NORTHBOUND_MODE=opcua 이면 OPC UA 서버에 값을 쓴다.
NORTHBOUND_MODE=mqtt 이면 같은 수집값을 MQTT 로만 발행하고 OPC UA 서버는 열지 않는다.
OPCUA_UPDATE_PERIOD_MS 가 있으면 수신 즉시가 아니라 그 주기로 최신 값만 반영한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from asyncua import ua
from pydantic import ValidationError

from client.errors import ClientError
from model.app_model import NorthboundMode, Settings
from model.ipc_model import IpcMessage, collector_ref, source_id
from model.protocol_model import MsgDataDTO, MsgHealthDTO, MsgSampleDTO, MsgSocketDTO, MsgTypeEnum
from model.tag_model import CollectorHeader, TagConversion, TagSettings, identification_placements
from mqtt_publisher.publisher import MqttPublisher, health_message, value_message
from mqtt_publisher.topics import health_topic, value_topic
from opcua_server.server import NorthboundServer, TagNodeSpec
from opcua_server.values import node_type, status_for_error, variant_from_opcua_type
from admin.server import AdminServer
from utils.message_log import MessageLog
from zeromq_client.sub_client import ZmqSubClient
from zeromq_client.topics import sub_topics_for

logger = logging.getLogger(__name__)

_UNCERTAIN = ua.StatusCode(ua.StatusCodes.UncertainLastUsableValue)
_NO_CONVERSION = TagConversion()


def _value_preview(value: object) -> str:
    if isinstance(value, list):
        return f"list[{len(value)}]"
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


@dataclass
class _SourceState:
    last_data_mono: float | None = None
    stale: bool = False
    device_ok: bool | None = None
    reason: str = ""


@dataclass
class _PendingWrite:
    tag_id: str
    value: object
    sample_ts: datetime
    conversion: TagConversion
    error: str | None = None
    # MQTT 모드에서만 사용. 있으면 flush 시 이 토픽으로 발행한다
    topic: str | None = None
    sample_ms: int = 0


class Forwarder:
    def __init__(
        self,
        settings: Settings,
        *,
        ipc: ZmqSubClient | None = None,
        server: NorthboundServer | None = None,
        mqtt: MqttPublisher | None = None,
        message_log: MessageLog | None = None,
    ) -> None:
        self.settings = settings
        app = settings.app
        self._mode = app.northbound
        self._mqtt_mode = self._mode is NorthboundMode.MQTT
        self._topic_root = app.mqtt.topic_root
        self.ipc = ipc or ZmqSubClient(
            app.zmq.sub_endpoints,
            bind=app.zmq.sub_bind,
            linger_ms=app.zmq.linger_ms,
            topics=sub_topics_for(
                root=app.zmq.topic_root,
                kinds=app.zmq.collector_kinds,
                device_keys=app.zmq.device_keys,
            ),
            topic_root=app.zmq.topic_root,
        )
        if self._mqtt_mode:
            # 테스트가 스파이 서버를 넘겨도 호출하지 않는다. 운영에서는 None
            self.server = server
            self.mqtt = mqtt if mqtt is not None else MqttPublisher(app.mqtt)
        else:
            self.server = server or NorthboundServer(app.opcua)
            self.mqtt = None
        self.message_log = message_log or MessageLog(app.ipc_log)
        self._index: dict[tuple[str, str, str], list[TagSettings]] = {}
        self._socket_index: dict[tuple[str, str, str, str], list[TagSettings]] = {}
        for tag in settings.tags:
            if not tag.enabled:
                continue
            if tag.source.is_socket:
                self._socket_index.setdefault(tag.source.socket_key, []).append(tag)
            else:
                self._index.setdefault(tag.source.key, []).append(tag)
        self._headers: dict[tuple[str, str], CollectorHeader] = {
            header.key: header for header in settings.collectors
        }
        self._sources: dict[str, _SourceState] = {}
        self._pending: dict[str, _PendingWrite] = {}
        self._auto_skipped: set[str] = set()
        self._multi_warned: set[tuple[str, ...]] = set()
        self.messages_received = 0
        self.messages_invalid = 0
        self.started_mono = time.monotonic()
        self._update_period_ms = max(0, app.opcua.update_period_ms)

    def _collector_ref(self, kind: str, device_key: str) -> str:
        return collector_ref(kind, device_key, root=self.settings.app.zmq.topic_root)

    def _collector_ref_sid(self, sid: str) -> str:
        kind, _, device_key = sid.partition("/")
        return self._collector_ref(kind, device_key) if device_key else sid

    def _opcua(self) -> NorthboundServer:
        if self.server is None:
            raise RuntimeError("OPC UA 서버가 없습니다")
        return self.server

    def _mqtt(self) -> MqttPublisher:
        if self.mqtt is None:
            raise RuntimeError("MQTT publisher 가 없습니다")
        return self.mqtt

    async def run(self, stop: asyncio.Event | None = None) -> bool:
        """SIGTERM/SIGINT 로 정리 종료. 관리 페이지에서 재시작을 누르면 True."""
        stop = stop or asyncio.Event()
        logger.info("북향 모드 %s", self._mode.value)
        restart = False
        tasks: list[asyncio.Task] = []
        admin: AdminServer | None = None
        admin_task: asyncio.Task | None = None
        if self.settings.app.admin.enable:
            admin = AdminServer(self, stop)
            admin_task = asyncio.create_task(admin.serve(), name="admin")
            await admin.started.wait()
        try:
            if not self._mqtt_mode:
                await self._opcua().start()
                await self._register_mapped_tags()
            self.message_log.check_writable()
            ready = await self._connect_mqtt(stop) if self._mqtt_mode else True
            if ready:
                ready = await self._connect_ipc(stop)
            if ready:
                tasks = [
                    asyncio.create_task(self._recv_loop(), name="ipc-recv"),
                    asyncio.create_task(self._watch_loop(), name="source-watch"),
                ]
                if self._update_period_ms > 0:
                    tasks.append(asyncio.create_task(self._flush_loop(), name="northbound-flush"))
                    logger.info("북향 값 반영 주기 %dms", self._update_period_ms)
                await stop.wait()
            restart = bool(admin and admin.restart)
        finally:
            if not stop.is_set():
                stop.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self._flush_pending()
            if admin_task is not None:
                try:
                    await admin_task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("관리 페이지 종료 실패")
            self.ipc.close()
            self.message_log.close()
            if self._mqtt_mode:
                await asyncio.to_thread(self._mqtt().close)
            else:
                await self._opcua().stop()
        return restart

    async def handle(self, msg: IpcMessage) -> None:
        """메시지 1건 처리. ack / cmd 는 무시한다."""
        self.messages_received += 1
        self.message_log.record(msg)
        try:
            if msg.header.msg_type is MsgTypeEnum.DATA:
                await self._on_data(msg)
            elif msg.header.msg_type is MsgTypeEnum.HEALTH:
                await self._on_health(msg)
        except ValidationError as e:
            self.messages_invalid += 1
            logger.warning("[%s] %s body 파싱 실패: %s", msg.topic, msg.header.msg_type.value, e)

    async def _register_mapped_tags(self) -> None:
        if self.settings.tag_map_file is None:
            logger.warning("tag map 파일이 없어 auto 노드만 사용합니다 (TAG_MAP_FILE 확인)")
        enabled = [t for t in self.settings.tags if t.enabled]
        for tag in enabled:
            sid = source_id(tag.source.kind, tag.source.device_key)
            await self._ensure_source(sid)
            vtype, is_array = variant_from_opcua_type(tag.target.data_type)
            await self._opcua().add_tag(
                TagNodeSpec(
                    tag_id=tag.tag_id,
                    segments=tag.target.segments,
                    name=tag.target.name,
                    vtype=vtype,
                    source_id=sid,
                    description=tag.target.description,
                    unit=tag.target.unit,
                    node_id=tag.node_id,
                    is_array=is_array,
                )
            )
        await self._register_identifications()
        logger.info(
            "tag map 태그 %d개 등록 (전체 %d개, auto=%s)",
            len(enabled),
            len(self.settings.tags),
            self.settings.app.opcua.auto_create_nodes,
        )

    async def _register_identifications(self) -> None:
        count = 0
        for segments, header in identification_placements(
            self.settings.collectors, self.settings.app.opcua.auto_root
        ):
            if await self._opcua().add_identification(segments, header.identification_fields()):
                count += 1
        if count:
            logger.info("Identification %d개 등록 (collector %d개)", count, len(self.settings.collectors))

    async def _connect_ipc(self, stop: asyncio.Event) -> bool:
        """연결될 때까지 재시도. stop 되면 False."""
        delay_s = max(0.1, self.settings.app.reconnect_period_ms / 1000.0)
        while not stop.is_set():
            try:
                self.ipc.connect()
                logger.info(
                    "ZeroMQ SUB ready %s topics=%s",
                    self.ipc._target(),
                    self.ipc.topics,
                )
                return True
            except ClientError as e:
                logger.error("ZeroMQ connect failed: %s(%s) %s", e.code.value, e.code.label, e.detail or e)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay_s)
        return False

    async def _connect_mqtt(self, stop: asyncio.Event) -> bool:
        """브로커 CONNACK 까지 재시도. stop 되면 False."""
        delay_s = max(0.1, self.settings.app.reconnect_period_ms / 1000.0)
        mqtt = self._mqtt()
        while not stop.is_set():
            try:
                await asyncio.to_thread(mqtt.connect)
                return True
            except ClientError as e:
                logger.error("MQTT connect failed: %s(%s) %s", e.code.value, e.code.label, e.detail or e)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay_s)
        return False

    async def _recv_loop(self) -> None:
        while True:
            try:
                msg = await self.ipc.recv()
            except ClientError as e:
                self.messages_invalid += 1
                logger.warning("IPC 수신 오류: %s(%s) %s", e.code.value, e.code.label, e.detail or e)
                continue
            try:
                await self.handle(msg)
            except Exception:
                logger.exception("[%s] 메시지 처리 실패", msg.topic)

    async def _watch_loop(self) -> None:
        """수신 공백 감시 + 카운터 갱신 + 메시지 로그 정리 (1초 주기)."""
        stale_ms = self.settings.app.source_stale_ms
        while True:
            self.message_log.maybe_cleanup()
            await asyncio.sleep(1.0)
            if not self._mqtt_mode:
                await self._opcua().set_counters(received=self.messages_received, invalid=self.messages_invalid)
            if stale_ms <= 0:
                continue
            now = time.monotonic()
            for sid, state in self._sources.items():
                if state.stale or state.last_data_mono is None:
                    continue
                if (now - state.last_data_mono) * 1000 < stale_ms:
                    continue
                await self._mark_stale(sid, state, stale_ms)

    async def _mark_stale(self, sid: str, state: _SourceState, stale_ms: int) -> None:
        state.stale = True
        await self._flush_pending()
        if self._mqtt_mode:
            kind, _, device_key = sid.partition("/")
            self._publish_health(kind, device_key, state)
            logger.warning("[%s] DATA %dms 동안 없음", self._collector_ref_sid(sid), stale_ms)
            return
        await self._opcua().set_source_state(sid, stale=True)
        n = await self._opcua().mark_source_tags(sid, _UNCERTAIN)
        logger.warning("[%s] DATA %dms 동안 없음 → 태그 %d개 Uncertain", self._collector_ref_sid(sid), stale_ms, n)

    async def _on_data(self, msg: IpcMessage) -> None:
        body = MsgDataDTO.model_validate(msg.header.msg_body)
        sid = source_id(msg.kind, body.device_key)
        state = await self._ensure_source(sid)
        if not self._mqtt_mode:
            await self._ensure_collector_identification(msg.kind, body.device_key)
        sample_ts = datetime.fromtimestamp(body.sample_ms / 1000.0, tz=timezone.utc)

        for sample in body.samples:
            if self._mqtt_mode:
                self._queue_mqtt_sample(msg.kind, body.device_key, sample, body.sample_ms)
                continue
            mapped = self._index.get((msg.kind, body.device_key, sample.addr))
            if mapped:
                for tag in mapped:
                    await self._write_sample(tag.tag_id, sample, sample_ts, tag.conversion)
                continue
            tag_id = await self._ensure_auto_tag(msg.kind, body.device_key, sample)
            if tag_id is not None:
                await self._write_sample(tag_id, sample, sample_ts, _NO_CONVERSION)

        for sock in body.sockets:
            if self._mqtt_mode:
                self._queue_mqtt_socket(msg.kind, body.device_key, sock, body.sample_ms)
            else:
                await self._on_socket(msg.kind, body.device_key, sock, sample_ts)

        state.last_data_mono = time.monotonic()
        if state.stale:
            state.stale = False
            logger.info("[%s] DATA 수신 재개", self._collector_ref(msg.kind, body.device_key))
            if self._mqtt_mode:
                self._publish_health(msg.kind, body.device_key, state)
            else:
                await self._opcua().set_source_state(sid, stale=False)
        if not self._mqtt_mode:
            await self._opcua().set_source_state(sid, last_data=datetime.now(timezone.utc))

    async def _on_health(self, msg: IpcMessage) -> None:
        body = MsgHealthDTO.model_validate(msg.header.msg_body)
        sid = source_id(msg.kind, body.device_key)
        state = await self._ensure_source(sid)
        if not self._mqtt_mode:
            await self._ensure_collector_identification(msg.kind, body.device_key)
        changed = state.device_ok is not body.device_ok
        state.device_ok = body.device_ok
        state.reason = body.reason or ""
        if self._mqtt_mode:
            if not body.device_ok:
                await self._flush_pending()
            self._publish_health(msg.kind, body.device_key, state)
            if body.device_ok:
                if changed:
                    logger.info("[%s] HEALTH device_ok=true", self._collector_ref(msg.kind, body.device_key))
            else:
                logger.warning(
                    "[%s] HEALTH device_ok=false reason=%s",
                    self._collector_ref(msg.kind, body.device_key),
                    body.reason,
                )
            return
        await self._opcua().set_source_state(sid, device_ok=body.device_ok, reason=state.reason)
        if body.device_ok:
            if changed:
                logger.info("[%s] HEALTH device_ok=true", self._collector_ref(msg.kind, body.device_key))
            return
        await self._flush_pending()
        n = await self._opcua().mark_source_tags(sid, _UNCERTAIN)
        logger.warning(
            "[%s] HEALTH device_ok=false reason=%s → 태그 %d개 Uncertain",
            self._collector_ref(msg.kind, body.device_key),
            body.reason,
            n,
        )

    async def _ensure_collector_identification(self, kind: str, device_key: str) -> None:
        """수신한 collector id 로 헤더를 찾아 Identification 을 붙인다."""
        if self._mqtt_mode:
            return
        header = self._headers.get((kind, device_key))
        if header is None or not header.has_identification:
            return
        await self._opcua().add_identification(
            header.object_segments(self.settings.app.opcua.auto_root),
            header.identification_fields(),
        )

    async def _ensure_source(self, sid: str) -> _SourceState:
        state = self._sources.get(sid)
        if state is None:
            state = self._sources[sid] = _SourceState()
            if not self._mqtt_mode:
                await self._opcua().ensure_source(sid)
        return state

    async def _on_socket(self, kind: str, device_key: str, sock: MsgSocketDTO, sample_ts: datetime) -> None:
        if sock.error:
            for tag in self._mapped_socket_tags(kind, device_key, sock.name):
                await self._write_value(tag.tag_id, None, sample_ts, tag.conversion, error=sock.error)
            return
        if sock.value is None:
            return
        for field, value in sock.value.items():
            mapped = self._socket_index.get((kind, device_key, sock.name, field))
            if mapped:
                for tag in mapped:
                    await self._write_value(tag.tag_id, value, sample_ts, tag.conversion)
                continue
            tag_id = await self._ensure_auto_socket_tag(kind, device_key, sock, field, value)
            if tag_id is not None:
                await self._write_value(tag_id, value, sample_ts, _NO_CONVERSION)

    def _mapped_socket_tags(self, kind: str, device_key: str, name: str) -> list[TagSettings]:
        tags: list[TagSettings] = []
        for (k, device, socket_name, _), mapped in self._socket_index.items():
            if (k, device, socket_name) == (kind, device_key, name):
                tags.extend(mapped)
        return tags

    def _conversion_for(self, tags: list[TagSettings], key: tuple[str, ...]) -> TagConversion:
        """MQTT 토픽은 소스당 하나다. 매핑이 여러 개면 첫 변환만 쓴다."""
        if not tags:
            return _NO_CONVERSION
        if len(tags) > 1 and key not in self._multi_warned:
            self._multi_warned.add(key)
            logger.warning("MQTT 토픽은 소스당 하나라 첫 tag map 변환만 사용합니다: %s (%d개)", key, len(tags))
        return tags[0].conversion

    def _queue_mqtt_sample(self, kind: str, device_key: str, sample: MsgSampleDTO, sample_ms: int) -> None:
        mapped = self._index.get((kind, device_key, sample.addr), [])
        conversion = self._conversion_for(mapped, (kind, device_key, sample.addr))
        topic = value_topic(kind, device_key, sample.addr, root=self._topic_root)
        self._queue_mqtt(topic, sample.value, sample_ms, conversion, sample.error)

    def _queue_mqtt_socket(self, kind: str, device_key: str, sock: MsgSocketDTO, sample_ms: int) -> None:
        if sock.error:
            fields = [
                field
                for (k, device, name, field) in self._socket_index
                if (k, device, name) == (kind, device_key, sock.name)
            ]
            if not fields:
                topic = value_topic(kind, device_key, sock.name, root=self._topic_root)
                self._queue_mqtt(topic, None, sample_ms, _NO_CONVERSION, sock.error)
                return
            for field in fields:
                mapped = self._socket_index.get((kind, device_key, sock.name, field), [])
                topic = value_topic(kind, device_key, sock.name, field, root=self._topic_root)
                conversion = self._conversion_for(mapped, (kind, device_key, sock.name, field))
                self._queue_mqtt(topic, None, sample_ms, conversion, sock.error)
            return
        if sock.value is None:
            return
        for field, value in sock.value.items():
            mapped = self._socket_index.get((kind, device_key, sock.name, field), [])
            conversion = self._conversion_for(mapped, (kind, device_key, sock.name, field))
            topic = value_topic(kind, device_key, sock.name, field, root=self._topic_root)
            self._queue_mqtt(topic, value, sample_ms, conversion)

    def _queue_mqtt(
        self,
        topic: str,
        value: object,
        sample_ms: int,
        conversion: TagConversion,
        error: str | None = None,
    ) -> None:
        if error is None and value is None:
            return
        sample_ts = datetime.fromtimestamp(sample_ms / 1000.0, tz=timezone.utc)
        if self._update_period_ms > 0:
            self._pending[topic] = _PendingWrite(
                tag_id=topic,
                value=value,
                sample_ts=sample_ts,
                conversion=conversion,
                error=error,
                topic=topic,
                sample_ms=sample_ms,
            )
            return
        self._publish_mqtt(topic, value, sample_ms, conversion, error)

    def _publish_mqtt(
        self,
        topic: str,
        value: object,
        sample_ms: int,
        conversion: TagConversion,
        error: str | None = None,
    ) -> None:
        if error:
            payload = value_message(None, sample_ms, error)
        elif value is None:
            return
        else:
            payload = value_message(conversion.apply(value), sample_ms)
        try:
            self._mqtt().publish(topic, payload)
        except ClientError as e:
            logger.warning("MQTT 발행 실패 [%s]: %s(%s) %s", topic, e.code.value, e.code.label, e.detail or e)

    def _publish_health(self, kind: str, device_key: str, state: _SourceState) -> None:
        topic = health_topic(kind, device_key, root=self._topic_root)
        payload = health_message(device_ok=state.device_ok, stale=state.stale, reason=state.reason)
        try:
            self._mqtt().publish(topic, payload)
        except ClientError as e:
            logger.warning("MQTT 발행 실패 [%s]: %s(%s) %s", topic, e.code.value, e.code.label, e.detail or e)

    async def _ensure_auto_tag(self, kind: str, device_key: str, sample: MsgSampleDTO) -> str | None:
        opcua = self.settings.app.opcua
        if not opcua.auto_create_nodes:
            return None
        tag_id = f"{kind}.{device_key}.{sample.addr}"
        if self._opcua().has_tag(tag_id):
            return tag_id
        if tag_id in self._auto_skipped:
            return None
        parsed = node_type(sample.type, sample.value)
        if parsed is None:
            # 값이 없거나 빈 배열이면 다음 샘플에서 다시 본다. 그 외에는 이 주소를 건너뛴다.
            if sample.value is not None and not (isinstance(sample.value, list) and len(sample.value) == 0):
                self._auto_skipped.add(tag_id)
                logger.warning(
                    "auto 노드 형식을 정할 수 없어 건너뜀: %s type=%s value=%s",
                    tag_id,
                    sample.type,
                    _value_preview(sample.value),
                )
            return None
        vtype, is_array = parsed
        added = await self._opcua().add_tag(
            TagNodeSpec(
                tag_id=tag_id,
                segments=(opcua.auto_root, kind, device_key),
                name=sample.addr,
                vtype=vtype,
                source_id=source_id(kind, device_key),
                description=sample.name or "",
                is_array=is_array,
            )
        )
        if added:
            logger.info("auto 노드 생성: %s (%s)", tag_id, vtype.name)
        return tag_id

    async def _ensure_auto_socket_tag(
        self,
        kind: str,
        device_key: str,
        sock: MsgSocketDTO,
        field: str,
        value: object,
    ) -> str | None:
        opcua = self.settings.app.opcua
        if not opcua.auto_create_nodes:
            return None
        tag_id = f"{kind}.{device_key}.{sock.name}.{field}"
        if self._opcua().has_tag(tag_id):
            return tag_id
        if tag_id in self._auto_skipped:
            return None
        parsed = node_type(None, value)
        if parsed is None:
            if value is not None and not (isinstance(value, list) and len(value) == 0):
                self._auto_skipped.add(tag_id)
                logger.warning(
                    "auto 노드 형식을 정할 수 없어 건너뜀: %s value=%s",
                    tag_id,
                    _value_preview(value),
                )
            return None
        vtype, is_array = parsed
        added = await self._opcua().add_tag(
            TagNodeSpec(
                tag_id=tag_id,
                segments=(opcua.auto_root, kind, device_key, sock.name),
                name=field,
                vtype=vtype,
                source_id=source_id(kind, device_key),
                description=f"{sock.command_name or sock.name} {field}".strip(),
                is_array=is_array,
            )
        )
        if added:
            logger.info("auto 노드 생성: %s (%s)", tag_id, vtype.name)
        return tag_id

    async def _write_sample(
        self,
        tag_id: str,
        sample: MsgSampleDTO,
        sample_ts: datetime,
        conversion: TagConversion,
    ) -> None:
        await self._write_value(tag_id, sample.value, sample_ts, conversion, error=sample.error)

    async def _write_value(
        self,
        tag_id: str,
        value: object,
        sample_ts: datetime,
        conversion: TagConversion,
        error: str | None = None,
    ) -> None:
        if error is None and value is None:
            return
        if self._update_period_ms > 0:
            self._pending[tag_id] = _PendingWrite(tag_id, value, sample_ts, conversion, error)
            return
        await self._apply_write(tag_id, value, sample_ts, conversion, error)

    async def _flush_loop(self) -> None:
        delay_s = max(0.001, self._update_period_ms / 1000.0)
        while True:
            await asyncio.sleep(delay_s)
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        if not self._pending:
            return
        pending = self._pending
        self._pending = {}
        for item in pending.values():
            if item.topic is not None:
                self._publish_mqtt(item.topic, item.value, item.sample_ms, item.conversion, item.error)
                continue
            await self._apply_write(item.tag_id, item.value, item.sample_ts, item.conversion, item.error)

    async def _apply_write(
        self,
        tag_id: str,
        value: object,
        sample_ts: datetime,
        conversion: TagConversion,
        error: str | None = None,
    ) -> None:
        if error:
            await self._opcua().update(tag_id, None, status=status_for_error(error), source_timestamp=sample_ts)
            return
        if value is None:
            return
        await self._opcua().update(tag_id, conversion.apply(value), source_timestamp=sample_ts)


async def run(settings: Settings) -> bool:
    """SIGTERM/SIGINT 로 정리 종료. 관리 페이지에서 재시작을 누르면 True."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    return await Forwarder(settings).run(stop)
