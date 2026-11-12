"""관리 페이지가 고칠 수 있는 .env 키. 주석과 나머지 키는 그대로 둔다."""

from __future__ import annotations

import os
import re
from pathlib import Path

from model.app_model import EmptySettingError, Settings, parse_northbound_mode
from model.tag_model import RESERVED_ROOT

BOOL_KEYS = (
    "ZMQ_SUB_BIND",
    "OPCUA_AUTO_CREATE_NODES",
    "MQTT_TLS",
    "MQTT_RETAIN",
)
INT_KEYS = (
    "SOURCE_STALE_MS",
    "OPCUA_UPDATE_PERIOD_MS",
    "MQTT_BROKER_PORT",
    "MQTT_QOS",
)
LIST_KEYS = (
    "ZMQ_SUB_ENDPOINTS",
    "ZMQ_COLLECTOR_KINDS",
    "ZMQ_DEVICE_KEYS",
)
# 화면이 항상 보내는 키. MQTT_PASSWORD 는 바꿀 때만 포함한다
FIELD_KEYS = (
    "NORTHBOUND_MODE",
    "SOURCE_STALE_MS",
    "ZMQ_SUB_ENDPOINTS",
    "ZMQ_SUB_BIND",
    "ZMQ_TOPIC_ROOT",
    "ZMQ_COLLECTOR_KINDS",
    "ZMQ_DEVICE_KEYS",
    "OPCUA_ENDPOINT",
    "OPCUA_UPDATE_PERIOD_MS",
    "OPCUA_AUTO_CREATE_NODES",
    "OPCUA_AUTO_ROOT",
    "MQTT_BROKER_HOST",
    "MQTT_BROKER_PORT",
    "MQTT_TOPIC_ROOT",
    "MQTT_QOS",
    "MQTT_TLS",
    "MQTT_RETAIN",
    "MQTT_CLIENT_ID",
    "MQTT_USERNAME",
)
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_DAY_MS = 86_400_000


class EnvError(ValueError):
    """화면으로 받은 설정이 파일에 쓸 수 없다."""


def runtime_values(settings: Settings) -> dict[str, str]:
    """지금 프로세스에 적용된 값. 비밀번호는 넣지 않는다."""
    app = settings.app
    return {
        "NORTHBOUND_MODE": app.northbound.value,
        "SOURCE_STALE_MS": str(app.source_stale_ms),
        "ZMQ_SUB_ENDPOINTS": ",".join(app.zmq.sub_endpoints),
        "ZMQ_SUB_BIND": _bool_text(app.zmq.sub_bind),
        "ZMQ_TOPIC_ROOT": app.zmq.topic_root,
        "ZMQ_COLLECTOR_KINDS": ",".join(app.zmq.collector_kinds),
        "ZMQ_DEVICE_KEYS": ",".join(app.zmq.device_keys),
        "OPCUA_ENDPOINT": app.opcua.endpoint,
        "OPCUA_UPDATE_PERIOD_MS": str(app.opcua.update_period_ms),
        "OPCUA_AUTO_CREATE_NODES": _bool_text(app.opcua.auto_create_nodes),
        "OPCUA_AUTO_ROOT": app.opcua.auto_root,
        "MQTT_BROKER_HOST": app.mqtt.host,
        "MQTT_BROKER_PORT": str(app.mqtt.port),
        "MQTT_TOPIC_ROOT": app.mqtt.topic_root,
        "MQTT_QOS": str(app.mqtt.qos),
        "MQTT_TLS": _bool_text(app.mqtt.tls),
        "MQTT_RETAIN": _bool_text(app.mqtt.retain),
        "MQTT_CLIENT_ID": app.mqtt.client_id,
        "MQTT_USERNAME": app.mqtt.username,
    }


