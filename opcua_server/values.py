"""샘플 값 → OPC UA 값 변환, 데이터 형식 추론, 에러 코드 → StatusCode."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from asyncua import ua

_INTEGER_RANGES = {
    ua.VariantType.SByte: (-(2**7), 2**7 - 1),
    ua.VariantType.Byte: (0, 2**8 - 1),
    ua.VariantType.Int16: (-(2**15), 2**15 - 1),
    ua.VariantType.UInt16: (0, 2**16 - 1),
    ua.VariantType.Int32: (-(2**31), 2**31 - 1),
    ua.VariantType.UInt32: (0, 2**32 - 1),
    ua.VariantType.Int64: (-(2**63), 2**63 - 1),
    ua.VariantType.UInt64: (0, 2**64 - 1),
}
_FLOAT32_MAX = 3.4028234663852886e38

# collector 샘플 type → 북향 노드 형식 (auto 노드용)
_SAMPLE_TYPE_TO_VARIANT = {
    # collector-plc (client/scan_decode.py: word 는 signed16, int/long word 는 다중 워드 정수)
    "bit": ua.VariantType.Boolean,
    "word": ua.VariantType.Int16,
    "int": ua.VariantType.Int64,
    "long word": ua.VariantType.Int64,
    "ascii": ua.VariantType.String,
    # collector_robot_modbus 메모리 맵 타입
    "u8": ua.VariantType.Byte,
    "i8": ua.VariantType.SByte,
    "u16": ua.VariantType.UInt16,
    "i16": ua.VariantType.Int16,
    "u32": ua.VariantType.UInt32,
    "i32": ua.VariantType.Int32,
    "u64": ua.VariantType.UInt64,
    "i64": ua.VariantType.Int64,
    "f32": ua.VariantType.Float,
    "f64": ua.VariantType.Double,
    "str": ua.VariantType.String,
}


def infer_variant_type(sample_type: str | None, value: Any) -> ua.VariantType | None:
    """샘플 type 우선, 모르면 값의 파이썬 타입으로 추론. 판단할 수 없으면 None."""
    if sample_type:
        vtype = _SAMPLE_TYPE_TO_VARIANT.get(sample_type.strip().lower())
        if vtype is not None:
            return vtype
    if isinstance(value, bool):
        return ua.VariantType.Boolean
    if isinstance(value, int):
        return ua.VariantType.Int64
    if isinstance(value, float):
        return ua.VariantType.Double
    if isinstance(value, str):
        return ua.VariantType.String
    return None


def status_for_error(code: str) -> ua.StatusCode:
    """샘플 error(collector ClientErrorCode 값) → 품질 상태."""
    if code == "E-2203":
        return ua.StatusCode(ua.StatusCodes.BadOutOfRange)
    if code.startswith("E-1"):
        return ua.StatusCode(ua.StatusCodes.BadNotConnected)
    if code.startswith("E-4"):
        return ua.StatusCode(ua.StatusCodes.BadConfigurationError)
    return ua.StatusCode(ua.StatusCodes.BadDeviceFailure)


def coerce_value(value: Any, vtype: ua.VariantType) -> Any:
    """
    북향 노드가 선언한 데이터 형식에 맞게 값을 변환한다.

    asyncua 서버는 선언과 다른 형식의 값도 그대로 받아들이므로 여기서 맞춘다.
    정수 형식에 소수가 들어오면 반올림한다. 변환할 수 없으면 TypeError/ValueError/OverflowError.
    """
    if vtype is ua.VariantType.Boolean:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise TypeError(f"Boolean 으로 변환할 수 없습니다: {value!r}")

    if vtype in _INTEGER_RANGES:
        if isinstance(value, float):
            value = round(value)
        elif isinstance(value, bool):
            value = int(value)
        elif not isinstance(value, int):
            raise TypeError(f"{vtype.name} 로 변환할 수 없습니다: {value!r}")
        low, high = _INTEGER_RANGES[vtype]
        if not low <= value <= high:
            raise OverflowError(f"{vtype.name} 범위({low}~{high})를 벗어났습니다: {value}")
        return value

    if vtype in (ua.VariantType.Float, ua.VariantType.Double):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{vtype.name} 로 변환할 수 없습니다: {value!r}")
        number = float(value)
        if vtype is ua.VariantType.Float and math.isfinite(number) and abs(number) > _FLOAT32_MAX:
            raise OverflowError(f"Float 범위를 벗어났습니다: {number}")
        return number

    if vtype is ua.VariantType.String:
        if isinstance(value, list):
            raise TypeError(f"String 으로 변환할 수 없습니다: {value!r}")
        return value if isinstance(value, str) else str(value)

    if vtype is ua.VariantType.DateTime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            text = value.strip()
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        raise TypeError(f"DateTime 으로 변환할 수 없습니다: {value!r}")

    raise TypeError(f"지원하지 않는 형식: {vtype.name}")
