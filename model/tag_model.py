"""tag map 스키마: collector 샘플·소켓 필드 → 북향 OPC UA 노드."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# 북향 상태 노드 루트. 태그 경로로 쓸 수 없다
RESERVED_ROOT = "_Forwarder"
IDENTIFICATION_BROWSE_NAME = "Identification"
IDENTIFICATION_CHILD_NAMES = ("AasId", "GlobalAssetId", "DeviceKey")

OpcUaDataType = Literal[
    "Boolean",
    "SByte",
    "Byte",
    "Int16",
    "UInt16",
    "Int32",
    "UInt32",
    "Int64",
    "UInt64",
    "Float",
    "Double",
    "String",
    "DateTime",
    "Double[]",
]


def _no_dot(v: str) -> str:
    # 토픽 collector.{kind}.{device_key}.{msg_type} 계층용
    if not v or "." in v:
        raise ValueError(f"'.' 없는 비어 있지 않은 값이어야 합니다: {v!r}")
    return v


class TagSource(BaseModel):
    kind: str = "plc"
    device_key: str
    # samples[].addr. socket 을 쓰면 비운다
    addr: str = ""
    # sockets[].name. addr 를 쓰면 비운다
    socket: str = ""
    # sockets[].value 의 키
    field: str = ""

    _validate_kind = field_validator("kind")(_no_dot)
    _validate_device_key = field_validator("device_key")(_no_dot)

    @property
    def is_socket(self) -> bool:
        return bool(self.socket)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.device_key, self.addr)

    @property
    def socket_key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.device_key, self.socket, self.field)

    @model_validator(mode="after")
    def _one_ref(self) -> TagSource:
        has_addr = bool(self.addr)
        has_socket = bool(self.socket)
        if has_addr and has_socket:
            raise ValueError("source.addr 와 source.socket 을 같이 쓸 수 없습니다")
        if not has_addr and not has_socket:
            raise ValueError("source.addr 또는 source.socket 이 필요합니다")
        if has_socket and not self.field:
            raise ValueError("source.socket 이면 source.field 가 필요합니다")
        if self.field and not has_socket:
            raise ValueError("source.field 는 source.socket 과 같이 씁니다")
        return self


class TagTarget(BaseModel):
    # Objects 아래 객체 경로. 예) Factory/Line01/Press01
    path: str
    name: str
    data_type: OpcUaDataType
    unit: str = ""
    description: str = ""

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(s for s in self.path.split("/") if s)

    @model_validator(mode="after")
    def _check_path(self) -> TagTarget:
        if not self.segments:
            raise ValueError(f"target.path 가 비어 있습니다: {self.path!r}")
        if self.segments[0] == RESERVED_ROOT:
            raise ValueError(f"target.path 첫 세그먼트로 {RESERVED_ROOT} 를 쓸 수 없습니다")
        if not self.name:
            raise ValueError("target.name 이 비어 있습니다")
        return self


class TagConversion(BaseModel):
    scale: float = 1.0
    offset: float = 0.0

    def apply(self, value: object) -> object:
        """제공값 = 원본값 × scale + offset. 숫자가 아니면 그대로 둔다. 리스트는 원소마다 적용한다."""
        if isinstance(value, list):
            return [self.apply(item) for item in value]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        if self.scale == 1.0 and self.offset == 0.0:
            return value
        return value * self.scale + self.offset


class CollectorHeader(BaseModel):
    """ZeroMQ collector id → AAS 키. 샘플이 아니라 collector 한 대에 대한 헤더다."""

    kind: str
    device_key: str
    aas_id: str = ""
    global_asset_id: str = ""

    _validate_kind = field_validator("kind")(_no_dot)
    _validate_device_key = field_validator("device_key")(_no_dot)

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.device_key)

    @property
    def has_identification(self) -> bool:
        return bool(self.aas_id or self.global_asset_id)

    def identification_fields(self) -> tuple[tuple[str, str], ...]:
        """값이 있는 Identification 자식만. 빈 노드는 만들지 않는다."""
        fields: list[tuple[str, str]] = []
        if self.aas_id:
            fields.append(("AasId", self.aas_id))
        if self.global_asset_id:
            fields.append(("GlobalAssetId", self.global_asset_id))
        if self.device_key:
            fields.append(("DeviceKey", self.device_key))
        return tuple(fields)

    def object_segments(self, auto_root: str) -> tuple[str, ...]:
        """북향에서 이 collector 의 객체 경로. auto 노드와 같은 부모다."""
        return (auto_root, self.kind, self.device_key)


_HEADER_KEYS = {"kind", "device_key", "aas_id", "global_asset_id", "browse_root", "node_root", "tags"}


class CompactTag(BaseModel):
    """collector 블록 안의 짧은 태그. kind·device_key·경로 접두는 헤더가 채운다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    # browse_root 아래 경로/이름. node_root 가 있으면 NodeId 접미로도 쓴다
    node: str
    type: OpcUaDataType
    addr: str = ""
    socket: str = ""
    field: str = ""
    # None 이면 node_root 가 있을 때만 NodeId 를 만든다
    link: bool | None = None
    description: str = ""
    unit: str = ""
    enabled: bool = True
    scale: float = 1.0
    offset: float = 0.0

    @field_validator("id", "node")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("비어 있을 수 없습니다")
        return stripped


