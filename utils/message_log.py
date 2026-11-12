"""
IPC 수신 메시지 로그.

| 출력 | 위치 | 내용 |
|------|------|------|
| 요약 | 콘솔 (logger `ipc`, INFO) | 메시지당 한 줄: 타입, 수집원, 샘플/오류 수 또는 health 상태 |
| 전문 | {프로젝트 루트}/data/logs/{device_key}/{YYYY-MM-DD}.log | 수신 시각 + ProtocolHeaderDTO JSON 한 줄 |

device_key 는 토픽 collector.{kind}.{device_key}.{msg_type} 에서 가져온다.
보관 기간 정리는 이 프로세스가 기록한 device_key 폴더 안에서만 한다.
정리 시점: 기동 후 폴더를 처음 기록할 때(IPC_LOG_CLEAN_ON_START) + 매일 IPC_LOG_CLEAN_HOUR 시.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from config import BASE_DIR
from model.app_model import IpcLogSettings
from model.ipc_model import IpcMessage
from model.protocol_model import MsgTypeEnum

LOG_ROOT = BASE_DIR / "data" / "logs"
_RETRY_S = 60

logger = logging.getLogger(__name__)
summary_logger = logging.getLogger("ipc")

_DAY_LOG = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")
_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)
_RECENT_MAX = 200


@dataclass
class _DayFile:
    day: str
    stream: TextIO


def safe_device_name(raw: str) -> str:
    """로그 폴더 이름. 경로 이동 문자(/, ..)는 제거한다."""
    s = _UNSAFE.sub("_", (raw or "").strip()).strip("._")
    return s[:128] or "unknown"


def device_folder(msg: IpcMessage) -> str:
    return safe_device_name(msg.address or msg.device_key)


def summarize(msg: IpcMessage) -> str:
    body = msg.header.msg_body if isinstance(msg.header.msg_body, dict) else {}
    head = f"{msg.header.msg_type.value.upper()} {msg.collector_ref}"
    if msg.header.msg_type is MsgTypeEnum.DATA:
        samples = body.get("samples")
        samples = samples if isinstance(samples, list) else []
        errors = sum(1 for s in samples if isinstance(s, dict) and s.get("error"))
        return f"{head} samples={len(samples)} errors={errors}"
    if msg.header.msg_type is MsgTypeEnum.HEALTH:
        return f"{head} device_ok={body.get('device_ok')} reason={body.get('reason') or '-'}"
    return head


class MessageLog:
    def __init__(self, cfg: IpcLogSettings, *, root: Path = LOG_ROOT) -> None:
        self.cfg = cfg
        self.root = root
        # 쓰기 실패 시 이 시각(monotonic)까지 파일 기록을 쉬고 다시 시도한다
        self._suspended_until: float | None = None
        self._files: dict[str, _DayFile] = {}
        # 관리 페이지용 최근 요약. 파일·콘솔 설정과 무관하게 메모리에만 둔다
        self._recent: deque[tuple[str, str, str]] = deque(maxlen=_RECENT_MAX)
        # 이번 기동에서 기록한 device 폴더 (정리 대상)
        self._folders: set[str] = set()
        # clean_on_start: 처음 기록한 뒤 아직 정리하지 않은 폴더
        self._pending_folders: set[str] = set()
        self._next_clean = self._next_clean_time(datetime.now())

    def check_writable(self) -> bool:
        """기동 시 로그 루트에 쓸 수 있는지 확인하고 결과를 로그로 남긴다."""
        if not self.cfg.file_enable:
            logger.info("IPC 메시지 파일 기록 꺼짐 (IPC_LOG_FILE=false)")
            return False
        probe = self.root / f".write_test_{os.getpid()}"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            logger.error(
                "IPC 메시지 파일을 %s 에 쓸 수 없습니다: %s "
                "(예: sudo mkdir -p %s && sudo chown <실행 계정>: %s). %d초마다 다시 시도합니다",
                self.root, e, self.root, self.root, _RETRY_S,
            )
            return False
        logger.info("IPC 메시지 파일 기록: %s/{device_key}/{YYYY-MM-DD}.log", self.root)
        return True

    def record(self, msg: IpcMessage) -> None:
        text = summarize(msg)
        self._recent.append((datetime.now().isoformat(timespec="milliseconds"), device_folder(msg), text))
        if self.cfg.summary:
            summary_logger.info(text)
        if not self.cfg.file_enable:
            return
        if self._suspended_until is not None and time.monotonic() < self._suspended_until:
            return
        self._write(msg)

    def recent(self, limit: int = 40) -> list[dict[str, str]]:
        """최신이 앞. 관리 페이지 폴링용."""
        limit = max(1, min(_RECENT_MAX, limit))
        items = list(self._recent)[-limit:]
        items.reverse()
        return [{"time": t, "device": device, "text": text} for t, device, text in items]

    def maybe_cleanup(self, now: datetime | None = None) -> None:
        """기동 시(새 device 폴더) + 매일 IPC_LOG_CLEAN_HOUR 시에 보관 기간이 지난 파일을 지운다."""
        if not self.cfg.file_enable or self.cfg.retention_days <= 0:
            return
        now = now or datetime.now()
        folders: set[str] = set()
        if self._pending_folders:
            folders |= self._pending_folders
            self._pending_folders.clear()
        if now >= self._next_clean:
            folders |= self._folders
            self._next_clean = self._next_clean_time(now)
        if not folders:
            return
        deleted = self.cleanup(self.cfg.retention_days, folders)
        if deleted:
            logger.info("IPC 메시지 로그 %d개 삭제 (보관 %d일)", deleted, self.cfg.retention_days)

    def _next_clean_time(self, now: datetime) -> datetime:
        at = now.replace(hour=self.cfg.clean_hour, minute=0, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)

    def cleanup(self, retention_days: int, folders: set[str]) -> int:
        """지정한 device 폴더의 YYYY-MM-DD.log 만 대상으로 한다."""
        cutoff = date.today() - timedelta(days=max(1, retention_days) - 1)
        deleted = 0
        for folder in sorted(folders):
            directory = self.root / folder
            if not directory.is_dir():
                continue
            for path in directory.glob("*.log"):
                m = _DAY_LOG.match(path.name)
                if m is None:
                    continue
                try:
                    if date.fromisoformat(m.group(1)) < cutoff:
                        path.unlink(missing_ok=True)
                        deleted += 1
                except (ValueError, OSError) as e:
                    logger.debug("로그 삭제 실패: %s (%s)", path, e)
        return deleted

    def close(self) -> None:
        for f in self._files.values():
            try:
                f.stream.close()
            except OSError:
                pass
        self._files.clear()

    def _write(self, msg: IpcMessage) -> None:
        folder = device_folder(msg)
        day = date.today().isoformat()
        line = (
            f"{datetime.now().isoformat(timespec='milliseconds')} "
            f"[{msg.header.msg_type.value.upper()}] {msg.collector_ref} {msg.header.model_dump_json()}\n"
        )
        try:
            f = self._files.get(folder)
            if f is None or f.day != day:
                f = self._open(folder, day)
            f.stream.write(line)
        except OSError as e:
            # 권한·디스크 문제로 매 메시지마다 실패하지 않도록 잠시 쉬었다가 다시 시도한다
            self._suspended_until = time.monotonic() + _RETRY_S
            self.close()
            logger.error("IPC 메시지 파일 기록 실패 (%s): %s. %d초 뒤 다시 시도합니다", self.root / folder, e, _RETRY_S)
            return
        if self._suspended_until is not None:
            self._suspended_until = None
            logger.info("IPC 메시지 파일 기록 재개: %s", self.root / folder)

    def _open(self, folder: str, day: str) -> _DayFile:
        old = self._files.pop(folder, None)
        if old is not None:
            old.stream.close()
        directory = self.root / folder
        directory.mkdir(parents=True, exist_ok=True)
        f = _DayFile(day, open(directory / f"{day}.log", "a", encoding="utf-8", buffering=1))
        self._files[folder] = f
        if folder not in self._folders:
            self._folders.add(folder)
            if self.cfg.clean_on_start:
                self._pending_folders.add(folder)
        return f
