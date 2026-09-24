"""认证暂停影响处置服务。

处置链路：证书暂停 → 沿部件序列与施工记录圈定车辆 → 技术人员确认匹配规则 →
合规人员决定暂停/复检/换件 → （申诉可暂缓行政结论）→ 换件/复检 → 另一人凭新
检测结果恢复。

关键不变量：
- 只追加事件；当前状态一律由 ``World.replay`` 得到，命令内不缓存。
- 处置目标必须是已确认匹配的 vin@部件序列 目标，绝不按车型整体扩大。
- 申诉只暂缓行政结论，安全限制仍然有效。
- 恢复者必须区别于处置人、检测签署人及换件施工人，且以新检测合格为依据。
- 离线回传相同编号+相同内容只收一次；编号相同内容变化则并存并形成独立争议。
- 整改期限锚定原截止点，进程暂停/恢复不重新计时。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, Iterable

from src.domain import (
    DISPOSITION_HOLD,
    DISPOSITION_REINSPECT,
    DISPOSITION_REPLACE,
    STATE_NOT_INSTALLED,
    STATE_PASS_ACTIVE,
    STATE_RESOLD,
    World,
    target_key,
)
from src.events import (
    ADMINISTRATIVE_RESUMED,
    ADMINISTRATIVE_STAYED,
    APPEAL,
    APPEAL_FILED,
    CERTIFICATION_EFFECT_RESTORED,
    CERTIFICATION_SUSPENDED,
    COMPLIANCE_PASS,
    HOLDING_PLACED,
    IMPACT_CASE,
    IMPACT_SCOPED,
    INSPECTION_DISPUTED,
    INSPECTION_EVIDENCE,
    INSPECTION_REPORT_RECEIVED,
    INSPECTION_SIGNED,
    MATCHING_CONFIRMED,
    NOTIFICATION_DELIVERED,
    OWNERSHIP_HISTORY,
    OWNERSHIP_TRANSFERRED,
    PART_BOUND,
    PART_CERTIFICATION,
    PART_REPLACED,
    PASS_ACTIVATED,
    PLAN_DECLARED,
    PROCESS_CLOCK,
    PROCESS_RESUMED,
    PROCESS_SUSPENDED,
    REINSPECTION_REQUIRED,
    REINSPECTION_SIGNED,
    REPLACEMENT_REQUIRED,
    VEHICLE_PROFILE,
    WORK_ATTESTED,
    Event,
)
from src.store import ConcurrentModificationError, EventStore

_DISPOSITION_EVENT = {
    DISPOSITION_HOLD: HOLDING_PLACED,
    DISPOSITION_REINSPECT: REINSPECTION_REQUIRED,
    DISPOSITION_REPLACE: REPLACEMENT_REQUIRED,
}


class DomainError(RuntimeError):
    """业务规则被违反。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ComplianceService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def _commit_one(self, build: Callable[[World], list[Event] | None]) -> Event | None:
        written = self._commit(build)
        return written[0] if written else None

    # ---- 通用提交（乐观并发，冲突时重载重算） ------------------------------

    def _commit(self, build: Callable[[World], list[Event] | None], *, retries: int = 32) -> list[Event]:
        for _ in range(retries):
            world = World.replay(self.store.all_events())
            produced = build(world)
            if not produced:
                return []
            # 为事件补版本号；版本基线取此刻存储，提交时再做原子校验。
            numbered: list[Event] = []
            per_agg: dict[str, int] = {}
            base_version: dict[str, int] = {}
            for event in produced:
                if event.aggregate_id in per_agg:
                    per_agg[event.aggregate_id] += 1
                else:
                    base = self.store.version(event.aggregate_id)
                    base_version[event.aggregate_id] = base
                    per_agg[event.aggregate_id] = base + 1
                version = per_agg[event.aggregate_id]
                numbered.append(
                    Event(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        aggregate_type=event.aggregate_type,
                        aggregate_id=event.aggregate_id,
                        occurred_at=event.occurred_at,
                        version=version,
                        summary=event.summary,
                        payload=dict(event.payload),
                    )
                )
            try:
                return self.store.append_many(numbered, expected_versions=base_version)
            except ConcurrentModificationError:
                continue
        raise ConcurrentModificationError("并发冲突重试耗尽")

    # ---- 0. 基线登记：车型配置 / 部件绑定 / 施工 / 初检 / 通行证 ----------

    def declare_vehicle(
        self, *, vin: str, vehicle_model: str, owner: str, at: datetime
    ) -> Event:
        def build(world: World) -> list[Event]:
            if vin in world.vehicles and world.vehicles[vin].declared_at is not None:
                return []
            return [
                Event(
                    event_id=_new_id("evt-plan"),
                    event_type=PLAN_DECLARED,
                    aggregate_type=VEHICLE_PROFILE,
                    aggregate_id=vin,
                    occurred_at=at,
                    version=1,
                    summary=f"登记车型 {vehicle_model} 车辆 {vin}，车主 {owner}",
                    payload={"vehicle_model": vehicle_model, "owner": owner},
                )
            ]

        return self._commit_one(build)

    def bind_part(
        self,
        *,
        vin: str,
        serial_no: str,
        part_model: str,
        cert_id: str,
        batch_no: str,
        purpose: str,
        at: datetime,
    ) -> Event:
        def build(world: World) -> list[Event]:
            if vin not in world.vehicles:
                raise DomainError(f"车辆 {vin} 尚未登记")
            if serial_no in world.vehicles[vin].parts:
                return []
            version = self.store.version(vin)
            return [
                Event(
                    event_id=_new_id("evt-bind"),
                    event_type=PART_BOUND,
                    aggregate_type=VEHICLE_PROFILE,
                    aggregate_id=vin,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"部件序列 {serial_no} 绑定到车辆 {vin}（批次 {batch_no}，用途 {purpose}）",
                    payload={
                        "serial_no": serial_no,
                        "part_model": part_model,
                        "cert_id": cert_id,
                        "batch_no": batch_no,
                        "purpose": purpose,
                    },
                )
            ]

        return self._commit_one(build)

    def attest_work(
        self,
        *,
        vin: str,
        serial_no: str,
        work_id: str,
        technician: str,
        shop: str,
        at: datetime,
    ) -> Event:
        def build(world: World) -> list[Event]:
            vehicle = world.vehicles.get(vin)
            if vehicle is None or serial_no not in vehicle.parts:
                raise DomainError(f"车辆 {vin} 上不存在部件 {serial_no}")
            part = vehicle.parts[serial_no]
            if part.installed_at is not None and part.removed_at is None:
                return []
            version = self.store.version(vin)
            return [
                Event(
                    event_id=_new_id("evt-work"),
                    event_type=WORK_ATTESTED,
                    aggregate_type=VEHICLE_PROFILE,
                    aggregate_id=vin,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"{shop} 的 {technician} 完成 {serial_no} 施工，工单 {work_id}",
                    payload={
                        "serial_no": serial_no,
                        "work_id": work_id,
                        "technician": technician,
                        "shop": shop,
                    },
                )
            ]

        return self._commit_one(build)

    def sign_initial_inspection(
        self,
        *,
        vin: str,
        serial_no: str,
        report_no: str,
        result: str,
        inspector: str,
        lab_id: str,
        at: datetime,
    ) -> Event:
        def build(world: World) -> list[Event]:
            vehicle = world.vehicles.get(vin)
            if vehicle is None or serial_no not in vehicle.parts:
                raise DomainError(f"车辆 {vin} 上不存在部件 {serial_no}")
            version = self.store.version(vin)
            return [
                Event(
                    event_id=_new_id("evt-inspect"),
                    event_type=INSPECTION_SIGNED,
                    aggregate_type=VEHICLE_PROFILE,
                    aggregate_id=vin,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"{lab_id} {inspector} 签署初检：{serial_no} 结论 {result}",
                    payload={
                        "serial_no": serial_no,
                        "report_no": report_no,
                        "result": result,
                        "inspector": inspector,
                        "lab_id": lab_id,
                    },
                )
            ]

        return self._commit_one(build)

    def activate_pass(self, *, pass_id: str, vin: str, at: datetime) -> Event:
        def build(world: World) -> list[Event]:
            vehicle = world.vehicles.get(vin)
            if vehicle is None:
                raise DomainError(f"车辆 {vin} 不存在")
            if vehicle.pass_id is not None:
                return []
            version = self.store.version(pass_id)
            return [
                Event(
                    event_id=_new_id("evt-pass"),
                    event_type=PASS_ACTIVATED,
                    aggregate_type=COMPLIANCE_PASS,
                    aggregate_id=pass_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"车辆 {vin} 的改装合规通行证 {pass_id} 生效",
                    payload={"vin": vin},
                )
            ]

        return self._commit_one(build)

    # ---- 1. 认证暂停 ------------------------------------------------------

    def suspend_certification(
        self,
        *,
        cert_id: str,
        part_model: str,
        affected_batches: Iterable[str],
        purposes: Iterable[str],
        reason: str,
        effective_at: datetime,
        original_due_at: datetime,
        suspended_by: str,
    ) -> Event:
        batches = tuple(affected_batches)
        purposes_t = tuple(purposes)

        def build(world: World) -> list[Event]:
            version = self.store.version(cert_id)
            return [
                Event(
                    event_id=_new_id("evt-cert-susp"),
                    event_type=CERTIFICATION_SUSPENDED,
                    aggregate_type=PART_CERTIFICATION,
                    aggregate_id=cert_id,
                    occurred_at=effective_at,
                    version=version + 1,
                    summary=f"部件认证 {cert_id} 按用途暂停：{reason}",
                    payload={
                        "cert_id": cert_id,
                        "part_model": part_model,
                        "affected_batches": list(batches),
                        "purposes": list(purposes_t),
                        "reason": reason,
                        "suspended_by": suspended_by,
                        "original_due_at": _iso(original_due_at),
                    },
                )
            ]

        return self._commit_one(build)

    # ---- 2. 圈定影响 ------------------------------------------------------

    def scope_impact(
        self,
        *,
        case_id: str,
        cert_id: str,
        at: datetime,
        scoped_by: str,
    ) -> list[Event]:
        def build(world: World) -> list[Event]:
            cert = world.certs.get(cert_id)
            if cert is None or not cert.suspensions:
                raise DomainError(f"证书 {cert_id} 不存在有效暂停记录")
            if case_id in world.cases:
                # 案卷已圈定，重复提交不产生第二份范围。
                return []
            suspension = cert.active_suspension
            batches = suspension["batches"]
            purposes = suspension["purposes"]

            candidates: list[dict[str, Any]] = []
            for vin, vehicle in world.vehicles.items():
                resold = bool(world.ownership.get(vin)) and any(
                    c["transferred_at"] <= at for c in world.ownership[vin]
                )
                for serial, part in vehicle.parts.items():
                    if part.cert_id != cert_id:
                        continue
                    if batches and part.batch_no not in batches:
                        continue  # 同车型但批次不在暂停范围，不得纳入
                    if purposes and part.purpose not in purposes:
                        continue  # 证书用途不匹配，不得纳入
                    # 暂停生效前已拆下的旧件不在当前影响范围内。
                    if part.removed_at is not None and part.removed_at <= suspension["effective_at"]:
                        continue

                    base_state = vehicle.part_state(serial) or STATE_NOT_INSTALLED
                    state = STATE_RESOLD if resold else base_state
                    candidates.append(
                        {
                            "vin": vin,
                            "serial_no": serial,
                            "vehicle_model": vehicle.vehicle_model,
                            "batch_no": part.batch_no,
                            "purpose": part.purpose,
                            "work_id": part.work_id,
                            "shop": part.shop,
                            "technician": part.technician,
                            "state": state,
                            "pass_active": base_state == STATE_PASS_ACTIVE,
                            "resold": resold,
                            "current_owner": world.current_owner(vin, at),
                        }
                    )

            version = self.store.version(case_id)
            return [
                Event(
                    event_id=_new_id("evt-scope"),
                    event_type=IMPACT_SCOPED,
                    aggregate_type=IMPACT_CASE,
                    aggregate_id=case_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"沿部件序列与施工记录圈定 {cert_id} 暂停影响车辆 {len(candidates)} 个目标",
                    payload={
                        "cert_id": cert_id,
                        "batches": sorted(batches),
                        "purposes": sorted(purposes),
                        "rule_version": 1,
                        "scoped_by": scoped_by,
                        "candidates": candidates,
                    },
                )
            ]

        return self._commit(build)

    # ---- 3. 技术人员确认匹配规则 ------------------------------------------

    def confirm_matching(
        self,
        *,
        case_id: str,
        confirmed: Iterable[str] | None = None,
        excluded: Iterable[str] = (),
        confirmed_by: str,
        at: datetime,
    ) -> Event:
        excluded = set(excluded)

        def build(world: World) -> list[Event]:
            case = world.cases.get(case_id)
            if case is None:
                raise DomainError(f"案卷 {case_id} 尚未圈定")
            candidate_keys = set(case.candidates)
            chosen = candidate_keys if confirmed is None else set(confirmed)
            invalid = chosen - candidate_keys
            if invalid:
                raise DomainError(f"确认目标超出圈定范围：{sorted(invalid)}")
            bad_excluded = excluded - candidate_keys
            if bad_excluded:
                raise DomainError(f"排除目标超出圈定范围：{sorted(bad_excluded)}")
            chosen -= excluded
            if chosen <= case.confirmed and excluded <= case.excluded and case.confirmed_by:
                return []  # 并发重复确认
            version = self.store.version(case_id)
            return [
                Event(
                    event_id=_new_id("evt-match"),
                    event_type=MATCHING_CONFIRMED,
                    aggregate_type=IMPACT_CASE,
                    aggregate_id=case_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"技术人员 {confirmed_by} 确认匹配规则，命中 {len(chosen)} 个目标",
                    payload={
                        "rule_version": 1,
                        "confirmed": sorted(chosen),
                        "excluded": sorted(excluded),
                        "confirmed_by": confirmed_by,
                    },
                )
            ]

        return self._commit_one(build)

    # ---- 4. 合规处置：暂停 / 复检 / 换件 ----------------------------------

    def decide_disposition(
        self,
        *,
        case_id: str,
        kind: str,
        targets: Iterable[str],
        decided_by: str,
        at: datetime,
        due_at: datetime | None = None,
    ) -> list[Event]:
        targets = list(targets)
        if kind not in _DISPOSITION_EVENT:
            raise DomainError(f"未知处置类别：{kind}")

        def build(world: World) -> list[Event]:
            case = world.cases.get(case_id)
            if case is None:
                raise DomainError(f"案卷 {case_id} 不存在")
            pending: list[Event] = []
            to_apply: list[str] = []
            for key in targets:
                if key not in case.confirmed:
                    raise DomainError(f"目标 {key} 未经技术匹配确认，不得处置")
                if case.is_restored(key):
                    continue
                latest = case.latest_disposition(key)
                if latest is not None and latest["kind"] == kind:
                    continue  # 并发/重复决定不叠加
                to_apply.append(key)
            if not to_apply:
                return []
            version = self.store.version(case_id)
            pending.append(
                Event(
                    event_id=_new_id(f"evt-{kind}"),
                    event_type=_DISPOSITION_EVENT[kind],
                    aggregate_type=IMPACT_CASE,
                    aggregate_id=case_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=self._disposition_summary(kind, to_apply, decided_by),
                    payload={
                        "targets": to_apply,
                        "decided_by": decided_by,
                        "due_at": _iso(due_at),
                    },
                )
            )
            # 通知必须送达当前车主；已转卖车辆经所有权投影自然指向新车主。
            for vin in {key.split("@", 1)[0] for key in to_apply}:
                owner = world.current_owner(vin, at)
                v = self.store.version(vin)
                pending.append(
                    Event(
                        event_id=_new_id("evt-notice"),
                        event_type=NOTIFICATION_DELIVERED,
                        aggregate_type=OWNERSHIP_HISTORY,
                        aggregate_id=vin,
                        occurred_at=at,
                        version=v + 1,
                        summary=f"向当前车主送达暂停处置通知（案卷 {case_id}）",
                        payload={
                            "vin": vin,
                            "case_id": case_id,
                            "recipient": owner,
                            "kind": "current_notice",
                            "reference": sorted(t for t in to_apply if t.startswith(vin + "@")),
                        },
                    )
                )
            return pending

        return self._commit(build)

    @staticmethod
    def _disposition_summary(kind: str, targets: list[str], decided_by: str) -> str:
        if kind == DISPOSITION_HOLD:
            return f"合规人员 {decided_by} 对 {len(targets)} 个目标施加安全暂停限制"
        if kind == DISPOSITION_REINSPECT:
            return f"合规人员 {decided_by} 要求 {len(targets)} 个目标复检"
        return f"合规人员 {decided_by} 要求 {len(targets)} 个目标换件"

    # ---- 5. 申诉：暂缓行政结论，不解除安全限制 ----------------------------

    def file_appeal(
        self,
        *,
        appeal_id: str,
        case_id: str,
        targets: Iterable[str],
        filed_by: str,
        at: datetime,
    ) -> Event:
        targets = list(targets)

        def build(world: World) -> list[Event]:
            case = world.cases.get(case_id)
            if case is None:
                raise DomainError(f"案卷 {case_id} 不存在")
            bad = [t for t in targets if t not in case.confirmed]
            if bad:
                raise DomainError(f"申诉目标超出确认范围：{bad}")
            if appeal_id in world.appeals:
                return []
            version = self.store.version(appeal_id)
            return [
                Event(
                    event_id=_new_id("evt-appeal"),
                    event_type=APPEAL_FILED,
                    aggregate_type=APPEAL,
                    aggregate_id=appeal_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"{filed_by} 就案卷 {case_id} 的 {len(targets)} 个目标提出申诉",
                    payload={
                        "appeal_id": appeal_id,
                        "case_id": case_id,
                        "targets": targets,
                        "filed_by": filed_by,
                    },
                )
            ]

        return self._commit_one(build)

    def stay_administrative(self, *, appeal_id: str, at: datetime, note: str = "") -> Event:
        def build(world: World) -> list[Event]:
            appeal = world.appeals.get(appeal_id)
            if appeal is None:
                raise DomainError(f"申诉 {appeal_id} 不存在")
            if appeal.stayed:
                return []
            version = self.store.version(appeal_id)
            return [
                Event(
                    event_id=_new_id("evt-stay"),
                    event_type=ADMINISTRATIVE_STAYED,
                    aggregate_type=APPEAL,
                    aggregate_id=appeal_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"申诉 {appeal_id} 获准，暂缓行政结论；安全限制继续有效。{note}",
                    payload={},
                )
            ]

        return self._commit_one(build)

    def resume_administrative(self, *, appeal_id: str, at: datetime) -> Event:
        def build(world: World) -> list[Event]:
            appeal = world.appeals.get(appeal_id)
            if appeal is None:
                raise DomainError(f"申诉 {appeal_id} 不存在")
            if not appeal.stayed:
                return []
            version = self.store.version(appeal_id)
            return [
                Event(
                    event_id=_new_id("evt-admin-resume"),
                    event_type=ADMINISTRATIVE_RESUMED,
                    aggregate_type=APPEAL,
                    aggregate_id=appeal_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"申诉 {appeal_id} 程序结束，恢复行政结论程序",
                    payload={},
                )
            ]

        return self._commit_one(build)

    # ---- 6. 检测机构（含离线）回传 ----------------------------------------

    def receive_inspection_report(
        self,
        *,
        report_no: str,
        vin: str,
        serial_no: str,
        lab_id: str,
        result: str,
        content_hash: str,
        received_at: datetime,
        channel: str = "online",
    ) -> list[Event]:
        """接收检测报告。

        相同 report_no + 相同 content_hash：只接受一次（幂等丢弃重复回传）。
        相同 report_no + 不同 content_hash：新旧证据并存，另立独立争议，
        绝不覆盖先前证据。
        """

        def build(world: World) -> list[Event]:
            evidence = world.evidences.get(report_no)
            if evidence is not None:
                hashes = {r["content_hash"] for r in evidence.reports}
                if content_hash in hashes:
                    return []  # 相同编号相同内容只收一次
            version = self.store.version(report_no)
            events = [
                Event(
                    event_id=_new_id("evt-report"),
                    event_type=INSPECTION_REPORT_RECEIVED,
                    aggregate_type=INSPECTION_EVIDENCE,
                    aggregate_id=report_no,
                    occurred_at=received_at,
                    version=version + 1,
                    summary=f"收到检测机构 {lab_id} 报告 {report_no}（{channel}）",
                    payload={
                        "report_no": report_no,
                        "vin": vin,
                        "serial_no": serial_no,
                        "lab_id": lab_id,
                        "result": result,
                        "content_hash": content_hash,
                        "channel": channel,
                    },
                )
            ]
            if evidence is not None and content_hash not in {r["content_hash"] for r in evidence.reports}:
                prior = evidence.reports[-1]
                events.append(
                    Event(
                        event_id=_new_id("evt-dispute"),
                        event_type=INSPECTION_DISPUTED,
                        aggregate_type=INSPECTION_EVIDENCE,
                        aggregate_id=report_no,
                        occurred_at=received_at,
                        version=version + 2,
                        summary=f"报告 {report_no} 离线回传内容变化，形成独立争议，原证据保留",
                        payload={
                            "report_no": report_no,
                            "prior_content_hash": prior["content_hash"],
                            "new_content_hash": content_hash,
                            "prior_result": prior["result"],
                            "new_result": result,
                        },
                    )
                )
            return events

        return self._commit(build)

    def sign_reinspection(
        self,
        *,
        case_id: str,
        target: str,
        serial_no: str,
        report_no: str,
        result: str,
        signed_by: str,
        at: datetime,
    ) -> Event:
        def build(world: World) -> list[Event]:
            case = world.cases.get(case_id)
            if case is None:
                raise DomainError(f"案卷 {case_id} 不存在")
            if target not in case.confirmed:
                raise DomainError(f"目标 {target} 不在案卷内")
            evidence = world.evidences.get(report_no)
            if evidence is None or not evidence.reports:
                raise DomainError(f"检测报告 {report_no} 尚未回传，不能签署复检结论")
            if not any(r["serial_no"] == serial_no and r["content_hash"] for r in evidence.reports):
                raise DomainError(f"报告 {report_no} 与部件 {serial_no} 不匹配")
            if any(r["target"] == target and r["report_no"] == report_no for r in case.reinspections):
                return []
            version = self.store.version(case_id)
            return [
                Event(
                    event_id=_new_id("evt-reinspect"),
                    event_type=REINSPECTION_SIGNED,
                    aggregate_type=IMPACT_CASE,
                    aggregate_id=case_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"{signed_by} 依据报告 {report_no} 签署 {target} 复检结论：{result}",
                    payload={
                        "target": target,
                        "serial_no": serial_no,
                        "report_no": report_no,
                        "result": result,
                        "signed_by": signed_by,
                    },
                )
            ]

        return self._commit_one(build)

    # ---- 7. 换件 ----------------------------------------------------------

    def replace_part(
        self,
        *,
        case_id: str,
        vin: str,
        old_serial_no: str,
        new_serial_no: str,
        new_cert_id: str,
        new_batch_no: str,
        purpose: str,
        technician: str,
        shop: str,
        work_id: str,
        at: datetime,
    ) -> Event:
        key = target_key(vin, old_serial_no)

        def build(world: World) -> list[Event]:
            case = world.cases.get(case_id)
            if case is None:
                raise DomainError(f"案卷 {case_id} 不存在")
            disposition = case.latest_disposition(key)
            if disposition is None or disposition["kind"] != DISPOSITION_REPLACE:
                raise DomainError(f"目标 {key} 未被要求换件")
            if case.is_restored(key):
                return []
            vehicle = world.vehicles.get(vin)
            if vehicle is None or old_serial_no not in vehicle.parts:
                raise DomainError(f"车辆 {vin} 上找不到部件 {old_serial_no}")
            # 新件不得落入任何仍有效的暂停范围。
            for cert_id, cert in world.certs.items():
                susp = cert.active_suspension
                if susp is None:
                    continue
                if new_cert_id != cert_id:
                    continue
                if susp["batches"] and new_batch_no not in susp["batches"]:
                    continue
                if susp["purposes"] and purpose not in susp["purposes"]:
                    continue
                raise DomainError("新件仍处于暂停认证的批次/用途范围内，不得装车")
            if new_serial_no in vehicle.parts and vehicle.parts[new_serial_no].installed:
                raise DomainError(f"部件 {new_serial_no} 已在车使用")
            version = self.store.version(vin)
            return [
                Event(
                    event_id=_new_id("evt-replaced"),
                    event_type=PART_REPLACED,
                    aggregate_type=VEHICLE_PROFILE,
                    aggregate_id=vin,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"{shop} 由 {technician} 将 {old_serial_no} 换为 {new_serial_no}",
                    payload={
                        "case_id": case_id,
                        "vin": vin,
                        "old_serial_no": old_serial_no,
                        "new_serial_no": new_serial_no,
                        "part_model": vehicle.parts[old_serial_no].part_model,
                        "new_cert_id": new_cert_id,
                        "new_batch_no": new_batch_no,
                        "purpose": purpose,
                        "technician": technician,
                        "shop": shop,
                        "work_id": work_id,
                    },
                )
            ]

        return self._commit_one(build)

    # ---- 8. 另一人凭新检测恢复 --------------------------------------------

    def restore(
        self,
        *,
        case_id: str,
        targets: Iterable[str],
        restored_by: str,
        report_no: str,
        at: datetime,
    ) -> Event:
        targets = list(targets)

        def build(world: World) -> list[Event]:
            case = world.cases.get(case_id)
            if case is None:
                raise DomainError(f"案卷 {case_id} 不存在")
            evidence = world.evidences.get(report_no)
            if evidence is None or not any(r["result"] == "pass" for r in evidence.reports):
                raise DomainError(f"报告 {report_no} 不存在或结论不合格，不能作为恢复依据")
            eligible: list[str] = []
            for key in targets:
                disposition = case.latest_disposition(key)
                if disposition is None:
                    raise DomainError(f"目标 {key} 没有在案处置，无需恢复")
                if case.is_restored(key):
                    raise DomainError(f"目标 {key} 已恢复，不得重复恢复")
                self._check_restore_basis(world, case, key, disposition, report_no, restored_by)
                eligible.append(key)
            if not eligible:
                return []
            version = self.store.version(case_id)
            return [
                Event(
                    event_id=_new_id("evt-restore"),
                    event_type=CERTIFICATION_EFFECT_RESTORED,
                    aggregate_type=IMPACT_CASE,
                    aggregate_id=case_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"{restored_by} 依据新检测 {report_no} 恢复 {len(eligible)} 个目标的认证效力",
                    payload={
                        "targets": sorted(eligible),
                        "restored_by": restored_by,
                        "basis": {"report_no": report_no, "result": "pass"},
                    },
                )
            ]

        return self._commit_one(build)

    def _check_restore_basis(
        self, world: World, case: Any, key: str, disposition: dict, report_no: str, restored_by: str
    ) -> None:
        if restored_by == disposition["decided_by"]:
            raise DomainError(f"恢复人 {restored_by} 不得与处置决定人相同")
        signed = [r for r in case.reinspections if r["target"] == key and r["report_no"] == report_no]
        if not signed:
            raise DomainError(f"目标 {key} 缺少基于报告 {report_no} 的新复检结论")
        basis = signed[-1]
        if basis["result"] != "pass":
            raise DomainError(f"目标 {key} 复检结论不合格，不能恢复")
        if basis["at"] <= disposition["at"]:
            raise DomainError("必须依据处置之后形成的新检测结果恢复")
        if restored_by == basis["signed_by"]:
            raise DomainError("恢复人不得与复检签署人相同")
        vin, serial = key.split("@", 1)
        if disposition["kind"] == DISPOSITION_REPLACE:
            replacements = world.replacements.get(key, [])
            if not replacements or replacements[-1]["at"] <= disposition["at"]:
                raise DomainError(f"目标 {key} 尚未完成换件")
            replacement = replacements[-1]
            if restored_by == replacement["technician"]:
                raise DomainError("换件完成后必须由另一人恢复")
            if basis["serial_no"] != replacement["new_serial_no"]:
                raise DomainError("恢复依据必须是换件后新部件的检测结果")
            if basis["at"] <= replacement["at"]:
                raise DomainError("新检测必须在换件完成之后进行")

    # ---- 9. 所有权转移：通知新车主，保留责任时间线 ------------------------

    def transfer_ownership(
        self,
        *,
        vin: str,
        from_owner: str,
        to_owner: str,
        at: datetime,
    ) -> list[Event]:
        def build(world: World) -> list[Event]:
            vehicle = world.vehicles.get(vin)
            if vehicle is None:
                raise DomainError(f"车辆 {vin} 不存在")
            current = world.current_owner(vin, at)
            if current is not None and current != from_owner:
                raise DomainError(f"登记当前车主为 {current}，与 {from_owner} 不符")
            base = self.store.version(vin)
            events = [
                Event(
                    event_id=_new_id("evt-transfer"),
                    event_type=OWNERSHIP_TRANSFERRED,
                    aggregate_type=OWNERSHIP_HISTORY,
                    aggregate_id=vin,
                    occurred_at=at,
                    version=base + 1,
                    summary=f"车辆 {vin} 所有权由 {from_owner} 转移给 {to_owner}；责任时间线延续",
                    payload={
                        "vin": vin,
                        "from_owner": from_owner,
                        "to_owner": to_owner,
                    },
                )
            ]
            # 仍在安全限制下的案卷，当前通知改送新车主；历史通知与责任不删除。
            holdings = {h["case_id"]: h for h in world.active_holdings(vin)}
            for case_id, holding in holdings.items():
                events.append(
                    Event(
                        event_id=_new_id("evt-notice-newowner"),
                        event_type=NOTIFICATION_DELIVERED,
                        aggregate_type=OWNERSHIP_HISTORY,
                        aggregate_id=vin,
                        occurred_at=at,
                        version=base + len(events) + 1,
                        summary=f"车辆转售后，向新车主 {to_owner} 送达当前安全限制通知（案卷 {case_id}）",
                        payload={
                            "vin": vin,
                            "case_id": case_id,
                            "recipient": to_owner,
                            "kind": "current_notice_new_owner",
                            "reference": holding["target"],
                        },
                    )
                )
            return events

        return self._commit(build)

    # ---- 10. 整改/升级进程时钟：暂停不重算截止点 --------------------------

    def suspend_process(
        self, *, clock_id: str, case_id: str, reason: str, original_due_at: datetime, at: datetime
    ) -> Event:
        def build(world: World) -> list[Event]:
            existing = world.clocks.get(clock_id)
            if existing is not None and not existing.running:
                return []
            due = existing.original_due_at if existing else original_due_at
            version = self.store.version(clock_id)
            return [
                Event(
                    event_id=_new_id("evt-clock-susp"),
                    event_type=PROCESS_SUSPENDED,
                    aggregate_type=PROCESS_CLOCK,
                    aggregate_id=clock_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"进程暂停（{reason}），整改截止点仍为 {due.isoformat()}",
                    payload={"case_id": case_id, "reason": reason, "original_due_at": _iso(due)},
                )
            ]

        return self._commit_one(build)

    def resume_process(self, *, clock_id: str, case_id: str, at: datetime) -> Event:
        def build(world: World) -> list[Event]:
            clock = world.clocks.get(clock_id)
            if clock is None:
                raise DomainError(f"进程时钟 {clock_id} 不存在")
            if clock.running:
                return []
            version = self.store.version(clock_id)
            return [
                Event(
                    event_id=_new_id("evt-clock-resume"),
                    event_type=PROCESS_RESUMED,
                    aggregate_type=PROCESS_CLOCK,
                    aggregate_id=clock_id,
                    occurred_at=at,
                    version=version + 1,
                    summary=f"进程恢复，继续按原截止点 {clock.original_due_at.isoformat() if clock.original_due_at else ''} 计时，不重新计时",
                    payload={"case_id": case_id},
                )
            ]

        return self._commit_one(build)
