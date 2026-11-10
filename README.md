# middleware_edge_forwarder

엣지에서 collector(`collector-plc`, `collector-*`)가 수집한 데이터를 IPC로 넘겨받아 북향으로 상위 시스템(MES·SCADA 등)에 제공하는 프로세스입니다. 북향은 `NORTHBOUND_MODE`에 따라 **OPC UA 서버** 또는 **MQTT publisher** 중 하나입니다.

IPC 방식은 아직 확정 전이며, 현재는 collector 레포들이 이미 쓰고 있는 **ZeroMQ PUB/SUB** 기준으로 구현되어 있습니다.

## 전체 구조

```mermaid
flowchart LR
    PLC[PLC / 로봇] --> C1[collector-plc]
    PLC --> C2[collector-*]
    C1 -- "ZeroMQ PUB<br>collector.plc.{device_key}.{data|health|ack}" --> F[middleware_edge_forwarder<br>ZeroMQ SUB]
    C2 -- "collector.{kind}.…" --> F
    C1 -- 같은 PUB --> E[embedded_ingest<br>DB·로그·명령]
    F -- "NORTHBOUND_MODE<br>opcua 또는 mqtt" --> U[상위 시스템]
```

- collector는 PUB 소켓을 **bind**, forwarder는 SUB로 **connect** 합니다. ZeroMQ PUB은 여러 SUB에 동시에 전달하므로 `embedded_ingest`와 나란히 붙을 수 있고, collector 쪽 설정은 바꾸지 않아도 됩니다.
- forwarder는 `data`, `health`만 사용합니다. `ack`는 수신 후 버리고, `cmd_r`/`cmd_w`는 보내지 않습니다 (명령 경로는 `embedded_ingest` 담당).

## 구성

```
main.py                    # 진입점: logging → forwarder.run
config.py                  # .env + tag map JSON → settings
forwarder.py               # IPC 수신 → 북향 OPC UA 반영 루프 (DATA/HEALTH 처리, 수신 공백 감시)
client/errors.py           # ClientError (collector 레포와 동일)
model/
  app_model.py             # AppSettings / ZmqSettings / OpcUaServerSettings / MqttPublisherSettings
  error_model.py           # ClientErrorCode (collector 와이어 코드 체계)
  protocol_model.py        # ProtocolHeaderDTO, MsgDataDTO, MsgHealthDTO (수신 전용)
  ipc_model.py             # IpcMessage: 전송 방식과 무관한 수신 단위
  tag_model.py             # tag map 스키마
zeromq_client/
  base_client.py           # asyncio 소켓 수명주기
  sub_client.py            # [topic, json] multipart → IpcMessage
  topics.py                # 토픽 조립·해석
opcua_server/
  server.py                # NorthboundServer: 노드 생성, 값·품질 갱신, _Forwarder 상태 노드
  values.py                # 형식 변환, 샘플 type → OPC UA 형식, 에러 코드 → StatusCode
mqtt_publisher/
  publisher.py             # MqttPublisher: 브로커 connect / publish / close (기본 구성)
  topics.py                # {root}/{kind}/{device_key}/{name} 조립
tester/
  fake_collector.py        # collector-plc 와 같은 와이어로 DATA/HEALTH PUB
  opcua_reader.py          # 북향 주소 공간 값·품질 출력
admin/
  server.py                # 같은 프로세스의 관리 페이지 (상태, .env 저장, 재시작)
  page.html                # 화면
utils/
  logging_setup.py         # 콘솔 로깅
  message_log.py           # IPC 수신 메시지 요약(콘솔) + 전문(일별 파일)
tag_map.example.json       # tag map 예시 (tag_map.json 으로 복사)
systemd/, service_add.sh   # 서비스 등록
```

IPC가 바뀌면 `IpcMessage`를 돌려주는 클라이언트(`recv()`)만 새로 만들면 되고, `forwarder.py`의 DATA/HEALTH 처리와 OPC UA 쪽은 그대로 씁니다.

## 환경 준비

