"""把事件流重放成可读的领域状态。

所有判断（圈定、处置是否生效、期限是否到期）都只基于重放结果，禁止在命令里
缓存"当前状态"，从而保证从通行证、车辆、部件任一入口看到的结论完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.events import (
    ADMINISTRATIVE_RESUMED,
    ADMINISTRATIVE_STAYED,
    APPEAL_FILED,
    CERTIFICATION_EFFECT_RESTORED,
    CERTIFICATION_SUSPENDED,
    HOLDING_PLACED,
    IMPACT_SCOPED,
    INSPECTION_DISPUTED,
    INSPECTION_REPORT_RECEIVED,
    INSPECTION_SIGNED,
    MATCHING_CONFIRMED,
    NOTIFICATION_DELIVERED,
    OWNERSHIP_TRANSFERRED,
    PART_BOUND,
    PART_REPLACED,
    PASS_ACTIVATED,
    PLAN_DECLARED,
    PROCESS_RESUMED,
    PROCESS_SUSPENDED,
    REINSPECTION_REQUIRED,
    REINSPECTION_SIGNED,
    REPLACEMENT_REQUIRED,
    WORK_ATTESTED,
    Event,
)

# 处置类别
DISPOSITION_HOLD = "hold"            # 暂停：立即施加安全限制
DISPOSITION_REINSPECT = "reinspect"  # 复检
DISPOSITION_REPLACE = "replace"      # 换件

DISPOSITION_EVENTS = {
    HOLDING_PLACED: DISPOSITION_HOLD,
    REINSPECTION_REQUIRED: DISPOSITION_REINSPECT,
    REPLACEMENT_REQUIRED: DISPOSITION_REPLACE,
}

# 圈定出的车辆/部件状态
STATE_NOT_INSTALLED = "not_installed"        # 未施工
STATE_PENDING_INSPECTION = "pending_inspection"  # 待检测
STATE_PASS_ACTIVE = "pass_active"            # 已取得通行证
STATE_RESOLD = "resold"                      # 已转卖


def target_key(vin: str, serial_no: str) -> str:
    return f"{vin}@{serial_no}"


@dataclass
class PartRecord:
    serial_no: str
    part_model: str | None = None
    cert_id: str | None = None
    batch_no: str | None = None
    purpose: str | None = None
    bound_at: datetime | None = None
    installed_at: datetime | None = None
    work_id: str | None = None
    technician: str | None = None
    shop: str | None = None
    removed_at: datetime | None = None
    successor_serial: str | None = None  # 被哪个部件替换

    @property
    def installed(self) -> bool:
        return self.installed_at is not None and self.removed_at is None


@dataclass
class VehicleState:
    vin: str
    vehicle_model: str | None = None
    registered_owner: str | None = None
    declared_at: datetime | None = None
    parts: dict[str, PartRecord] = field(default_factory=dict)
    inspections: list[dict[str, Any]] = field(default_factory=list)
    pass_id: str | None = None
    pass_activated_at: datetime | None = None

    def part_state(self, serial: str) -> str | None:
        part = self.parts.get(serial)
        if part is None or part.bound_at is None:
            return None
        if not part.installed:
            return STATE_NOT_INSTALLED
        passed = any(
            i["serial_no"] == serial and i["result"] == "pass" for i in self.inspections
        )
        if self.pass_activated_at is not None and passed:
            return STATE_PASS_ACTIVE
        return STATE_PENDING_INSPECTION


@dataclass
class CertificationState:
    cert_id: str
    part_model: str | None = None
    suspensions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active_suspension(self) -> dict[str, Any] | None:
        return self.suspensions[-1] if self.suspensions else None


@dataclass
class CaseState:
    case_id: str
    cert_id: str | None = None
    batches: frozenset[str] = frozenset()
    purposes: frozenset[str] = frozenset()
    rule_version: int = 0
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)
    excluded: set[str] = field(default_factory=set)
    confirmed_by: str | None = None
    dispositions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reinspections: list[dict[str, Any]] = field(default_factory=list)
    restores: list[dict[str, Any]] = field(default_factory=list)
    appeals: list[str] = field(default_factory=list)
    created: bool = False

    def latest_disposition(self, key: str) -> dict[str, Any] | None:
        history = self.dispositions.get(key)
        return history[-1] if history else None

    def is_restored(self, key: str) -> bool:
        return any(key in r["targets"] for r in self.restores)

    def active_targets(self) -> dict[str, dict[str, Any]]:
        """仍处于处置措施下（未被恢复）的目标。"""
        return {
            key: self.latest_disposition(key)
            for key in self.dispositions
            if not self.is_restored(key)
        }


@dataclass
class AppealState:
    appeal_id: str
    case_id: str | None = None
    targets: set[str] = field(default_factory=set)
    filed_by: str | None = None
    stayed: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EvidenceState:
    report_no: str
    reports: list[dict[str, Any]] = field(default_factory=list)
    disputes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ClockState:
    clock_id: str
    case_id: str | None = None
    original_due_at: datetime | None = None
    suspensions: list[dict[str, Any]] = field(default_factory=list)
    resumes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return len(self.resumes) >= len(self.suspensions)

    @property
    def due_at(self) -> datetime | None:
        # 期限永远锚定原截止点；暂停/恢复不重新计时。
        return self.original_due_at


@dataclass
class World:
    vehicles: dict[str, VehicleState] = field(default_factory=dict)
    certs: dict[str, CertificationState] = field(default_factory=dict)
    cases: dict[str, CaseState] = field(default_factory=dict)
    appeals: dict[str, AppealState] = field(default_factory=dict)
    evidences: dict[str, EvidenceState] = field(default_factory=dict)
    clocks: dict[str, ClockState] = field(default_factory=dict)
    ownership: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    notifications: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    replacements: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # ---- 构造 -------------------------------------------------------------

    @classmethod
    def replay(cls, events: list[Event]) -> "World":
        world = cls()
        for event in sorted(events, key=lambda e: (e.occurred_at, e.event_id, e.version)):
            world.apply(event)
        return world

    # ---- 所有权辅助 -------------------------------------------------------

    def owner_at(self, vin: str, moment: datetime) -> str | None:
        vehicle = self.vehicles.get(vin)
        current = vehicle.registered_owner if vehicle else None
        for change in self.ownership.get(vin, []):
            if change["transferred_at"] <= moment:
                current = change["to_owner"]
            else:
                break
        return current

    def current_owner(self, vin: str, at: datetime) -> str | None:
        return self.owner_at(vin, at)

    def ownership_periods(self, vin: str) -> list[dict[str, Any]]:
        """返回历任车主的责任时间线（含区间）。"""
        vehicle = self.vehicles.get(vin)
        periods: list[dict[str, Any]] = []
        owner = vehicle.registered_owner if vehicle else None
        start = vehicle.declared_at if vehicle else None
        for change in self.ownership.get(vin, []):
            periods.append(
                {"owner": owner, "from": start, "to": change["transferred_at"]}
            )
            owner = change["to_owner"]
            start = change["transferred_at"]
        periods.append({"owner": owner, "from": start, "to": None})
        return periods

    # ---- 应用事件 ---------------------------------------------------------

    def apply(self, event: Event) -> None:
        p = event.payload
        t = event.event_type

        if t == PLAN_DECLARED:
            vehicle = self.vehicles.setdefault(event.aggregate_id, VehicleState(event.aggregate_id))
            vehicle.vehicle_model = p.get("vehicle_model")
            vehicle.registered_owner = p.get("owner")
            vehicle.declared_at = event.occurred_at

        elif t == PART_BOUND:
            vehicle = self.vehicles.setdefault(event.aggregate_id, VehicleState(event.aggregate_id))
            part = vehicle.parts.setdefault(p["serial_no"], PartRecord(p["serial_no"]))
            part.part_model = p.get("part_model")
            part.cert_id = p.get("cert_id")
            part.batch_no = p.get("batch_no")
            part.purpose = p.get("purpose")
            part.bound_at = event.occurred_at

        elif t == WORK_ATTESTED:
            vehicle = self.vehicles[event.aggregate_id]
            part = vehicle.parts.setdefault(p["serial_no"], PartRecord(p["serial_no"]))
            part.cert_id = p.get("cert_id", part.cert_id)
            part.batch_no = p.get("batch_no", part.batch_no)
            part.purpose = p.get("purpose", part.purpose)
            part.installed_at = event.occurred_at
            part.work_id = p.get("work_id")
            part.technician = p.get("technician")
            part.shop = p.get("shop")
            part.removed_at = None

        elif t == INSPECTION_SIGNED:
            vehicle = self.vehicles[event.aggregate_id]
            vehicle.inspections.append(
                {
                    "serial_no": p["serial_no"],
                    "report_no": p.get("report_no"),
                    "result": p["result"],
                    "inspector": p.get("inspector"),
                    "lab_id": p.get("lab_id"),
                    "at": event.occurred_at,
                }
            )

        elif t == PASS_ACTIVATED:
            vin = p["vin"]
            vehicle = self.vehicles.setdefault(vin, VehicleState(vin))
            vehicle.pass_id = event.aggregate_id
            vehicle.pass_activated_at = event.occurred_at

        elif t == CERTIFICATION_SUSPENDED:
            cert_id = p.get("cert_id", event.aggregate_id)
            cert = self.certs.setdefault(cert_id, CertificationState(cert_id))
            cert.part_model = p.get("part_model", cert.part_model)
            cert.suspensions.append(
                {
                    "batches": frozenset(p.get("affected_batches", ())),
                    "purposes": frozenset(p.get("purposes", ())),
                    "reason": p.get("reason"),
                    "effective_at": event.occurred_at,
                    "original_due_at": _dt(p.get("original_due_at")),
                }
            )

        elif t == IMPACT_SCOPED:
            case = self.cases.setdefault(event.aggregate_id, CaseState(event.aggregate_id))
            case.created = True
            case.cert_id = p["cert_id"]
            case.batches = frozenset(p.get("batches", ()))
            case.purposes = frozenset(p.get("purposes", ()))
            case.rule_version = p.get("rule_version", 1)
            for candidate in p["candidates"]:
                key = target_key(candidate["vin"], candidate["serial_no"])
                case.candidates[key] = dict(candidate)

        elif t == MATCHING_CONFIRMED:
            case = self.cases[event.aggregate_id]
            case.rule_version = p.get("rule_version", case.rule_version)
            case.confirmed |= set(p.get("confirmed", ()))
            case.excluded |= set(p.get("excluded", ()))
            case.confirmed -= case.excluded
            case.confirmed_by = p["confirmed_by"]

        elif t in DISPOSITION_EVENTS:
            case = self.cases[event.aggregate_id]
            kind = DISPOSITION_EVENTS[t]
            for key in p["targets"]:
                case.dispositions.setdefault(key, []).append(
                    {
                        "kind": kind,
                        "decided_by": p["decided_by"],
                        "at": event.occurred_at,
                        "due_at": _dt(p.get("due_at")),
                        "event_id": event.event_id,
                    }
                )

        elif t == APPEAL_FILED:
            appeal_id = p.get("appeal_id", event.aggregate_id)
            appeal = self.appeals.setdefault(appeal_id, AppealState(appeal_id))
            appeal.case_id = p["case_id"]
            appeal.targets |= set(p.get("targets", ()))
            appeal.filed_by = p.get("filed_by")
            appeal.history.append({"kind": "filed", "at": event.occurred_at})
            case = self.cases.get(p["case_id"])
            if case is not None:
                case.appeals.append(p["appeal_id"])

        elif t == ADMINISTRATIVE_STAYED:
            appeal = self.appeals[event.aggregate_id]
            appeal.stayed = True
            appeal.history.append({"kind": "stayed", "at": event.occurred_at})

        elif t == ADMINISTRATIVE_RESUMED:
            appeal = self.appeals[event.aggregate_id]
            appeal.stayed = False
            appeal.history.append({"kind": "resumed", "at": event.occurred_at})

        elif t == OWNERSHIP_TRANSFERRED:
            vin = p["vin"]
            self.ownership.setdefault(vin, []).append(
                {
                    "from_owner": p["from_owner"],
                    "to_owner": p["to_owner"],
                    "transferred_at": event.occurred_at,
                }
            )

        elif t == NOTIFICATION_DELIVERED:
            vin = p["vin"]
            self.notifications.setdefault(vin, []).append(
                {
                    "case_id": p.get("case_id"),
                    "recipient": p["recipient"],
                    "kind": p.get("kind", "current_notice"),
                    "at": event.occurred_at,
                    "reference": p.get("reference"),
                }
            )

        elif t == PART_REPLACED:
            vin = p["vin"]
            vehicle = self.vehicles[vin]
            old = vehicle.parts[p["old_serial_no"]]
            old.removed_at = event.occurred_at
            old.successor_serial = p["new_serial_no"]
            new = vehicle.parts.setdefault(p["new_serial_no"], PartRecord(p["new_serial_no"]))
            new.part_model = p.get("part_model", old.part_model)
            new.cert_id = p["new_cert_id"]
            new.batch_no = p["new_batch_no"]
            new.purpose = p.get("purpose", old.purpose)
            new.installed_at = event.occurred_at
            new.work_id = p.get("work_id")
            new.technician = p.get("technician")
            new.shop = p.get("shop")
            self.replacements.setdefault(target_key(vin, p["old_serial_no"]), []).append(
                {
                    "new_serial_no": p["new_serial_no"],
                    "new_cert_id": p["new_cert_id"],
                    "technician": p.get("technician"),
                    "shop": p.get("shop"),
                    "at": event.occurred_at,
                }
            )

        elif t == INSPECTION_REPORT_RECEIVED:
            evidence = self.evidences.setdefault(p["report_no"], EvidenceState(p["report_no"]))
            evidence.reports.append(
                {
                    "vin": p["vin"],
                    "serial_no": p["serial_no"],
                    "lab_id": p.get("lab_id"),
                    "result": p["result"],
                    "content_hash": p["content_hash"],
                    "channel": p.get("channel", "online"),
                    "at": event.occurred_at,
                    "event_id": event.event_id,
                }
            )

        elif t == INSPECTION_DISPUTED:
            evidence = self.evidences.setdefault(event.aggregate_id, EvidenceState(event.aggregate_id))
            evidence.disputes.append(
                {
                    "report_no": p["report_no"],
                    "prior_content_hash": p["prior_content_hash"],
                    "new_content_hash": p["new_content_hash"],
                    "prior_result": p.get("prior_result"),
                    "new_result": p.get("new_result"),
                    "at": event.occurred_at,
                }
            )

        elif t == REINSPECTION_SIGNED:
            case = self.cases[event.aggregate_id]
            case.reinspections.append(
                {
                    "target": p["target"],
                    "serial_no": p["serial_no"],
                    "report_no": p["report_no"],
                    "result": p["result"],
                    "signed_by": p["signed_by"],
                    "at": event.occurred_at,
                }
            )

        elif t == CERTIFICATION_EFFECT_RESTORED:
            case = self.cases[event.aggregate_id]
            case.restores.append(
                {
                    "targets": frozenset(p["targets"]),
                    "restored_by": p["restored_by"],
                    "basis": p.get("basis", {}),
                    "at": event.occurred_at,
                }
            )

        elif t == PROCESS_SUSPENDED:
            clock = self.clocks.setdefault(event.aggregate_id, ClockState(event.aggregate_id))
            clock.case_id = p["case_id"]
            if clock.original_due_at is None:
                clock.original_due_at = _dt(p["original_due_at"])
            clock.suspensions.append({"reason": p.get("reason"), "at": event.occurred_at})

        elif t == PROCESS_RESUMED:
            clock = self.clocks.setdefault(event.aggregate_id, ClockState(event.aggregate_id))
            clock.case_id = p.get("case_id", clock.case_id)
            if clock.original_due_at is None:
                # 恢复时若缺失原截止点，仍不允许凭空设定新期限。
                clock.original_due_at = _dt(p.get("due_at"))
            clock.resumes.append({"at": event.occurred_at})

    # ---- 查询辅助 ---------------------------------------------------------

    def cases_for_vin(self, vin: str) -> set[str]:
        result = set()
        for case_id, case in self.cases.items():
            if any(key.split("@", 1)[0] == vin for key in case.candidates):
                result.add(case_id)
        return result

    def cases_for_serial(self, serial_no: str) -> set[str]:
        result = set()
        for case_id, case in self.cases.items():
            if any(key.split("@", 1)[1] == serial_no for key in case.candidates):
                result.add(case_id)
        return result

    def active_holdings(self, vin: str) -> list[dict[str, Any]]:
        """车辆当前仍有效的安全限制（申诉不解除）。"""
        holdings = []
        for case_id, case in self.cases.items():
            for key, disposition in case.dispositions.items():
                if not key.startswith(vin + "@"):
                    continue
                if case.is_restored(key):
                    continue
                latest = disposition[-1]
                holdings.append(
                    {
                        "case_id": case_id,
                        "target": key,
                        "kind": latest["kind"],
                        "decided_by": latest["decided_by"],
                        "at": latest["at"],
                        "due_at": latest["due_at"],
                    }
                )
        return holdings


def _dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
