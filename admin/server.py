"""포워더와 같은 asyncio 루프에서 여는 관리 페이지."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from admin.env_file import EnvError, pending_restart, settings_view, validate_settings, write_updates
from admin.logs import list_devices, tail_device
from admin.status import build_status

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = ROOT / ".env"
_PAGE_PATH = Path(__file__).resolve().parent / "page.html"
_MAX_BODY = 64 * 1024

_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Error",
}


class AdminServer:
    """상태 조회, .env 저장, 프로세스 재시작 요청. 바인드 실패로 데이터 경로를 멈추지 않는다."""

    def __init__(self, forwarder, stop: asyncio.Event, *, env_path: Path | None = None, host: str | None = None, port: int | None = None):
        cfg = forwarder.settings.app.admin
        self.forwarder = forwarder
        self.stop = stop
        self.env_path = env_path or DEFAULT_ENV
        self.host = cfg.host if host is None else host
        self.port = cfg.port if port is None else port
        self.token = cfg.token
        self.restart = False
        self.started = asyncio.Event()
        self.bound_port = 0
        self._server: asyncio.Server | None = None

    async def serve(self) -> None:
        try:
            try:
                self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=65536)
            except OSError as exc:
                logger.error(
                    "관리 페이지를 열 수 없습니다 (%s:%s): %s. 데이터 경로는 계속합니다",
                    self.host,
                    self.port,
                    exc,
                )
            else:
                sockaddr = self._server.sockets[0].getsockname()
                self.bound_port = int(sockaddr[1])
                shown = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
                logger.info("관리 페이지 http://%s:%s/", shown, self.bound_port)
                if self.host in ("0.0.0.0", "::") and not self.token:
                    logger.warning("관리 페이지가 모든 인터페이스에 열려 있고 ADMIN_TOKEN 이 비어 있습니다")
        finally:
            self.started.set()
        try:
            await self.stop.wait()
        finally:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        do_restart = False
        try:
            try:
                parsed = await asyncio.wait_for(_read_request(reader), timeout=20)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
                writer.write(_response(400, {"error": "요청을 읽지 못했습니다"}))
                return
            if parsed is None:
                writer.write(_response(400, {"error": "요청을 읽지 못했습니다"}))
                return
            method, target, headers, body = parsed
            status, payload = self._dispatch(method, target, headers, body)
            writer.write(_response(status, payload))
            await writer.drain()
            path = urlsplit(target).path
            path = path if path == "/" else path.rstrip("/")
            do_restart = status == 200 and path == "/api/restart"
        except Exception:
            logger.exception("관리 요청 처리 실패")
            try:
                writer.write(_response(500, {"error": "요청을 처리하지 못했습니다"}))
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if do_restart:
                self.restart = True
                self.stop.set()

    def _dispatch(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, object]:
        parts = urlsplit(target)
        path = parts.path if parts.path == "/" else parts.path.rstrip("/")
        if method == "GET" and path == "/":
            return 200, _PAGE_PATH.read_bytes()
        if path.startswith("/api/") and not _authorized(self.token, headers):
            return 401, {"error": "인증이 필요합니다", "token_required": True}
        query = parse_qs(parts.query)
        if path == "/api/status" and method == "GET":
            payload = build_status(self.forwarder)
            payload["pending_restart"] = pending_restart(self.env_path, self.forwarder.settings)
            payload["recent"] = self.forwarder.message_log.recent(40)
            return 200, payload
        if path == "/api/settings" and method == "GET":
            return 200, settings_view(self.env_path, self.forwarder.settings)
        if path == "/api/settings" and method == "POST":
            return self._save(body)
        if path == "/api/restart" and method == "POST":
            return 200, {"ok": True, "restarting": True}
        if path == "/api/logs" and method == "GET":
            return 200, {"devices": list_devices(self.forwarder.message_log.root)}
        if path == "/api/logs/tail" and method == "GET":
            return self._tail(query)
        if path.startswith("/api/"):
            return 404, {"error": "없는 경로입니다"}
        return 404, {"error": "없는 경로입니다"}

    def _save(self, body: bytes) -> tuple[int, dict]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
            updates = validate_settings(payload)
            write_updates(self.env_path, updates)
        except EnvError as exc:
            return 400, {"error": str(exc)}
        except (json.JSONDecodeError, UnicodeError):
            return 400, {"error": "JSON 이 올바르지 않습니다"}
        except OSError:
            logger.exception("관리 설정 저장 실패")
            return 500, {"error": ".env 를 저장하지 못했습니다"}
        return 200, {"ok": True, "restart_required": True}

    def _tail(self, query: dict[str, list[str]]) -> tuple[int, dict]:
        device = (query.get("device") or [""])[0]
        raw_lines = (query.get("lines") or ["40"])[0]
        try:
            lines = int(raw_lines)
        except ValueError:
            lines = 40
        try:
            return 200, tail_device(self.forwarder.message_log.root, device, lines)
        except ValueError as exc:
            return 400, {"error": str(exc)}


def _authorized(expected: str, headers: dict[str, str]) -> bool:
    if not expected:
        return True
    got = headers.get("x-admin-token", "")
    return hmac.compare_digest(got, expected)


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes] | None:
    head = await reader.readuntil(b"\r\n\r\n")
    text = head.decode("latin1")
    lines = text.split("\r\n")
    request = lines[0].split(" ")
    if len(request) < 2:
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0") or "0")
    except ValueError as exc:
        raise ValueError("content-length") from exc
    if length < 0 or length > _MAX_BODY:
        raise ValueError("body")
    body = await reader.readexactly(length) if length else b""
    return request[0].upper(), request[1], headers, body


def _response(status: int, payload: object) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        body = bytes(payload)
        content_type = "text/html; charset=utf-8"
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    reason = _REASONS.get(status, "Error")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "\r\n"
    )
    return head.encode("ascii") + body