def expand_tag_map(data: dict) -> dict:
    """중첩 tags 를 평탄한 collectors + tags 로 펼친다. 맨 위 tags 는 그대로 둔다."""
    collectors_in = data.get("collectors") or []
    tags_out: list = list(data.get("tags") or [])
    collectors_out: list = []
    for raw in collectors_in:
        if not isinstance(raw, dict):
            collectors_out.append(raw)
            continue
        unknown = set(raw) - _HEADER_KEYS
        if unknown:
            who = f"{raw.get('kind', '?')}/{raw.get('device_key', '?')}"
            raise ValueError(f"collector {who} 에 알 수 없는 키: {sorted(unknown)}")
        block = {key: raw[key] for key in ("kind", "device_key", "aas_id", "global_asset_id") if key in raw}
        browse_root = str(raw.get("browse_root") or "").strip("/")
        node_root = str(raw.get("node_root") or "").rstrip("/")
        collectors_out.append(block)
        nested = raw.get("tags")
        if not nested:
            continue
        if not isinstance(nested, list):
            raise ValueError(f"collector {block.get('kind')}/{block.get('device_key')} tags 는 배열이어야 합니다")
        kind = str(block.get("kind") or "")
        device_key = str(block.get("device_key") or "")
        for item in nested:
            tags_out.append(
                _expand_compact(item, kind=kind, device_key=device_key, browse_root=browse_root, node_root=node_root)
            )
    return {"collectors": collectors_out, "tags": tags_out}


def _expand_compact(
    item: object, *, kind: str, device_key: str, browse_root: str, node_root: str
) -> dict:
    where = f"{kind}/{device_key}"
    try:
        tag = item if isinstance(item, CompactTag) else CompactTag.model_validate(item)
    except ValidationError as e:
        raise ValueError(f"{where} 태그 형식 오류: {e}") from e
    parts = tag.node.split("/")
    if len(parts) < 2 or any(not part for part in parts):
        raise ValueError(f"{where} {tag.id}: node 는 경로/이름 이어야 합니다 ({tag.node!r})")
    parent = "/".join(parts[:-1])
    path = f"{browse_root}/{parent}" if browse_root else parent
    link = bool(node_root) if tag.link is None else tag.link
    node_id = None
    if link:
        if not node_root:
            raise ValueError(f"{where} {tag.id}: node_root 없이 NodeId 를 만들 수 없습니다")
        node_id = f"{node_root}/{tag.node}"
    source: dict[str, str] = {"kind": kind, "device_key": device_key}
    if tag.socket:
        source["socket"] = tag.socket
        source["field"] = tag.field
    else:
        source["addr"] = tag.addr
    expanded: dict = {
        "tag_id": tag.id,
        "enabled": tag.enabled,
        "source": source,
        "target": {
            "path": path,
            "name": parts[-1],
            "data_type": tag.type,
            "unit": tag.unit,
            "description": tag.description,
        },
    }
    if node_id is not None:
        expanded["node_id"] = node_id
    if tag.scale != 1.0 or tag.offset != 0.0:
        expanded["conversion"] = {"scale": tag.scale, "offset": tag.offset}
    try:
        TagSettings.model_validate(expanded)
    except ValidationError as e:
        raise ValueError(f"{where} {tag.id}: {e}") from e
    return expanded


