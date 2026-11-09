"""프로젝트 진입점. IPC 수신 → 북향 OPC UA 또는 MQTT 루프를 시작한다."""

import asyncio
import logging
import sys

from config import settings
from forwarder import run as run_forwarder
from utils.logging_setup import setup_logging

logger = logging.getLogger("main")


def _asyncio_run(coro):
    # pyzmq asyncio 는 Windows Proactor 루프에서 add_reader 가 없어 selector 스레드를 따로 띄운다
    if sys.platform == "win32" and sys.version_info >= (3, 12):
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


def main():
    setup_logging()
    logger.info("middleware_edge_forwarder 시작 (northbound=%s)", settings.app.northbound.value)
    exit_code = 0
    try:
        _asyncio_run(run_forwarder(settings))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        logger.error("북향 OPC UA 서버를 시작할 수 없습니다 (OPCUA_ENDPOINT 포트 확인): %s", e)
        exit_code = 1
    finally:
        logger.info("middleware_edge_forwarder 종료")
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
