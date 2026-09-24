"""校验领域事件信封的基础字段。"""

from __future__ import annotations

from datetime import datetime

from src.events import AGGREGATE_TYPES, ENVELOPE_REQUIRED, EVENT_TYPES


def validate_event(record: dict) -> list[str]:
    errors = [f"缺少字段：{name}" for name in ENVELOPE_REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if "event_type" in record and record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知事件类型：{record['event_type']}")
    if "aggregate_type" in record and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    if "occurred_at" in record:
        value = record["occurred_at"]
        if not isinstance(value, datetime):
            try:
                datetime.fromisoformat(str(value))
            except ValueError:
                errors.append("occurred_at 必须是合法的 date-time")
    return errors
