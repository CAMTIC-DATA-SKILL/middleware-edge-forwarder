"""
collector-plc 와 같은 와이어로 DATA/HEALTH 를 PUB 하는 가짜 collector.

사용 (프로젝트 루트에서):
    python tester/fake_collector.py
    python tester/fake_collector.py --device-key plc-sim-1 --period-ms 500
    python tester/fake_collector.py --count 10 --health-fail   # 10회 송신 후 device_ok=false

미들웨어 SUB(tcp://127.0.0.1:5556)가 열린 뒤에 실행한다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from uuid import uuid4

import zmq


def now_ms() -> int:
    return int(time.time() * 1000)


def header(device_key: str, msg_type: str, body: dict) -> dict:
    return {
        "msg_id": str(uuid4()),
        "gateway_address": "gateway",
        "collector_address": device_key,
        "msg_type": msg_type,
        "msg_body": body,
        "timestamp_ms": now_ms(),
    }


def data_body(device_key: str, tick: int) -> dict:
    return {
        "device_key": device_key,
        "sample_ms": now_ms(),
        "samples": [
            {"addr": "D100", "type": "word", "value": int(250 + 50 * math.sin(tick / 5)), "error": None},
            {"addr": "D101~102", "type": "int", "value": 100000 + tick, "error": None},
            {"addr": "D800.0", "type": "bit", "value": tick % 2, "error": None},
            {"addr": "D200~204", "type": "ascii", "value": "LOT-A001", "error": None},
            {"addr": "D9999", "type": "word", "value": None, "error": "E-2203"},
        ],
    }


def health_body(device_key: str, ok: bool, reason: str | None = None) -> dict:
    return {
        "device_key": device_key,
        "ipc_ok": True,
        "device_ok": ok,
        "reason": reason,
        "observed_ms": now_ms(),
    }


def send(sock: zmq.Socket, kind: str, device_key: str, msg_type: str, body: dict) -> None:
    topic = f"collector.{kind}.{device_key}.{msg_type}"
    payload = json.dumps(header(device_key, msg_type, body), ensure_ascii=False)
    sock.send_multipart([topic.encode("utf-8"), payload.encode("utf-8")])
    print(f"[{topic}] {payload}")


def main() -> None:
    parser = argparse.ArgumentParser(description="fake collector PUB")
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:5556", help="미들웨어 SUB 주소. PUB 이 connect 한다")
    parser.add_argument("--kind", default="plc")
    parser.add_argument("--device-key", default="plc-sim-1")
    parser.add_argument("--period-ms", type=int, default=1000)
    parser.add_argument("--count", type=int, default=0, help="DATA 송신 횟수 (0 = 무한)")
    parser.add_argument("--health-fail", action="store_true", help="종료 직전 device_ok=false HEALTH 송신")
    args = parser.parse_args()

    sock = zmq.Context.instance().socket(zmq.PUB)
    sock.setsockopt(zmq.LINGER, 1000)
    sock.connect(args.endpoint)
    print(f"PUB connect={args.endpoint} kind={args.kind} device_key={args.device_key} (Ctrl+C 종료)")
    # PUB slow joiner: SUB 가 붙을 시간을 준다
    time.sleep(1.0)

    tick = 0
    try:
        send(sock, args.kind, args.device_key, "health", health_body(args.device_key, True))
        while args.count <= 0 or tick < args.count:
            send(sock, args.kind, args.device_key, "data", data_body(args.device_key, tick))
            tick += 1
            time.sleep(args.period_ms / 1000.0)
        if args.health_fail:
            send(sock, args.kind, args.device_key, "health", health_body(args.device_key, False, "E-1001"))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstop")
    finally:
        sock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