Python 3.11 이상 (collector-plc와 동일).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp tag_map.example.json tag_map.json   # 선택. 없으면 auto 노드만 사용
```

## 실행

```bash
python main.py
```

### 엔드포인트 맞추기

| 역할 | collector-plc (.env) | middleware_edge_forwarder (.env) |
| --- | --- | --- |
| DATA/HEALTH 전달 | `ZMQ_PUB_ENDPOINT=tcp://127.0.0.1:5556` + `ZMQ_PUB_BIND=true` | `ZMQ_SUB_ENDPOINTS=tcp://127.0.0.1:5556` + `ZMQ_SUB_BIND=false` |

collector가 여러 개(포트가 다름)라면 `ZMQ_SUB_ENDPOINTS`에 쉼표로 모두 적습니다. SUB 소켓 하나가 전부에 connect 합니다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `ZMQ_COLLECTOR_KINDS` | `plc` | 구독할 collector 종류. 비우면 `collector.*` 전체 |
| `ZMQ_DEVICE_KEYS` | (빈 값) | 특정 `device_key`만 받을 때 |
| `NORTHBOUND_MODE` | (필수) | `opcua` 또는 `mqtt`. 비어 있으면 로그 후 종료 |
| `OPCUA_ENDPOINT` | `opc.tcp://0.0.0.0:4840/edge/` | `opcua` 모드의 북향 서버 주소 |
| `OPCUA_NAMESPACE_URI` | `urn:middleware:edge:forwarder:tags` | 상위 시스템은 이 URI로 네임스페이스 번호를 찾습니다 |
| `OPCUA_AUTO_CREATE_NODES` | `true` | tag map에 없는 샘플도 자동 노출 |
| `OPCUA_UPDATE_PERIOD_MS` | `0` | 북향 노드 값 반영 주기. collector가 더 자주 보내도 이 간격으로만 갱신 (0 = 수신 즉시) |
| `SOURCE_STALE_MS` | `10000` | 이 시간 동안 DATA가 없으면 해당 수집원 태그를 Uncertain 처리 (0 = 끔) |

전체 목록은 `.env.example`을 참고합니다.

### 북향 모드

`NORTHBOUND_MODE` 로 둘 중 하나만 고릅니다. 값이 비어 있으면 기본값으로 바꾸지 않고 `NORTHBOUND_MODE 가 비어 있습니다` 를 로그한 뒤 종료합니다.

| 모드 | 동작 |
| --- | --- |
| `opcua` | OPC UA 서버를 열고 값을 노드에 쓴다. MQTT 브로커에는 연결하지 않는다 |
| `mqtt` | 브로커로만 발행한다. OPC UA 서버는 열지 않는다 |

MQTT 토픽은 `{MQTT_TOPIC_ROOT}/{kind}/{device_key}/{name}` 입니다. 샘플은 주소, 소켓은 이름과 필드입니다. 수집원 상태는 마지막 세그먼트가 `health` 입니다.

```text
edge/robot/robot-1/S0004
edge/robot/robot-1/hw/cpu_temperature
edge/robot/robot-1/health
```

tag map 에 같은 소스가 있으면 첫 변환(scale/offset)을 적용한 값을 보냅니다. 매핑이 없는 샘플도 발행합니다. `OPCUA_UPDATE_PERIOD_MS` 는 두 모드 모두에서 값 반영 주기를 정합니다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` | `127.0.0.1` / `1883` | `mqtt` 모드의 브로커 주소 |
| `MQTT_TOPIC_ROOT` | `edge` | 토픽 접두사 |
| `MQTT_QOS` | `0` | `0`, `1`, `2` |
| `MQTT_TLS` | `false` | `true` 이면 시스템 CA. 포트는 보통 `8883` |

### 수신 메시지 로그

| 출력 | 위치 | 설정 |
| --- | --- | --- |
| 요약 (메시지당 한 줄) | 콘솔 / journal | `IPC_LOG_SUMMARY=true` |
| 전문 (헤더+body JSON) | `{프로젝트 루트}/data/logs/{device_key}/{YYYY-MM-DD}.log` | `IPC_LOG_FILE=true` |

```text
# 콘솔
2026-10-12 17:50:01 [INFO] ipc: DATA collector.plc.plc-mitsubishi-1 samples=5 errors=1
2026-10-12 17:50:01 [INFO] ipc: HEALTH collector.plc.plc-mitsubishi-1 device_ok=False reason=E-1001

