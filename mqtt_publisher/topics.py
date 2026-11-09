"""
MQTT 토픽 조립.

발행: {root}/{kind}/{device_key}/{name...}
  예) edge/robot/robot-1/S0004
      edge/robot/robot-1/hw/cpu_temperature
"""

from __future__ import annotations

from client.errors import ClientError
from model.error_model import ClientErrorCode

_FORBIDDEN = "+#\x00"


def _leaf(segment: str, label: str) -> str:
    if (
        not segment
        or segment in {".", ".."}
        or "/" in segment
        or any(ch in segment for ch in _FORBIDDEN)
    ):
        raise ClientError(
            ClientErrorCode.INVALID_CONFIG,
            f"MQTT 토픽 세그먼트 오류 ({label}): {segment!r}",
        )
    return segment


def value_topic(kind: str, device_key: str, *name: str, root: str = "edge") -> str:
    """값 토픽. name 은 addr, 또는 소켓 이름과 필드처럼 세그먼트를 이어서 넘긴다."""
    if not name:
        raise ClientError(ClientErrorCode.INVALID_CONFIG, "MQTT 토픽 이름이 비어 있습니다")
    segments = [_leaf(part, "root") for part in root.split("/") if part]
    segments.append(_leaf(kind, "kind"))
    segments.append(_leaf(device_key, "device_key"))
    segments.extend(_leaf(part, "name") for part in name)
    return "/".join(segments)


HEALTH_SEGMENT = "health"


def health_topic(kind: str, device_key: str, *, root: str = "edge") -> str:
    """수집원 상태. 값 토픽과 같은 접두사 아래 health 세그먼트."""
    return value_topic(kind, device_key, HEALTH_SEGMENT, root=root)


def require_topic(topic: str) -> str:
    """이미 조립된 토픽. 와일드카드·빈 세그먼트는 거절한다."""
    if not topic or topic.startswith("/") or topic.endswith("/") or "//" in topic:
        raise ClientError(ClientErrorCode.INVALID_CONFIG, f"MQTT 토픽 형식 오류: {topic!r}")
    for segment in topic.split("/"):
        _leaf(segment, "topic")
    return topic
