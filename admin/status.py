"""포워더 메모리의 수집원·북향 상태를 화면용 dict 로 만든다."""

from __future__ import annotations

import time


def build_status(forwarder) -> dict:
    app = forwarder.settings.app
    now = time.monotonic()
    live: dict[tuple[str, str], dict] = {}
    for sid, state in forwarder._sources.items():
        kind, _, device_key = sid.partition("/")
        age = None
        if state.last_data_mono is not None:
            age = max(0, int((now - state.last_data_mono) * 1000))
        live[(kind, device_key)] = {
            "kind": kind,
            "device_key": device_key,
            "device_ok": state.device_ok,
            "stale": state.stale,
            "reason": state.reason,
            "last_data_age_ms": age,
            "seen": True,
        }

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for header in forwarder.settings.collectors:
        key = (header.kind, header.device_key)
        seen.add(key)
        row = dict(live.get(key) or _unseen(header.kind, header.device_key))
        row["aas_id"] = header.aas_id
        row["in_tag_map"] = True
        rows.append(row)
    for key, row in live.items():
        if key in seen:
            continue
        row = dict(row)
        row["aas_id"] = ""
        row["in_tag_map"] = False
        rows.append(row)
    rows.sort(key=lambda row: (row["kind"], row["device_key"]))

    tag_map = forwarder.settings.tag_map_file
    ipc = forwarder.ipc
    return {
        "northbound": app.northbound.value,
        "uptime_s": max(0, int(now - forwarder.started_mono)),
        "messages_received": forwarder.messages_received,
        "messages_invalid": forwarder.messages_invalid,
        "zmq_open": _flag(ipc, "is_open"),
        "endpoints": list(app.zmq.sub_endpoints),
        "subscriptions": list(getattr(ipc, "topics", []) or []),
        "opcua_endpoint": app.opcua.endpoint,
        "mqtt_broker": f"{app.mqtt.host}:{app.mqtt.port}",
        "mqtt_topic": f"{app.mqtt.topic_root}/{{kind}}/{{device_key}}/{{name}}",
        "mqtt_connected": _mqtt_connected(forwarder),
        "tag_map": tag_map.name if tag_map is not None else None,
        "tag_count": len(forwarder.settings.tags),
        "sources": rows,
    }


def _unseen(kind: str, device_key: str) -> dict:
    return {
        "kind": kind,
        "device_key": device_key,
        "device_ok": None,
        "stale": False,
        "reason": "",
        "last_data_age_ms": None,
        "seen": False,
    }


def _flag(obj, name: str) -> bool:
    try:
        return bool(getattr(obj, name))
    except Exception:
        return False


def _mqtt_connected(forwarder) -> bool | None:
    mqtt = getattr(forwarder, "mqtt", None)
    if mqtt is None:
        return None
    try:
        return bool(mqtt.is_connected())
    except Exception:
        return False