# data/logs/plc-mitsubishi-1/2026-10-12.log (프로젝트 루트 기준)
2026-10-12T17:50:01.123 [DATA] collector.plc.plc-mitsubishi-1 {"msg_id":"…","collector_address":"plc-mitsubishi-1","msg_type":"data","msg_body":{…},…}
```

- 폴더 이름은 토픽 `collector.{kind}.{device_key}.{msg_type}`의 `device_key`입니다. 새 장비가 들어오면 폴더가 자동으로 생깁니다. 경로에 쓸 수 없는 문자(`/`, `..` 등)는 `_`로 바뀝니다.
- 파일명 날짜 기준으로 보관 기간이 지난 파일을 지웁니다. 이 프로세스가 기록한 device_key 폴더 안의 `YYYY-MM-DD.log`만 대상입니다. 스케줄은 `.env`로 정합니다.

  | 변수 | 기본값 | 설명 |
  | --- | --- | --- |
  | `IPC_LOG_RETENTION_DAYS` | `7` | 오늘 포함 최근 N일만 보관 (0 = 삭제 안 함) |
  | `IPC_LOG_CLEAN_HOUR` | `3` | 매일 이 시각(로컬, 0~23)에 정리 |
  | `IPC_LOG_CLEAN_ON_START` | `true` | 기동 후 device 폴더를 처음 기록할 때도 정리 |
- `data/logs`는 프로젝트 루트(`main.py`가 있는 폴더) 기준이며, 실행 위치(cwd)와 무관합니다. 없으면 자동으로 만듭니다.
- 쓰기에 실패하면 에러 로그를 남기고 60초마다 다시 시도하며, 서비스는 계속 동작합니다.

## 북향 주소 공간

```text
Objects
├── Factory/Line01/Press01      ← tag map (경로·이름·형식·단위 고정)
│   ├── Temperature
│   └── ProductionCount
├── Collectors                  ← auto 노드: {OPCUA_AUTO_ROOT}/{kind}/{device_key}/{addr}
│   ├── robot
│   │   └── robot-1
│   │       ├── Identification
│   │       │   ├── AasId         collector 헤더 상수
│   │       │   ├── GlobalAssetId
│   │       │   └── DeviceKey
│   │       └── S0004
│   └── plc
│       └── plc-mitsubishi-1
│           ├── D100
│           └── D800.0
└── _Forwarder                  ← 상태 노드 (예약 이름)
    ├── StartTime / TagCount / LastUpdateTime
    ├── MessagesReceived / MessagesInvalid
    └── Sources/{kind}/{device_key}
        ├── DeviceOk            collector HEALTH 의 device_ok
        ├── Stale               SOURCE_STALE_MS 동안 DATA 없음
        ├── Reason              마지막 HEALTH 실패 코드 (예: E-1001)
        └── LastDataTime
