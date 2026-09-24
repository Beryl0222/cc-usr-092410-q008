"""领域事件定义与信封校验。

记录一经接收，event_id、occurred_at、version 不得原地改写；状态变更一律以
追加的后继事件表达。所有处置命令只产生新事件，绝不修改既有事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---- 事件类型 -------------------------------------------------------------

PLAN_DECLARED = "PLAN_DECLARED"
PART_BOUND = "PART_BOUND"
WORK_ATTESTED = "WORK_ATTESTED"
INSPECTION_SIGNED = "INSPECTION_SIGNED"
PASS_ACTIVATED = "PASS_ACTIVATED"

# 部件认证状态变更
CERTIFICATION_SUSPENDED = "CERTIFICATION_SUSPENDED"

# 影响处置：圈定 -> 技术确认 -> 合规处置
IMPACT_SCOPED = "IMPACT_SCOPED"
MATCHING_CONFIRMED = "MATCHING_CONFIRMED"
HOLDING_PLACED = "HOLDING_PLACED"
REINSPECTION_REQUIRED = "REINSPECTION_REQUIRED"
REPLACEMENT_REQUIRED = "REPLACEMENT_REQUIRED"

# 申诉与行政结论
APPEAL_FILED = "APPEAL_FILED"
ADMINISTRATIVE_STAYED = "ADMINISTRATIVE_STAYED"
ADMINISTRATIVE_RESUMED = "ADMINISTRATIVE_RESUMED"

# 换件与恢复
PART_REPLACED = "PART_REPLACED"
CERTIFICATION_EFFECT_RESTORED = "CERTIFICATION_EFFECT_RESTORED"

# 所有权与通知
OWNERSHIP_TRANSFERRED = "OWNERSHIP_TRANSFERRED"
NOTIFICATION_DELIVERED = "NOTIFICATION_DELIVERED"

# 检测机构（含离线）回传
INSPECTION_REPORT_RECEIVED = "INSPECTION_REPORT_RECEIVED"
REINSPECTION_SIGNED = "REINSPECTION_SIGNED"
INSPECTION_DISPUTED = "INSPECTION_DISPUTED"

# 整改/升级进程时钟（暂停期不重新计时）
PROCESS_SUSPENDED = "PROCESS_SUSPENDED"
PROCESS_RESUMED = "PROCESS_RESUMED"

EVENT_TYPES = frozenset(
    {
        PLAN_DECLARED,
        PART_BOUND,
        WORK_ATTESTED,
        INSPECTION_SIGNED,
        PASS_ACTIVATED,
        CERTIFICATION_SUSPENDED,
        IMPACT_SCOPED,
        MATCHING_CONFIRMED,
        HOLDING_PLACED,
        REINSPECTION_REQUIRED,
        REPLACEMENT_REQUIRED,
        APPEAL_FILED,
        ADMINISTRATIVE_STAYED,
        ADMINISTRATIVE_RESUMED,
        PART_REPLACED,
        CERTIFICATION_EFFECT_RESTORED,
        OWNERSHIP_TRANSFERRED,
        NOTIFICATION_DELIVERED,
        INSPECTION_REPORT_RECEIVED,
        REINSPECTION_SIGNED,
        INSPECTION_DISPUTED,
        PROCESS_SUSPENDED,
        PROCESS_RESUMED,
    }
)

# ---- 聚合类型 -------------------------------------------------------------

VEHICLE_PROFILE = "vehicle_profile"
MODIFICATION_PLAN = "modification_plan"
INSPECTION_DECISION = "inspection_decision"
COMPLIANCE_PASS = "compliance_pass"
PART_CERTIFICATION = "part_certification"
IMPACT_CASE = "impact_case"
APPEAL = "appeal"
OWNERSHIP_HISTORY = "ownership_history"
INSPECTION_EVIDENCE = "inspection_evidence"
PROCESS_CLOCK = "process_clock"

AGGREGATE_TYPES = frozenset(
    {
        VEHICLE_PROFILE,
        MODIFICATION_PLAN,
        INSPECTION_DECISION,
        COMPLIANCE_PASS,
        PART_CERTIFICATION,
        IMPACT_CASE,
        APPEAL,
        OWNERSHIP_HISTORY,
        INSPECTION_EVIDENCE,
        PROCESS_CLOCK,
    }
)

ENVELOPE_REQUIRED = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
)


@dataclass(frozen=True)
class Event:
    """不可变事件信封。payload 承载事件的全部业务字段。"""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(record: dict) -> "Event":
        occurred_at = record["occurred_at"]
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at)
        payload = {k: v for k, v in record.items() if k not in ENVELOPE_REQUIRED}
        return Event(
            event_id=record["event_id"],
            event_type=record["event_type"],
            aggregate_type=record["aggregate_type"],
            aggregate_id=record["aggregate_id"],
            occurred_at=occurred_at,
            version=record["version"],
            summary=record["summary"],
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "summary": self.summary,
        }
        data.update(self.payload)
        return data
