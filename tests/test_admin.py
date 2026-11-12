"""관리 페이지: .env 저장, 상태 스냅샷, HTTP."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from admin.env_file import (
    apply_updates,
    pending_restart,
    read_assignments,
    release_process_env,
    runtime_values,
    settings_view,
    validate_settings,
    write_updates,
)
from admin.logs import list_devices, tail_device
from admin.server import AdminServer
from admin.status import build_status
from forwarder import Forwarder, _SourceState
from model.app_model import AdminSettings, AppSettings, IpcLogSettings, NorthboundMode, Settings
from model.ipc_model import IpcMessage
from model.protocol_model import MsgTypeEnum, ProtocolHeaderDTO
from model.tag_model import CollectorHeader
from utils.message_log import MessageLog


def _settings() -> Settings:
    return Settings(
        app=AppSettings(
            northbound=NorthboundMode.OPCUA,
            ipc_log=IpcLogSettings(summary=False, file_enable=False),
            admin=AdminSettings(enable=True, host="127.0.0.1", port=9, token=""),
        ),
        collectors=[CollectorHeader(kind="robot", device_key="robot-1", aas_id="urn:aas")],
    )


def _forwarder(root: Path, settings: Settings | None = None) -> Forwarder:
    settings = settings or _settings()
    log = MessageLog(settings.app.ipc_log, root=root)
    ipc = SimpleNamespace(topics=["collector.robot."], is_open=True)
    forwarder = Forwarder(
        settings,
        ipc=ipc,  # type: ignore[arg-type]
        server=object(),  # type: ignore[arg-type]
        message_log=log,
    )
    forwarder._sources["robot/robot-1"] = _SourceState(last_data_mono=time.monotonic(), device_ok=True)
    forwarder._sources["plc/plc-1"] = _SourceState(device_ok=False, stale=True, reason="E-1001")
    return forwarder


def _form(settings: Settings, **overrides: object) -> dict:
    payload: dict[str, object] = dict(runtime_values(settings))
    payload.update(overrides)
    return payload


async def _request(port: int, method: str, path: str, body: bytes | None = None, token: str = "") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
    if token:
        head.append(f"X-Admin-Token: {token}")
    data = body or b""
    if body is not None:
        head.append("Content-Type: application/json")
        head.append(f"Content-Length: {len(data)}")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    await writer.wait_closed()
    status_line, _, rest = raw.partition(b"\r\n")
    _, code, *_ = status_line.decode().split(" ")
    _, _, payload = rest.partition(b"\r\n\r\n")
    return int(code), payload


class EnvFileTest(unittest.TestCase):
    def test_apply_keeps_comments_and_unrelated_keys(self) -> None:
        text = "# comment\nNORTHBOUND_MODE=opcua\nMQTT_PASSWORD=secret\nOTHER=1\n"
        updated = apply_updates(text, {"NORTHBOUND_MODE": "mqtt"})
        self.assertIn("# comment\n", updated)
        self.assertIn("NORTHBOUND_MODE=mqtt\n", updated)
        self.assertIn("MQTT_PASSWORD=secret\n", updated)
        self.assertIn("OTHER=1\n", updated)

    def test_apply_appends_missing_key_once(self) -> None:
        updated = apply_updates("OTHER=1\nZMQ_TOPIC_ROOT=old\nZMQ_TOPIC_ROOT=collector\n", {"ZMQ_TOPIC_ROOT": "edge"})
        self.assertEqual(updated.count("ZMQ_TOPIC_ROOT=edge\n"), 2)
        self.assertNotIn("old", updated)
        appended = apply_updates("OTHER=1", {"MQTT_QOS": "1"})
        self.assertTrue(appended.endswith("MQTT_QOS=1\n"))
        self.assertIn("OTHER=1\n", appended)

    def test_password_roundtrip_when_quoted(self) -> None:
        updated = apply_updates("", {"MQTT_PASSWORD": 'a b#c"'})
        self.assertEqual(read_assignments(updated)["MQTT_PASSWORD"], 'a b#c"')

    def test_validate_rejects_bad_mode_and_endpoint(self) -> None:
        settings = _settings()
        with self.assertRaises(ValueError):
            validate_settings(_form(settings, NORTHBOUND_MODE="both"))
        with self.assertRaises(ValueError):
            validate_settings(_form(settings, ZMQ_SUB_ENDPOINTS="http://127.0.0.1:5556"))

    def test_password_omitted_does_not_change_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MQTT_PASSWORD=secret\nOTHER=1\n", encoding="utf-8")
            write_updates(path, validate_settings(_form(_settings(), NORTHBOUND_MODE="mqtt")))
            text = path.read_text(encoding="utf-8")
            self.assertIn("MQTT_PASSWORD=secret\n", text)
            self.assertIn("OTHER=1\n", text)
            self.assertIn("NORTHBOUND_MODE=mqtt\n", text)

    def test_pending_ignores_mode_alias_and_hides_password(self) -> None:
        settings = _settings()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("NORTHBOUND_MODE=OPC-UA\nMQTT_PASSWORD=secret\n", encoding="utf-8")
            view = settings_view(path, settings)
            self.assertEqual(view["fields"]["NORTHBOUND_MODE"]["saved"], "opcua")
            self.assertFalse(view["fields"]["NORTHBOUND_MODE"]["pending"])
            self.assertTrue(view["password_set"])
            self.assertTrue(view["pending_restart"])
            self.assertNotIn("secret", json.dumps(view))

    def test_release_process_env_drops_only_file_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("ADMIN_TEST_ONLY=opcua\n", encoding="utf-8")
            os.environ["ADMIN_TEST_ONLY"] = "mqtt"
            os.environ["ADMIN_TEST_KEEP"] = "stay"
            try:
                release_process_env(path)
                self.assertNotIn("ADMIN_TEST_ONLY", os.environ)
                self.assertEqual(os.environ["ADMIN_TEST_KEEP"], "stay")
            finally:
                os.environ.pop("ADMIN_TEST_ONLY", None)
                os.environ.pop("ADMIN_TEST_KEEP", None)


class StatusAndLogTest(unittest.TestCase):
    def test_snapshot_merges_tag_map_and_live_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = build_status(_forwarder(Path(tmp)))
        keys = {(row["kind"], row["device_key"]) for row in status["sources"]}
        self.assertEqual(keys, {("robot", "robot-1"), ("plc", "plc-1")})
        robot = next(row for row in status["sources"] if row["device_key"] == "robot-1")
        self.assertTrue(robot["seen"])
        self.assertTrue(robot["in_tag_map"])
        self.assertEqual(robot["aas_id"], "urn:aas")
        plc = next(row for row in status["sources"] if row["device_key"] == "plc-1")
        self.assertFalse(plc["in_tag_map"])
        self.assertTrue(plc["stale"])
        self.assertEqual(status["subscriptions"], ["collector.robot."])
        self.assertTrue(status["zmq_open"])
        self.assertIsNone(status["mqtt_connected"])

    def test_recent_keeps_summary_without_file_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            forwarder = _forwarder(Path(tmp))
            forwarder.message_log.record(
                IpcMessage(
                    kind="robot",
                    topic="collector.robot.robot-1.data",
                    address="robot-1",
                    header=ProtocolHeaderDTO(
                        msg_id=uuid4(),
                        gateway_address="gw",
                        collector_address="robot-1",
                        msg_type=MsgTypeEnum.DATA,
                        timestamp_ms=1,
                        msg_body={"device_key": "robot-1", "samples": []},
                    ),
                )
            )
            recent = forwarder.message_log.recent()
            self.assertEqual(len(recent), 1)
            self.assertIn("DATA", recent[0]["text"])
            self.assertEqual(list_devices(forwarder.message_log.root), [])

    def test_tail_rejects_escape_and_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = root / "robot-1"
            day.mkdir()
            (day / "2026-11-09.log").write_text("short\n" + ("x" * 500) + "\n", encoding="utf-8")
            (root / "skip me").mkdir()
            self.assertEqual(list_devices(root), ["robot-1"])
            tailed = tail_device(root, "robot-1", 10)
            self.assertEqual(tailed["lines"][0], "short")
            self.assertTrue(tailed["lines"][1].endswith("…"))
            with self.assertRaises(ValueError):
                tail_device(root, "../.env", 10)
            self.assertFalse(pending_restart(root / "missing.env", _settings()))


class AdminHttpTest(unittest.IsolatedAsyncioTestCase):
    async def _serve(self, forwarder: Forwarder, env_path: Path, token: str = ""):
        forwarder.settings.app.admin.token = token
        stop = asyncio.Event()
        server = AdminServer(forwarder, stop, env_path=env_path, host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        await server.started.wait()
        return server, stop, task

    async def test_page_status_save_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            env_path.write_text("# keep\nOTHER=1\nMQTT_PASSWORD=secret\n", encoding="utf-8")
            forwarder = _forwarder(root)
            server, stop, task = await self._serve(forwarder, env_path)
            try:
                status, page = await _request(server.bound_port, "GET", "/")
                self.assertEqual(status, 200)
                self.assertIn("엣지 포워더".encode(), page)

                status, body = await _request(server.bound_port, "GET", "/api/status")
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["northbound"], "opcua")
                self.assertEqual(len(payload["sources"]), 2)

                form = _form(forwarder.settings, NORTHBOUND_MODE="mqtt")
                status, body = await _request(
                    server.bound_port,
                    "POST",
                    "/api/settings",
                    json.dumps(form).encode(),
                )
                self.assertEqual(status, 200, body)
                saved = env_path.read_text(encoding="utf-8")
                self.assertIn("NORTHBOUND_MODE=mqtt\n", saved)
                self.assertIn("MQTT_PASSWORD=secret\n", saved)
                self.assertIn("OTHER=1\n", saved)
                self.assertIn("# keep\n", saved)
                self.assertEqual(forwarder.settings.app.northbound, NorthboundMode.OPCUA)

                status, body = await _request(server.bound_port, "GET", "/api/settings")
                view = json.loads(body)
                self.assertTrue(view["pending_restart"])
                self.assertNotIn("secret", body.decode())

                status, _ = await _request(
                    server.bound_port,
                    "POST",
                    "/api/settings",
                    json.dumps(_form(forwarder.settings, ZMQ_SUB_ENDPOINTS="nope")).encode(),
                )
                self.assertEqual(status, 400)
                self.assertIn("NORTHBOUND_MODE=mqtt\n", env_path.read_text(encoding="utf-8"))

                status, body = await _request(server.bound_port, "POST", "/api/restart")
                self.assertEqual(status, 200)
                self.assertIn(b"restarting", body)
                await asyncio.wait_for(stop.wait(), timeout=2)
                self.assertTrue(server.restart)
            finally:
                stop.set()
                await task

    async def test_token_and_log_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = root / "robot-1"
            day.mkdir()
            (day / "2026-11-09.log").write_text("line\n", encoding="utf-8")
            forwarder = _forwarder(root)
            server, stop, task = await self._serve(forwarder, root / ".env", token="s3cret")
            try:
                status, _ = await _request(server.bound_port, "GET", "/api/logs")
                self.assertEqual(status, 401)
                status, body = await _request(server.bound_port, "GET", "/api/logs", token="s3cret")
                self.assertEqual(json.loads(body)["devices"], ["robot-1"])
                status, body = await _request(
                    server.bound_port,
                    "GET",
                    "/api/logs/tail?device=../.env",
                    token="s3cret",
                )
                self.assertEqual(status, 400)
                status, body = await _request(
                    server.bound_port,
                    "GET",
                    "/api/logs/tail?device=robot-1&lines=5",
                    token="s3cret",
                )
                self.assertEqual(json.loads(body)["lines"], ["line"])
            finally:
                stop.set()
                await task

    async def test_bind_failure_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forwarder = _forwarder(root)
            stop = asyncio.Event()
            first = AdminServer(forwarder, stop, env_path=root / ".env", host="127.0.0.1", port=0)
            first_task = asyncio.create_task(first.serve())
            await first.started.wait()
            second = AdminServer(forwarder, stop, env_path=root / ".env", host="127.0.0.1", port=first.bound_port)
            second_task = asyncio.create_task(second.serve())
            await second.started.wait()
            self.assertEqual(second.bound_port, 0)
            stop.set()
            await first_task
            await second_task