```

- 변수 NodeId는 `ns=<namespace>;s=<tag_id>`로 고정됩니다. auto 노드의 tag_id는 `{kind}.{device_key}.{addr}` 입니다 (예: `plc.plc-mitsubishi-1.D100`).
- 값의 SourceTimestamp는 collector DATA의 `sample_ms`, ServerTimestamp는 forwarder 반영 시각입니다.
- 상위 시스템에서 값을 쓰는 것은 허용하지 않습니다.

### tag map

collector 샘플 `(kind, device_key, addr)`을 북향 노드에 연결합니다. 운영에서는 tag map으로 경로를 고정하고 auto 노드는 끄는 것(`OPCUA_AUTO_CREATE_NODES=false`)을 권장합니다.

```json
{
  "tags": [
    {
      "tag_id": "line01.press01.temperature",
      "enabled": true,
      "source": { "kind": "plc", "device_key": "plc-mitsubishi-1", "addr": "D100" },
      "target": {
        "path": "Factory/Line01/Press01",
        "name": "Temperature",
        "data_type": "Double",
        "unit": "°C",
        "description": "프레스 1호기 금형 온도"
      },
      "conversion": { "scale": 0.1, "offset": 0.0 }
    }
  ]
}
```

- `source.addr`는 collector가 DATA에 싣는 `addr` 문자열과 **완전히 같아야** 합니다 (`D100`, `D800.0`, `D101~102` …).
- `data_type`: `Boolean`, `SByte`, `Byte`, `Int16`, `UInt16`, `Int32`, `UInt32`, `Int64`, `UInt64`, `Float`, `Double`, `String`
- `conversion`: 제공값 = 원본값 × `scale` + `offset` (숫자만). 소수가 나오면 `data_type`을 `Double`/`Float`로 둡니다. 정수 형식이면 반올림합니다.
- 같은 샘플을 여러 태그에 연결할 수 있습니다. `target.path` 첫 세그먼트로 `_Forwarder`는 쓸 수 없습니다.

tag map 파일 하나가 이 프로세스의 표입니다. 그 안에 자산과 태그를 여러 개 둘 수 있습니다. 파일을 자산마다 쪼개지 않습니다.

### collector 헤더 (AAS 표시)

개별 라이브 값에 AAS 태그를 달지 않습니다. tag map `collectors` 는 ZeroMQ 로 올라오는 **collector id 헤더**입니다. `(kind, device_key)` 로 조회해 `aas_id` 를 붙이고, 그 collector 객체 아래에 Identification 을 한 번 둡니다.

토픽 `collector.robot.robot-1.data` / body `device_key=robot-1` → 헤더의 `aas_id` → 북향 `Collectors/robot/robot-1/Identification/AasId`.

```json
{
  "collectors": [
    {
      "kind": "robot",
      "device_key": "robot-1",
      "aas_id": "urn:example:aas:hyundai:hh7:pickplace:01",
      "global_asset_id": "urn:example:asset:hyundai:hh7:pickplace:01"
    }
  ]
}
```

- 샘플(`S0004` 등)마다 헤더를 쓰지 않습니다. collector 한 대에 행 하나입니다.
- `.env` 나 DATA 토픽에 AAS id 를 싣지 않습니다. 키는 이 표에만 둡니다.
- `tags` 는 예전처럼 주소 재배치가 필요할 때만 씁니다. 헤더와 무관합니다.
- 헤더가 없는 collector 는 값만 나가고 Identification 은 없습니다.
- BrowseName 은 `Identification`, `AasId`, `GlobalAssetId`, `DeviceKey`. NodeId 예: `ns=;s=Collectors/robot/robot-1/Identification/AasId`.
- AAS Property 매칭은 상위 뷰어가 합니다.

### auto 노드 형식

| 샘플 `type` | OPC UA 형식 |
| --- | --- |
| `bit` | Boolean |
| `word` | Int16 (collector-plc는 signed16으로 디코드) |
| `int`, `long word` | Int64 |
| `ascii` | String |
| `u8`/`i8`/`u16`/`i16`/`u32`/`i32`/`u64`/`i64`/`f32`/`f64` (robot 메모리 맵) | 대응하는 정수/실수 형식 |
| 그 외 | 값의 파이썬 타입으로 추론 (배열은 지원하지 않음) |

### 품질 상태

| 상황 | 품질 | 값 |
| --- | --- | --- |
| 시작 후 아직 값 없음 | `BadWaitingForInitialData` | 없음 |
| 정상 수신 | `Good` | 현재 값 |
| 샘플 `error` = `E-2203` (주소 범위 밖) | `BadOutOfRange` | 없음 |
| 샘플 `error` = `E-1xxx` / `E-4xxx` / 기타 | `BadNotConnected` / `BadConfigurationError` / `BadDeviceFailure` | 없음 |
| 노드 형식으로 변환 불가 | `BadTypeMismatch` | 없음 |
| HEALTH `device_ok=false` 또는 `SOURCE_STALE_MS` 동안 DATA 없음 | `UncertainLastUsableValue` | 마지막 정상 값 |

다음 DATA를 받으면 다시 `Good`이 됩니다.

## 관리 페이지

포워더와 같은 프로세스에서 연다. 수집원 목록은 메모리에만 있으므로 화면용 프로세스를 따로 두지 않는다. `ADMIN_PORT` 가 이미 쓰이고 있으면 오류 로그만 남기고 수신과 북향은 계속한다.

기본 주소는 `http://127.0.0.1:8088/` 이다.

