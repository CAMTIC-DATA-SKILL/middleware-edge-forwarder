"""북향 OPC UA 서버: 태그 노드 관리, 값·품질 갱신, _Forwarder 상태 노드."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from asyncua import Node, Server, ua

from model.app_model import OpcUaServerSettings
from model.tag_model import IDENTIFICATION_BROWSE_NAME, RESERVED_ROOT
from opcua_server.values import coerce_array, coerce_value

logger = logging.getLogger(__name__)

_GOOD = ua.StatusCode(ua.StatusCodes.Good)
_WAITING = ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData)
_TYPE_MISMATCH = ua.StatusCode(ua.StatusCodes.BadTypeMismatch)


@dataclass(frozen=True)
class TagNodeSpec:
    """북향 변수 노드 하나. node_id 가 있으면 그 값, 없으면 s=<tag_id>."""

    tag_id: str
    segments: tuple[str, ...]
    name: str
    vtype: ua.VariantType
    source_id: str
    description: str = ""
    unit: str = ""
    node_id: int | str | None = None
    is_array: bool = False


@dataclass(frozen=True)
class _TagNode:
    node_id: ua.NodeId
    vtype: ua.VariantType
    source_id: str
    is_array: bool = False


@dataclass(frozen=True)
class _SourceNodes:
    device_ok: ua.NodeId
    stale: ua.NodeId
    reason: ua.NodeId
    last_data: ua.NodeId


class NorthboundServer:
    """
    collector 샘플을 상위 시스템에 제공하는 OPC UA 서버.

    - 태그는 실행 중에도 추가할 수 있다 (tag map 은 시작 시, auto 노드는 첫 샘플 수신 시).
    - 값을 받기 전까지 StatusCode 는 BadWaitingForInitialData.
    - tag map `collectors` 헤더가 있으면 해당 collector 객체 아래에 `Identification` 상수를 둔다.
    - `_Forwarder` 아래에 프로세스·수집원 상태 노드를 둔다. 상위 쓰기는 허용하지 않는다.
    """

    def __init__(self, cfg: OpcUaServerSettings) -> None:
        self._cfg = cfg
        self._server: Server | None = None
        self._ns = 0
        self._objects: dict[tuple[str, ...], Node] = {}
        self._tags: dict[str, _TagNode] = {}
        self._sources: dict[str, _SourceNodes] = {}
        # 품질만 바꿀 때 값·발생 시각을 유지하기 위한 마지막 정상 값
        self._last_values: dict[str, tuple[ua.Variant, datetime]] = {}
        self._mismatch_logged: set[str] = set()
        self._status: dict[str, ua.NodeId] = {}
        self._sources_root: Node | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def tag_count(self) -> int:
        return len(self._tags)

    def has_tag(self, tag_id: str) -> bool:
        return tag_id in self._tags

    def has_source(self, source_id: str) -> bool:
        return source_id in self._sources

    async def start(self) -> None:
        if self._server is not None:
            return
        cfg = self._cfg
        server = Server()
        await server.init()
        server.set_endpoint(cfg.endpoint)
        server.set_server_name(cfg.server_name)
        await server.set_application_uri(cfg.application_uri)
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self._ns = await server.register_namespace(cfg.namespace_uri)
        self._objects = {(): server.nodes.objects}
        self._server = server
        try:
            await self._build_status_nodes()
            await server.start()
        except BaseException:
            self._server = None
            raise
        logger.info("북향 OPC UA 서버 시작: %s (namespace=%s)", cfg.endpoint, cfg.namespace_uri)

    async def stop(self) -> None:
        if self._server is None:
            return
        try:
            await self._server.stop()
        finally:
            self._server = None
            self._tags.clear()
            self._sources.clear()
            self._last_values.clear()
            self._status.clear()
            logger.info("북향 OPC UA 서버 종료: %s", self._cfg.endpoint)

    async def add_tag(self, spec: TagNodeSpec) -> bool:
        """이미 있는 tag_id 면 False."""
        if self._server is None or spec.tag_id in self._tags:
            return False
        parent = await self._ensure_object_path(spec.segments)
        node_id = variable_node_id(spec.tag_id, spec.node_id, self._ns)
        initial = [] if spec.is_array else ua.get_default_value(spec.vtype)
        variable = await parent.add_variable(
            node_id,
            ua.QualifiedName(spec.name, self._ns),
            initial,
            varianttype=spec.vtype,
            datatype=_data_type_id(spec.vtype),
        )
        if spec.is_array:
            await _mark_one_dimension(variable)
        if spec.description:
            await variable.write_attribute(
                ua.AttributeIds.Description,
                ua.DataValue(ua.Variant(ua.LocalizedText(spec.description), ua.VariantType.LocalizedText)),
            )
        if spec.unit:
            await variable.add_property(
                ua.NodeId(f"{spec.tag_id}.EngineeringUnits", self._ns),
                ua.QualifiedName("EngineeringUnits", 0),
                ua.EUInformation(
                    UnitId=-1,
                    DisplayName=ua.LocalizedText(spec.unit),
                    Description=ua.LocalizedText(spec.unit),
                ),
                varianttype=ua.VariantType.ExtensionObject,
                datatype=ua.NodeId(ua.ObjectIds.EUInformation),
            )
        waiting = [] if spec.is_array else ua.get_default_value(spec.vtype)
        await variable.write_value(
            ua.DataValue(ua.Variant(waiting, spec.vtype), StatusCode=_WAITING)
        )
        self._tags[spec.tag_id] = _TagNode(node_id, spec.vtype, spec.source_id, spec.is_array)
        await self._write_status(self._status.get("TagCount"), len(self._tags), ua.VariantType.UInt32)
        logger.debug("태그 추가: %s (%s/%s, %s)", spec.tag_id, "/".join(spec.segments), spec.name, spec.vtype.name)
        return True

    async def add_identification(self, segments: tuple[str, ...], fields: tuple[tuple[str, str], ...]) -> bool:
        """설비 경로 아래에 Identification 상수 노드를 만든다. 이미 있으면 False."""
        if self._server is None or not fields or not segments or segments[0] == RESERVED_ROOT:
            return False
        if not any(name in ("AasId", "GlobalAssetId") for name, _ in fields):
            return False
        ident_segments = (*segments, IDENTIFICATION_BROWSE_NAME)
        if ident_segments in self._objects:
            return False
        parent = await self._ensure_object_path(segments)
        ident = await parent.add_object(
            ua.NodeId("/".join(ident_segments), self._ns),
            ua.QualifiedName(IDENTIFICATION_BROWSE_NAME, self._ns),
        )
        self._objects[ident_segments] = ident
        for name, value in fields:
            variable = await ident.add_variable(
                ua.NodeId("/".join((*ident_segments, name)), self._ns),
                ua.QualifiedName(name, self._ns),
                value,
                varianttype=ua.VariantType.String,
                datatype=_data_type_id(ua.VariantType.String),
            )
            await variable.set_writable(False)
        logger.debug("Identification 추가: %s", "/".join(ident_segments))
        return True

    async def update(
        self,
        tag_id: str,
        value: Any,
        status: ua.StatusCode | None = None,
        source_timestamp: datetime | None = None,
    ) -> bool:
        """
        태그 값을 갱신한다. 등록되지 않은 tag_id 면 False.

        - value 가 None 이면 마지막 정상 값을 유지하고 품질 상태만 바꾼다 (아직 값이 없으면 무시).
        - 품질이 Bad 이면 OPC UA 규격에 따라 값은 null.
        - 노드 형식으로 변환할 수 없으면 BadTypeMismatch.
        """
        tag = self._tags.get(tag_id)
        if tag is None or self._server is None:
            return False

        now = datetime.now(timezone.utc)
        status_code = status or _GOOD
        variant: ua.Variant | None = None

        if value is not None:
            try:
                coerced = coerce_array(value, tag.vtype) if tag.is_array else coerce_value(value, tag.vtype)
                variant = ua.Variant(coerced, tag.vtype)
                self._mismatch_logged.discard(tag_id)
            except (TypeError, ValueError, OverflowError) as e:
                if tag_id not in self._mismatch_logged:
                    logger.warning("값을 %s 형식으로 변환할 수 없어 BadTypeMismatch 로 표시: %s (%s)", tag.vtype.name, tag_id, e)
                    self._mismatch_logged.add(tag_id)
                status_code = _TYPE_MISMATCH

        if status_code.is_bad():
            variant = ua.Variant()
        elif variant is None:
            last = self._last_values.get(tag_id)
            if last is None:
                return True
            variant, last_ts = last
            source_timestamp = source_timestamp or last_ts
        else:
            self._last_values[tag_id] = (variant, source_timestamp or now)

        await self._write(
            tag.node_id,
            ua.DataValue(variant, StatusCode=status_code, SourceTimestamp=source_timestamp or now, ServerTimestamp=now),
        )
        return True

    async def mark_source_tags(self, source_id: str, status: ua.StatusCode) -> int:
        """수집원 태그 전체의 품질만 바꾼다 (값은 마지막 정상 값 유지). 바꾼 태그 수 반환."""
        count = 0
        for tag_id, tag in list(self._tags.items()):
            if tag.source_id == source_id:
                await self.update(tag_id, None, status=status)
                count += 1
        return count

    async def ensure_source(self, source_id: str) -> None:
        """_Forwarder/Sources/{kind}/{device_key} 상태 노드를 만든다."""
        if self._server is None or source_id in self._sources or self._sources_root is None:
            return
        parent = self._sources_root
        for segment in source_id.split("/"):
            node_key = f"{parent.nodeid.Identifier}.{segment}"
            existing = self._objects.get((RESERVED_ROOT, node_key))
            if existing is None:
                existing = await parent.add_object(ua.NodeId(node_key, self._ns), ua.QualifiedName(segment, self._ns))
                self._objects[(RESERVED_ROOT, node_key)] = existing
            parent = existing
        self._sources[source_id] = _SourceNodes(
            device_ok=await self._add_status_variable(parent, "DeviceOk", None, ua.VariantType.Boolean, "collector HEALTH 의 장비 연결 상태"),
            stale=await self._add_status_variable(parent, "Stale", False, ua.VariantType.Boolean, "SOURCE_STALE_MS 동안 DATA 가 없으면 true"),
            reason=await self._add_status_variable(parent, "Reason", "", ua.VariantType.String, "마지막 HEALTH 실패 사유 코드"),
            last_data=await self._add_status_variable(parent, "LastDataTime", None, ua.VariantType.DateTime, "마지막 DATA 수신 시각"),
        )

    async def set_source_state(
        self,
        source_id: str,
        *,
        device_ok: bool | None = None,
        stale: bool | None = None,
        reason: str | None = None,
        last_data: datetime | None = None,
    ) -> None:
        nodes = self._sources.get(source_id)
        if nodes is None:
            return
        if device_ok is not None:
            await self._write_status(nodes.device_ok, device_ok, ua.VariantType.Boolean)
        if stale is not None:
            await self._write_status(nodes.stale, stale, ua.VariantType.Boolean)
        if reason is not None:
            await self._write_status(nodes.reason, reason, ua.VariantType.String)
        if last_data is not None:
            await self._write_status(nodes.last_data, last_data, ua.VariantType.DateTime)
            await self._write_status(self._status.get("LastUpdateTime"), last_data, ua.VariantType.DateTime)

    async def set_counters(self, *, received: int, invalid: int) -> None:
        await self._write_status(self._status.get("MessagesReceived"), received, ua.VariantType.UInt64)
        await self._write_status(self._status.get("MessagesInvalid"), invalid, ua.VariantType.UInt64)

    async def _ensure_object_path(self, segments: tuple[str, ...]) -> Node:
        for depth in range(1, len(segments) + 1):
            key = segments[:depth]
            if key not in self._objects:
                self._objects[key] = await self._objects[key[:-1]].add_object(
                    ua.NodeId("/".join(key), self._ns),
                    ua.QualifiedName(key[-1], self._ns),
                )
        return self._objects[segments]

    async def _build_status_nodes(self) -> None:
        objects = self._objects[()]
        root = await objects.add_object(ua.NodeId(RESERVED_ROOT, self._ns), ua.QualifiedName(RESERVED_ROOT, self._ns))
        now = datetime.now(timezone.utc)
        self._status = {
            "StartTime": await self._add_status_variable(root, "StartTime", now, ua.VariantType.DateTime, "forwarder 시작 시각"),
            "TagCount": await self._add_status_variable(root, "TagCount", 0, ua.VariantType.UInt32, "제공 중인 태그 수"),
            "LastUpdateTime": await self._add_status_variable(root, "LastUpdateTime", None, ua.VariantType.DateTime, "마지막 DATA 반영 시각"),
            "MessagesReceived": await self._add_status_variable(root, "MessagesReceived", 0, ua.VariantType.UInt64, "IPC 수신 메시지 수"),
            "MessagesInvalid": await self._add_status_variable(root, "MessagesInvalid", 0, ua.VariantType.UInt64, "파싱 실패 메시지 수"),
        }
        self._sources_root = await root.add_object(
            ua.NodeId(f"{RESERVED_ROOT}.Sources", self._ns), ua.QualifiedName("Sources", self._ns)
        )

    async def _add_status_variable(
        self,
        parent: Node,
        name: str,
        value: Any,
        vtype: ua.VariantType,
        description: str,
    ) -> ua.NodeId:
        """value 가 None 이면 BadWaitingForInitialData 로 만든다."""
        variable = await parent.add_variable(
            ua.NodeId(f"{parent.nodeid.Identifier}.{name}", self._ns),
            ua.QualifiedName(name, self._ns),
            ua.get_default_value(vtype) if value is None else value,
            varianttype=vtype,
            datatype=_data_type_id(vtype),
        )
        await variable.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant(ua.LocalizedText(description), ua.VariantType.LocalizedText)),
        )
        if value is None:
            await variable.write_value(ua.DataValue(ua.Variant(ua.get_default_value(vtype), vtype), StatusCode=_WAITING))
        return variable.nodeid

    async def _write_status(self, node_id: ua.NodeId | None, value: Any, vtype: ua.VariantType) -> None:
        if node_id is not None:
            await self._write(node_id, ua.DataValue(ua.Variant(value, vtype), StatusCode=_GOOD))

    async def _write(self, node_id: ua.NodeId, data_value: ua.DataValue) -> bool:
        """`Server.write_attribute_value` 는 실패 결과를 버리므로 주소 공간에 직접 써서 확인한다."""
        if self._server is None:
            return False
        result = await self._server.iserver.aspace.write_attribute_value(node_id, ua.AttributeIds.Value, data_value)
        if not result.is_good():
            logger.warning("노드 값 쓰기 실패: %s (%s)", node_id.to_string(), result.name)
            return False
        return True


def variable_node_id(tag_id: str, node_id: int | str | None, ns: int) -> ua.NodeId:
    """tag map node_id → 북향 변수 NodeId. 문자열은 ns=2;s=... 형식을 그대로 쓴다."""
    if node_id is None:
        return ua.NodeId(tag_id, ns)
    if isinstance(node_id, int):
        return ua.NodeId(node_id, ns)
    raw = node_id.strip()
    if not raw:
        return ua.NodeId(tag_id, ns)
    if "=" in raw:
        try:
            return ua.NodeId.from_string(raw)
        except ua.UaError:
            pass
    return ua.NodeId(raw, ns)


def _data_type_id(vtype: ua.VariantType) -> ua.NodeId:
    return ua.NodeId(getattr(ua.ObjectIds, vtype.name))


async def _mark_one_dimension(variable: Node) -> None:
    """가변 길이 1차원 배열. asyncua 는 Dimensions 가 없으면 ValueRank 를 비워 둔다."""
    await variable.write_attribute(
        ua.AttributeIds.ValueRank,
        ua.DataValue(ua.Variant(int(ua.ValueRank.OneDimension), ua.VariantType.Int32)),
    )
    await variable.write_attribute(
        ua.AttributeIds.ArrayDimensions,
        ua.DataValue(ua.Variant([0], ua.VariantType.UInt32)),
    )