class TagSettings(BaseModel):
    tag_id: str
    # 있으면 북향 변수 NodeId. 정수면 i=<node_id>, 문자열이면 ns=2;s=... 또는 s=<값>. 없으면 s=<tag_id>
    node_id: int | str | None = None
    enabled: bool = True
    source: TagSource
    target: TagTarget
    conversion: TagConversion = Field(default_factory=TagConversion)

    @field_validator("node_id", mode="before")
    @classmethod
    def _node_id(cls, value: object) -> int | str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("node_id 는 bool 일 수 없습니다")
        if isinstance(value, int):
            if value < 1:
                raise ValueError("node_id 정수 값은 1 이상이어야 합니다")
            return value
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        raise ValueError(f"node_id 는 정수 또는 문자열이어야 합니다: {value!r}")


class TagMapFile(BaseModel):
    collectors: list[CollectorHeader] = Field(default_factory=list)
    tags: list[TagSettings] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _expand_nested(cls, data: object) -> object:
        if isinstance(data, dict):
            return expand_tag_map(data)
        return data

    @model_validator(mode="after")
    def _unique(self) -> TagMapFile:
        seen_collectors: set[tuple[str, str]] = set()
        for header in self.collectors:
            if header.key in seen_collectors:
                raise ValueError(f"중복된 collector: {header.kind}/{header.device_key}")
            seen_collectors.add(header.key)
            if header.has_identification and header.kind == RESERVED_ROOT:
                raise ValueError(f"collector.kind 로 {RESERVED_ROOT} 를 쓸 수 없습니다")

        seen_ids: set[str] = set()
        seen_node_ids: set[int | str] = set()
        seen_targets: set[tuple[tuple[str, ...], str]] = set()
        for tag in self.tags:
            if tag.tag_id in seen_ids:
                raise ValueError(f"중복된 tag_id: {tag.tag_id}")
            seen_ids.add(tag.tag_id)
            if tag.node_id is not None:
                if tag.node_id in seen_node_ids:
                    raise ValueError(f"중복된 node_id: {tag.node_id}")
                seen_node_ids.add(tag.node_id)
            if not tag.enabled:
                continue
            target_key = (tag.target.segments, tag.target.name)
            if target_key in seen_targets:
                raise ValueError(
                    f"같은 경로에 같은 이름의 태그: {tag.target.path}/{tag.target.name}"
                )
            seen_targets.add(target_key)
        return self


def identification_placements(
    collectors: list[CollectorHeader], auto_root: str
) -> list[tuple[tuple[str, ...], CollectorHeader]]:
    """collector 객체 아래에 Identification 을 한 번만 둔다."""
    placements: list[tuple[tuple[str, ...], CollectorHeader]] = []
    seen: set[tuple[str, ...]] = set()
    root = auto_root.strip() or "Collectors"
    if root.split("/")[0] == RESERVED_ROOT:
        return placements
    for header in collectors:
        if not header.has_identification:
            continue
        segments = header.object_segments(root)
        if segments in seen:
            continue
        seen.add(segments)
        placements.append((segments, header))
    return placements
