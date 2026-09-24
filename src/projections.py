"""统一追溯投影。

从通行证、车辆（VIN）或部件序列任一入口，重建完全相同的一条链路：
认证变更 → 施工 → 检测 → 申诉 → 换件 → 恢复。三入口只是解析入口不同，
解析到的案卷集合相同，随后走同一个投影函数，因此结论必然一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.domain import (
    DISPOSITION_EVENTS,
    World,
)
from src.events import (
    ADMINISTRATIVE_RESUMED,
    ADMINISTRATIVE_STAYED,
    APPEAL_FILED,
    CERTIFICATION_EFFECT_RESTORED,
    CERTIFICATION_SUSPENDED,
    Event,
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
    REINSPECTION_SIGNED,
    WORK_ATTESTED,
)

# 链路阶段顺序（用于校验与呈现）
CHAIN_STAGES = (
    "certification_change",
    "work",
    "inspection",
    "appeal",
    "replacement",
    "restoration",
)


@dataclass(frozen=True)
class EntryRef:
    kind: str          # pass | vin | serial
    value: str
    vins: frozenset[str]
    serials: frozenset[str]
    case_ids: frozenset[str]


class TraceQuery:
    def __init__(self, world: World) -> None:
        self.world = world

    # ---- 入口解析 ---------------------------------------------------------

    def resolve(self, kind: str, value: str) -> EntryRef:
        if kind == "vin":
            vins = frozenset({value})
            serials = frozenset(self.world.vehicles[value].parts) if value in self.world.vehicles else frozenset()
        elif kind == "serial":
            vins = frozenset(
                vin for vin, vehicle in self.world.vehicles.items() if value in vehicle.parts
            )
            serials = frozenset({value})
        elif kind == "pass":
            vehicle = next((v for v in self.world.vehicles.values() if v.pass_id == value), None)
            if vehicle is None:
                raise KeyError(f"通行证 {value} 不存在")
            vins = frozenset({vehicle.vin})
            serials = frozenset(vehicle.parts)
        else:
            raise ValueError(f"未知查询入口：{kind}")

        cases: set[str] = set()
        for case_id, case in self.world.cases.items():
            for key in case.candidates:
                vin, serial = key.split("@", 1)
                if vins and vin in vins:
                    cases.add(case_id)
                elif serials and serial in serials:
                    cases.add(case_id)
        return EntryRef(kind=kind, value=value, vins=vins, serials=serials, case_ids=frozenset(cases))

    # ---- 主投影 -----------------------------------------------------------

    def trace(self, kind: str, value: str) -> dict[str, Any]:
        ref = self.resolve(kind, value)
        return self._project(ref)

    def _project(self, ref: EntryRef) -> dict[str, Any]:
        world = self.world
        vins, serials, case_ids = ref.vins, ref.serials, ref.case_ids

        def key_in_scope(key: str) -> bool:
            vin, serial = key.split("@", 1)
            return vin in vins or serial in serials

        chain: list[dict[str, Any]] = []

        # 认证变更：相关证书的全部暂停记录
        cert_ids = {world.cases[c].cert_id for c in case_ids if world.cases[c].cert_id}
        for vin in vins:
            vehicle = world.vehicles.get(vin)
            if vehicle is not None:
                cert_ids.update(p.cert_id for p in vehicle.parts.values() if p.cert_id)
        for cert_id in sorted(cert_ids):
            cert = world.certs.get(cert_id)
            if cert is None:
                continue
            for susp in cert.suspensions:
                chain.append(
                    {
                        "stage": "certification_change",
                        "cert_id": cert_id,
                        "batches": sorted(susp["batches"]),
                        "purposes": sorted(susp["purposes"]),
                        "reason": susp["reason"],
                        "effective_at": susp["effective_at"],
                        "original_due_at": susp["original_due_at"],
                    }
                )

        # 施工、绑定、车辆登记、通行证
        for vin in sorted(vins):
            vehicle = world.vehicles.get(vin)
            if vehicle is None:
                continue
            chain.append(
                {
                    "stage": "vehicle_declared",
                    "vin": vin,
                    "vehicle_model": vehicle.vehicle_model,
                    "owner": vehicle.registered_owner,
                    "at": vehicle.declared_at,
                }
            )
            for serial in sorted(vehicle.parts):
                part = vehicle.parts[serial]
                chain.append(
                    {
                        "stage": "part_bound",
                        "vin": vin,
                        "serial_no": serial,
                        "cert_id": part.cert_id,
                        "batch_no": part.batch_no,
                        "purpose": part.purpose,
                        "at": part.bound_at,
                    }
                )
                if part.installed_at:
                    chain.append(
                        {
                            "stage": "work",
                            "vin": vin,
                            "serial_no": serial,
                            "work_id": part.work_id,
                            "technician": part.technician,
                            "shop": part.shop,
                            "installed_at": part.installed_at,
                            "removed_at": part.removed_at,
                        }
                    )
            if vehicle.pass_id:
                chain.append(
                    {
                        "stage": "pass_activated",
                        "pass_id": vehicle.pass_id,
                        "vin": vin,
                        "at": vehicle.pass_activated_at,
                    }
                )

        # 检测（初检与机构回传、争议）
        for vin in sorted(vins):
            vehicle = world.vehicles.get(vin)
            if vehicle is None:
                continue
            for inspection in vehicle.inspections:
                chain.append(
                    {
                        "stage": "inspection",
                        "kind": "initial",
                        "vin": vin,
                        **inspection,
                    }
                )
        for report_no, evidence in world.evidences.items():
            for report in evidence.reports:
                if report["vin"] not in vins and report["serial_no"] not in serials:
                    continue
                chain.append(
                    {
                        "stage": "inspection",
                        "kind": "lab_report",
                        "report_no": report_no,
                        **report,
                    }
                )
            for dispute in evidence.disputes:
                chain.append(
                    {
                        "stage": "inspection",
                        "kind": "dispute",
                        "report_no": report_no,
                        **dispute,
                    }
                )

        # 案卷：圈定、确认、处置、复检、换件、恢复
        case_view: dict[str, dict[str, Any]] = {}
        for case_id in sorted(case_ids):
            case = world.cases[case_id]
            confirmed = {k for k in case.confirmed if key_in_scope(k)}
            targets_in_scope = sorted(k for k in case.dispositions if key_in_scope(k))
            dispositions = []
            for key in targets_in_scope:
                for item in case.dispositions[key]:
                    dispositions.append({"target": key, **item})
            restores = [
                {
                    "targets": sorted(t for t in r["targets"] if key_in_scope(t)),
                    "restored_by": r["restored_by"],
                    "basis": r["basis"],
                    "at": r["at"],
                }
                for r in case.restores
            ]
            case_view[case_id] = {
                "cert_id": case.cert_id,
                "batches": sorted(case.batches),
                "purposes": sorted(case.purposes),
                "confirmed_targets": sorted(confirmed),
                "confirmed_by": case.confirmed_by,
                "dispositions": sorted(dispositions, key=lambda d: d["at"]),
                "reinspections": [r for r in case.reinspections if key_in_scope(r["target"])],
                "restores": restores,
                "appeals": list(case.appeals),
            }
            for item in dispositions:
                chain.append({"stage": "disposition", "case_id": case_id, **item})
            for item in case.reinspections:
                if key_in_scope(item["target"]):
                    chain.append({"stage": "inspection", "kind": "reinspection", "case_id": case_id, **item})
            for r in restores:
                if r["targets"]:
                    chain.append({"stage": "restoration", "case_id": case_id, **r})

        # 换件
        for vin in sorted(vins):
            vehicle = world.vehicles.get(vin)
            if vehicle is None:
                continue
            for serial, part in vehicle.parts.items():
                if part.successor_serial:
                    chain.append(
                        {
                            "stage": "replacement",
                            "vin": vin,
                            "old_serial_no": serial,
                            "new_serial_no": part.successor_serial,
                            "removed_at": part.removed_at,
                        }
                    )

        # 申诉与行政结论
        appeal_view: dict[str, Any] = {}
        for case_id in case_ids:
            for appeal_id in world.cases[case_id].appeals:
                appeal = world.appeals[appeal_id]
                appeal_view[appeal_id] = {
                    "case_id": case_id,
                    "targets": sorted(t for t in appeal.targets if key_in_scope(t)),
                    "filed_by": appeal.filed_by,
                    "administrative_stayed": appeal.stayed,
                    "history": list(appeal.history),
                }
                chain.append(
                    {
                        "stage": "appeal",
                        "appeal_id": appeal_id,
                        "case_id": case_id,
                        "filed_by": appeal.filed_by,
                        "administrative_stayed": appeal.stayed,
                    }
                )

        # 所有权与通知
        ownership_view: dict[str, list[dict[str, Any]]] = {}
        for vin in sorted(vins):
            periods = world.ownership_periods(vin)
            ownership_view[vin] = periods
            chain.append({"stage": "ownership_timeline", "vin": vin, "periods": periods})
            for note in world.notifications.get(vin, []):
                chain.append({"stage": "notification", **note})

        # 当前仍有效的安全限制（申诉停留不影响其存在）
        active_holdings = []
        for vin in sorted(vins):
            active_holdings.extend(world.active_holdings(vin))

        # 期限时钟
        clocks = [
            {
                "clock_id": clock.clock_id,
                "case_id": clock.case_id,
                "original_due_at": clock.original_due_at,
                "running": clock.running,
                "suspensions": list(clock.suspensions),
                "resumes": list(clock.resumes),
            }
            for clock in world.clocks.values()
            if clock.case_id in case_ids
        ]

        chain.sort(key=_chain_order)

        return {
            "entry": {"kind": ref.kind, "value": ref.value},
            "vins": sorted(ref.vins),
            "serials": sorted(ref.serials),
            "case_ids": sorted(ref.case_ids),
            "cases": case_view,
            "appeals": appeal_view,
            "ownership": ownership_view,
            "active_holdings": active_holdings,
            "clocks": clocks,
            "chain": chain,
        }

    # ---- 三入口一致性的直接断言辅助 --------------------------------------

    def consistent_across_entries(self, *, pass_id: str, vin: str, serial_no: str) -> bool:
        traces = [
            self.trace("pass", pass_id),
            self.trace("vin", vin),
            self.trace("serial", serial_no),
        ]
        fingerprints = {_fingerprint(t) for t in traces}
        return len(fingerprints) == 1


def _chain_order(item: dict[str, Any]) -> tuple:
    """按发生时间排序；无时间戳的条目排末尾，且绝不与 datetime 直接比较。"""
    when = item.get("at") or item.get("effective_at") or item.get("installed_at")
    return (0, when) if when is not None else (1, 0)


def _fingerprint(trace: dict[str, Any]) -> tuple:
    """只对业务结论做指纹（忽略入口字段本身）。"""
    return (
        tuple(trace["case_ids"]),
        tuple(sorted((c, tuple(v["confirmed_targets"]),
                      tuple((d["target"], d["kind"]) for d in v["dispositions"]),
                      tuple((tuple(r["targets"]), r["restored_by"]) for r in v["restores"]))
                     for c, v in trace["cases"].items())),
        tuple(sorted((h["case_id"], h["target"], h["kind"]) for h in trace["active_holdings"])),
        tuple(sorted((a, v["administrative_stayed"], tuple(v["targets"]))
                     for a, v in trace["appeals"].items())),
    )


# 供事件驱动型投影复用：标注事件属于链路的哪一段
EVENT_STAGE = {
    PLAN_DECLARED: "vehicle_declared",
    PART_BOUND: "part_bound",
    WORK_ATTESTED: "work",
    INSPECTION_SIGNED: "inspection",
    PASS_ACTIVATED: "pass_activated",
    CERTIFICATION_SUSPENDED: "certification_change",
    MATCHING_CONFIRMED: "matching",
    **{event: "disposition" for event in DISPOSITION_EVENTS},
    APPEAL_FILED: "appeal",
    ADMINISTRATIVE_STAYED: "appeal",
    ADMINISTRATIVE_RESUMED: "appeal",
    PART_REPLACED: "replacement",
    REINSPECTION_SIGNED: "inspection",
    CERTIFICATION_EFFECT_RESTORED: "restoration",
    OWNERSHIP_TRANSFERRED: "ownership",
    NOTIFICATION_DELIVERED: "notification",
    INSPECTION_REPORT_RECEIVED: "inspection",
    INSPECTION_DISPUTED: "inspection",
    PROCESS_SUSPENDED: "clock",
    PROCESS_RESUMED: "clock",
}


def classify(event: Event) -> str:
    return EVENT_STAGE.get(event.event_type, "other")
