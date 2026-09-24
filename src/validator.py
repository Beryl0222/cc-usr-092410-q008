"""校验领域事件信封与事件载荷的基础字段。

记录一经接收，标识、发生时间与版本不得原地改写；更正使用新的后继记录。
"""

from src.events import AGGREGATE_TYPES, EVENT_CATALOG

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")


def validate_event(record: dict) -> list[str]:
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")

    event_type = record.get("event_type")
    if event_type is not None and event_type not in EVENT_CATALOG:
        errors.append(f"未知事件类型：{event_type}")

    aggregate_type = record.get("aggregate_type")
    if aggregate_type is not None and aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{aggregate_type}")

    payload = record.get("payload")
    if event_type in EVENT_CATALOG:
        expected_aggregate, required_payload = EVENT_CATALOG[event_type]
        if aggregate_type is not None and aggregate_type != expected_aggregate:
            errors.append(
                f"事件 {event_type} 的聚合类型必须是 {expected_aggregate}，实际为 {aggregate_type}"
            )
        if isinstance(payload, dict):
            for name in required_payload:
                if name not in payload:
                    errors.append(f"事件 {event_type} 缺少载荷字段：{name}")
        elif required_payload:
            errors.append(f"事件 {event_type} 必须携带 payload 对象")

    return errors