def read_assignments(text: str) -> dict[str, str]:
    """KEY=value. 주석은 건너뛰고, 같은 키가 여러 줄이면 마지막 값을 쓴다."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        parsed = _assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        found[key] = value
    return found


def settings_view(path: Path, settings: Settings) -> dict:
    """폼에 채울 저장값과, 실행 중 값과 다른지."""
    disk = read_assignments(path.read_text(encoding="utf-8")) if path.is_file() else {}
    running = runtime_values(settings)
    fields: dict[str, dict[str, object]] = {}
    pending = False
    for key, live in running.items():
        if key in disk:
            saved = normalize(key, disk[key])
            changed = saved != live
        else:
            saved = live
            changed = False
        if changed:
            pending = True
        fields[key] = {"saved": saved, "running": live, "pending": changed}
    password = disk.get("MQTT_PASSWORD", settings.app.mqtt.password)
    if "MQTT_PASSWORD" in disk and disk["MQTT_PASSWORD"] != settings.app.mqtt.password:
        pending = True
    return {
        "fields": fields,
        "password_set": bool(password),
        "pending_restart": pending,
    }


def pending_restart(path: Path, settings: Settings) -> bool:
    return bool(settings_view(path, settings)["pending_restart"])


def validate_settings(payload: object) -> dict[str, str]:
    """JSON 객체를 .env 에 쓸 문자열로 검사한다."""
    if not isinstance(payload, dict):
        raise EnvError("JSON 객체여야 합니다")
    unknown = set(payload) - set(FIELD_KEYS) - {"MQTT_PASSWORD"}
    if unknown:
        names = ", ".join(sorted(unknown))
        raise EnvError(f"바꿀 수 없는 키입니다: {names}")
    missing = [key for key in FIELD_KEYS if key not in payload]
    if missing:
        raise EnvError(f"빠진 항목이 있습니다: {', '.join(missing)}")

    updates = {
        "NORTHBOUND_MODE": _mode(payload["NORTHBOUND_MODE"]),
        "SOURCE_STALE_MS": _int_text(payload["SOURCE_STALE_MS"], "SOURCE_STALE_MS", 0, _DAY_MS),
        "ZMQ_SUB_ENDPOINTS": _endpoints(payload["ZMQ_SUB_ENDPOINTS"]),
        "ZMQ_SUB_BIND": _bool_text(_as_bool(payload["ZMQ_SUB_BIND"], "ZMQ_SUB_BIND")),
        "ZMQ_TOPIC_ROOT": _segment(payload["ZMQ_TOPIC_ROOT"], "ZMQ_TOPIC_ROOT"),
        "ZMQ_COLLECTOR_KINDS": _csv(payload["ZMQ_COLLECTOR_KINDS"], "ZMQ_COLLECTOR_KINDS"),
        "ZMQ_DEVICE_KEYS": _csv(payload["ZMQ_DEVICE_KEYS"], "ZMQ_DEVICE_KEYS"),
        "OPCUA_ENDPOINT": _opcua_endpoint(payload["OPCUA_ENDPOINT"]),
        "OPCUA_UPDATE_PERIOD_MS": _int_text(
            payload["OPCUA_UPDATE_PERIOD_MS"], "OPCUA_UPDATE_PERIOD_MS", 0, _DAY_MS
        ),
        "OPCUA_AUTO_CREATE_NODES": _bool_text(
            _as_bool(payload["OPCUA_AUTO_CREATE_NODES"], "OPCUA_AUTO_CREATE_NODES")
        ),
        "OPCUA_AUTO_ROOT": _auto_root(payload["OPCUA_AUTO_ROOT"]),
        "MQTT_BROKER_HOST": _host(payload["MQTT_BROKER_HOST"]),
        "MQTT_BROKER_PORT": _int_text(payload["MQTT_BROKER_PORT"], "MQTT_BROKER_PORT", 1, 65535),
        "MQTT_TOPIC_ROOT": _mqtt_root(payload["MQTT_TOPIC_ROOT"]),
        "MQTT_QOS": _int_text(payload["MQTT_QOS"], "MQTT_QOS", 0, 2),
        "MQTT_TLS": _bool_text(_as_bool(payload["MQTT_TLS"], "MQTT_TLS")),
        "MQTT_RETAIN": _bool_text(_as_bool(payload["MQTT_RETAIN"], "MQTT_RETAIN")),
        "MQTT_CLIENT_ID": _client_id(payload["MQTT_CLIENT_ID"]),
        "MQTT_USERNAME": _optional_line(payload["MQTT_USERNAME"], "MQTT_USERNAME"),
    }
    if "MQTT_PASSWORD" in payload and payload["MQTT_PASSWORD"] is not None:
        updates["MQTT_PASSWORD"] = _password(payload["MQTT_PASSWORD"])
    return updates


def apply_updates(text: str, updates: dict[str, str]) -> str:
    """있는 키는 그 자리에서 바꾸고, 없는 키만 파일 끝에 추가한다."""
    if text and not text.endswith("\n"):
        text += "\n"
    found: set[str] = set()
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        parsed = _assignment(line)
        if parsed is None or parsed[0] not in updates:
            out.append(line if line.endswith("\n") else line + "\n")
            continue
        key = parsed[0]
        out.append(_format_line(key, updates[key]))
        found.add(key)
    missing = [key for key in updates if key not in found]
    if missing:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        for key in missing:
            out.append(_format_line(key, updates[key]))
    return "".join(out)


def release_process_env(path: Path) -> None:
    """재실행 전에 호출한다. 파일에 있는 키를 프로세스 환경에서 지워 다음 기동이 파일을 다시 읽게 한다."""
    if not path.is_file():
        return
    for key in read_assignments(path.read_text(encoding="utf-8")):
        os.environ.pop(key, None)


def write_updates(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    new = apply_updates(text, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    mode = path.stat().st_mode & 0o777 if path.is_file() else 0o600
    try:
        tmp.write_text(new, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def normalize(key: str, value: str) -> str:
    """파일에 적힌 표기를 실행 중 값과 비교할 수 있게 맞춘다."""
    try:
        if key == "NORTHBOUND_MODE":
            return parse_northbound_mode(value).value
        if key in BOOL_KEYS:
            return _bool_text(_as_bool(value, key))
        if key in INT_KEYS:
            return str(int(str(value).strip()))
        if key in LIST_KEYS:
            return ",".join(part.strip() for part in str(value).split(",") if part.strip())
    except (EnvError, EmptySettingError, ValueError):
        return str(value).strip()
    return str(value).strip()


def _assignment(line: str) -> tuple[str, str] | None:
    body = line.strip()
    if not body or body.startswith("#") or "=" not in body:
        return None
    key, _, raw = body.partition("=")
    key = key.strip()
    if not key or " " in key:
        return None
    return key, _parse_value(raw)


def _parse_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        end = _quoted_end(raw)
        inner = raw[1:end]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def _quoted_end(raw: str) -> int:
    escaped = False
    for index, char in enumerate(raw[1:], start=1):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return index
    return len(raw)


def _format_line(key: str, value: str) -> str:
    if key == "MQTT_PASSWORD" and any(char in value for char in ' #\t"\''):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"\n'
    return f"{key}={value}\n"


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _as_bool(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off", ""):
            return False
    raise EnvError(f"{key} 는 true 또는 false 입니다")


def _as_text(value: object, key: str) -> str:
    if isinstance(value, bool) or value is None:
        raise EnvError(f"{key} 값이 없습니다")
    if isinstance(value, (int, float)):
        return str(int(value))
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise EnvError(f"{key} 에 줄바꿈을 넣을 수 없습니다")
        return value.strip()
    raise EnvError(f"{key} 값이 없습니다")


def _mode(value: object) -> str:
    try:
        return parse_northbound_mode(_as_text(value, "NORTHBOUND_MODE")).value
    except EmptySettingError as exc:
        raise EnvError("NORTHBOUND_MODE 는 opcua 또는 mqtt 입니다") from exc
    except ValueError as exc:
        raise EnvError(str(exc)) from exc


def _int_text(value: object, key: str, low: int, high: int) -> str:
    raw = value.strip() if isinstance(value, str) else value
    try:
        number = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise EnvError(f"{key} 는 정수입니다") from exc
    if isinstance(value, bool) or not low <= number <= high:
        raise EnvError(f"{key} 는 {low}~{high} 입니다")
    return str(number)


def _endpoints(value: object) -> str:
    parts = _split_list(_as_text(value, "ZMQ_SUB_ENDPOINTS"))
    if not parts:
        raise EnvError("ZMQ_SUB_ENDPOINTS 가 비어 있습니다")
    for part in parts:
        scheme, sep, rest = part.partition("://")
        if scheme not in ("tcp", "ipc") or not sep or not rest:
            raise EnvError(f"엔드포인트는 tcp:// 또는 ipc:// 로 시작합니다: {part}")
    return ",".join(parts)


def _csv(value: object, key: str) -> str:
    parts = _split_list(_as_text(value, key))
    for part in parts:
        if _SEGMENT.fullmatch(part) is None:
            raise EnvError(f"{key} 항목은 영문, 숫자, _, - 만 씁니다: {part}")
    return ",".join(parts)


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


def _segment(value: object, key: str) -> str:
    text = _as_text(value, key)
    if _SEGMENT.fullmatch(text) is None:
        raise EnvError(f"{key} 는 영문, 숫자, _, - 만 씁니다")
    return text


def _mqtt_root(value: object) -> str:
    text = _as_text(value, "MQTT_TOPIC_ROOT")
    parts = text.split("/")
    if not parts or any(_SEGMENT.fullmatch(part) is None for part in parts):
        raise EnvError("MQTT_TOPIC_ROOT 에 와일드카드나 공백을 넣을 수 없습니다")
    return text


def _auto_root(value: object) -> str:
    text = _as_text(value, "OPCUA_AUTO_ROOT")
    if not text or "/" in text or text == RESERVED_ROOT:
        raise EnvError(f"OPCUA_AUTO_ROOT 로 {RESERVED_ROOT} 나 슬래시를 쓸 수 없습니다")
    return text


def _opcua_endpoint(value: object) -> str:
    text = _as_text(value, "OPCUA_ENDPOINT")
    if not text.startswith("opc.tcp://") or len(text) <= len("opc.tcp://"):
        raise EnvError("OPCUA_ENDPOINT 는 opc.tcp:// 로 시작합니다")
    return text


def _host(value: object) -> str:
    text = _as_text(value, "MQTT_BROKER_HOST")
    if not text or any(char.isspace() or char in "/\\" for char in text):
        raise EnvError("MQTT_BROKER_HOST 가 비어 있거나 경로를 포함하고 있습니다")
    return text


def _client_id(value: object) -> str:
    text = _as_text(value, "MQTT_CLIENT_ID")
    if not text or len(text) > 128:
        raise EnvError("MQTT_CLIENT_ID 는 1~128자입니다")
    return text


def _optional_line(value: object, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise EnvError(f"{key} 에 줄바꿈을 넣을 수 없습니다")
        return value.strip()
    raise EnvError(f"{key} 값이 없습니다")


def _password(value: object) -> str:
    if not isinstance(value, str):
        raise EnvError("MQTT_PASSWORD 는 문자열이어야 합니다")
    if "\n" in value or "\r" in value:
        raise EnvError("MQTT_PASSWORD 에 줄바꿈을 넣을 수 없습니다")
    return value