- 구독 주소, tag map 에 적힌 collector, 최근에 DATA/HEALTH 가 온 collector
- 이번 실행의 수신 요약과 `data/logs/{device_key}` 파일의 마지막 줄
- 북향 모드, MQTT 토픽 접두사, ZeroMQ 주소와 구독 필터, OPC UA 주소

저장은 `.env` 의 해당 키만 바꾼다. 주석과 tag map 은 그대로 둔다. MQTT 비밀번호는 다시 보여 주지 않고, 입력한 경우에만 덮어쓴다. 적용은 페이지의 재시작이다. 프로세스가 소켓을 닫고 같은 명령으로 다시 뜬다.

`ADMIN_TOKEN` 이 있으면 `/api/` 요청에 `X-Admin-Token` 헤더가 필요하다. `ADMIN_HOST` 를 루프백 밖으로 열 때는 토큰을 둔다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `ADMIN_ENABLE` | `true` | 관리 페이지를 연다 |
| `ADMIN_HOST` | `127.0.0.1` | 바인드 주소 |
| `ADMIN_PORT` | `8088` | 바인드 포트 |
| `ADMIN_TOKEN` | (빈 값) | 비어 있으면 토큰을 검사하지 않는다 |

## 로컬 스모크 테스트

collector나 PLC 없이 확인할 수 있습니다. 터미널 3개를 씁니다 (collector-plc와 포트가 겹치므로 동시에 띄우지 않습니다).

```bash
# 1) forwarder (예시 tag map 사용)
TAG_MAP_FILE=tag_map.example.json python main.py

# 2) 가짜 collector: collector.plc.plc-sim-1.{data,health} 를 0.5초마다 PUB
python tester/fake_collector.py --period-ms 500
#    --count 10 --health-fail : 10회 송신 후 device_ok=false 를 보내고 종료

# 3) 북향 서버 조회 (2초마다 반복)
python tester/opcua_reader.py --watch 2
```

UaExpert 등에서 `opc.tcp://<엣지 IP>:4840/edge/`로 접속해도 됩니다 (보안 없음 모드).

## 서비스 등록 (Linux)

`.venv` 와 `.env` 를 만든 뒤, 프로젝트 루트에서 등록합니다. 유닛은 현재 경로를 기준으로 생성됩니다 (`systemd/*.service` 는 `/opt` 고정 설치 예시).

```bash
sudo bash service_add.sh
journalctl -u middleware-edge-forwarder -f
```

- `/etc/systemd/system` 에 시스템 유닛을 등록하고 `multi-user.target` 으로 부팅 시 자동 기동합니다. `data/logs` 쓰기 권한과 맞춥니다.
- collector-plc / collector-robot-modbus 가 같은 장비에 있으면 부팅 시 그 다음에 기동합니다. 없어도 등록은 됩니다.
- 제거: `sudo systemctl disable --now middleware-edge-forwarder && sudo rm /etc/systemd/system/middleware-edge-forwarder.service && sudo systemctl daemon-reload`

## 개발 현황 / 이후 검토

- [x] ZeroMQ SUB로 collector DATA/HEALTH 수신 (다중 엔드포인트, kind/device 필터)
- [x] tag map 기반 북향 노드 + auto 노드
- [x] 값 형식 변환, scale/offset, 품질 상태, SourceTimestamp 전달
- [x] HEALTH·수신 공백을 Uncertain 품질과 `_Forwarder` 상태 노드에 반영
- [x] tag map `collectors` 헤더(collector id → aas_id)를 북향 `Identification` 으로 노출
- [x] `NORTHBOUND_MODE=opcua|mqtt`. 한 번에 하나만 북향으로 보낸다
- [ ] IPC 방식 확정 (ZeroMQ 유지 시 collector별 엔드포인트/포트 규칙 정리)
- [ ] 북향 보안 (인증서·사용자 인증). 현재는 NoSecurity
- [ ] 표준 단위 코드(UNECE)·AnalogItemType 적용 (`middleware_gateway_opc_ua` catalog 참고)
- [ ] tag map 변경 시 재시작 없이 반영
- [ ] 배열 샘플(robot `i16[6]` 등) 지원
