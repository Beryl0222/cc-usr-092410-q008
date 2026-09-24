"""全链路场景测试。

覆盖：
1) 同车型不同批次/用途/状态下的精确圈定（未施工、待检测、已发证、已转卖）；
2) 技术确认 → 合规处置 → 复检/换件 → 另一人恢复 的职责分离；
3) 申诉只暂缓行政结论、不解除安全限制；
4) 转卖通知新车主，原施工方与历任车主责任时间线保留；
5) 离线回传幂等与"同编号内容变化→独立争议、不覆盖原证据"；
6) 整改/升级期限按原截止点延续，恢复不重新计时；
7) 通行证/车辆/部件三入口链路一致；
8) 并发复核不重复不扩大、部分恢复不外溢。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone, timedelta

from src.domain import (
    STATE_NOT_INSTALLED,
    STATE_PASS_ACTIVE,
    STATE_PENDING_INSPECTION,
    STATE_RESOLD,
    World,
    target_key,
)
from src.events import (
    CERTIFICATION_SUSPENDED,
    IMPACT_SCOPED,
    INSPECTION_DISPUTED,
    INSPECTION_REPORT_RECEIVED,
    NOTIFICATION_DELIVERED,
    OWNERSHIP_TRANSFERRED,
    PART_REPLACED,
)
from src.projections import TraceQuery
from src.service import ComplianceService, DomainError
from src.store import DuplicateEventError, EventStore

TZ = timezone(timedelta(hours=8))


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ) if "T" in text else datetime.fromisoformat(text)


class ScenarioTest(unittest.TestCase):
    # ---- 夹具 --------------------------------------------------------------

    def setUp(self) -> None:
        self.store = EventStore()
        self.svc = ComplianceService(self.store)

        self.cert = "CERT-BRK-01"
        self.model = "MX-7"
        self.part_model = "BRK-X"
        self.bad_batch = "B-2026-08"

        self.vins = {
            "A": "VIN-A",  # 受影响批次，已发证
            "B": "VIN-B",  # 同车型，干净批次，不得纳入
            "C": "VIN-C",  # 受影响批次，未施工
            "D": "VIN-D",  # 受影响批次，待检测
            "E": "VIN-E",  # 受影响批次但用途不同（赛道），不得纳入
            "F": "VIN-F",  # 受影响批次，已转卖
        }
        self.serials = {k: f"S-{k}" for k in self.vins}

        self._build_fleet()

    def _build_fleet(self) -> None:
        s = self.svc
        configs = {
            "A": (self.bad_batch, "street", "owner-a"),
            "B": ("B-2026-07", "street", "owner-b"),
            "C": (self.bad_batch, "street", "owner-c"),
            "D": (self.bad_batch, "street", "owner-d"),
            "E": (self.bad_batch, "track", "owner-e"),
            "F": (self.bad_batch, "street", "owner-f0"),
        }
        for k, (batch, purpose, owner) in configs.items():
            vin, serial = self.vins[k], self.serials[k]
            s.declare_vehicle(vin=vin, vehicle_model=self.model, owner=owner, at=dt("2026-08-01T09:00:00+08:00"))
            s.bind_part(
                vin=vin, serial_no=serial, part_model=self.part_model,
                cert_id=self.cert, batch_no=batch, purpose=purpose,
                at=dt("2026-08-02T09:00:00+08:00"),
            )

        # A、B、D、E、F 已施工；C 只绑定未施工。
        for k in ("A", "B", "D", "E", "F"):
            self.svc.attest_work(
                vin=self.vins[k], serial_no=self.serials[k], work_id=f"W-{k}",
                technician=f"tech-{k.lower()}", shop="改装一厂",
                at=dt("2026-08-05T10:00:00+08:00"),
            )
        # A、B、E 初检合格并发证；D 施工后尚未检测；F 也已发证。
        for k in ("A", "B", "E", "F"):
            self.svc.sign_initial_inspection(
                vin=self.vins[k], serial_no=self.serials[k], report_no=f"R-INIT-{k}",
                result="pass", inspector="insp-zhao", lab_id="LAB-1",
                at=dt("2026-08-08T10:00:00+08:00"),
            )
            self.svc.activate_pass(
                pass_id=f"PASS-{k}", vin=self.vins[k], at=dt("2026-08-10T10:00:00+08:00")
            )

        self.susp_effective = dt("2026-09-10T08:00:00+08:00")
        self.due = dt("2026-10-01T18:00:00+08:00")
        self.svc.suspend_certification(
            cert_id=self.cert, part_model=self.part_model,
            affected_batches=[self.bad_batch], purposes=["street"],
            reason="台架复验发现热衰退超标", effective_at=self.susp_effective,
            original_due_at=self.due, suspended_by="cert-officer",
        )

        # F 在暂停生效后转卖。
        self.svc.transfer_ownership(
            vin=self.vins["F"], from_owner="owner-f0", to_owner="owner-f1",
            at=dt("2026-09-12T11:00:00+08:00"),
        )

        self.case_id = "CASE-1"
        self.svc.scope_impact(
            case_id=self.case_id, cert_id=self.cert,
            at=dt("2026-09-13T09:00:00+08:00"), scoped_by="analyst-1",
        )

    def world(self) -> World:
        return World.replay(self.store.all_events())

    def case(self) -> "object":
        return self.world().cases[self.case_id]

    # ---- 1. 精确圈定 -------------------------------------------------------

    def test_scoping_is_per_batch_purpose_and_state_not_per_model(self) -> None:
        case = self.case()
        keys = set(case.candidates)
        self.assertEqual(
            keys,
            {target_key(self.vins[k], self.serials[k]) for k in ("A", "C", "D", "F")},
        )
        # 同车型干净批次 B、用途不同 E 绝不纳入。
        self.assertNotIn(target_key(self.vins["B"], self.serials["B"]), keys)
        self.assertNotIn(target_key(self.vins["E"], self.serials["E"]), keys)

        states = {key: c["state"] for key, c in case.candidates.items()}
        self.assertEqual(states[target_key(self.vins["A"], self.serials["A"])], STATE_PASS_ACTIVE)
        self.assertEqual(states[target_key(self.vins["C"], self.serials["C"])], STATE_NOT_INSTALLED)
        self.assertEqual(states[target_key(self.vins["D"], self.serials["D"])], STATE_PENDING_INSPECTION)
        self.assertEqual(states[target_key(self.vins["F"], self.serials["F"])], STATE_RESOLD)
        # 转卖车辆的当前车主被正确投影。
        f_candidate = case.candidates[target_key(self.vins["F"], self.serials["F"])]
        self.assertEqual(f_candidate["current_owner"], "owner-f1")

    def test_scope_is_idempotent_and_immutable(self) -> None:
        before = len(self.store.all_events())
        again = self.svc.scope_impact(
            case_id=self.case_id, cert_id=self.cert,
            at=dt("2026-09-13T10:00:00+08:00"), scoped_by="analyst-1",
        )
        self.assertEqual(again, [])
        self.assertEqual(len(self.store.all_events()), before)
        scoped = [e for e in self.store.all_events() if e.event_type == IMPACT_SCOPED]
        self.assertEqual(len(scoped), 1)

    # ---- 2. 处置必须经技术确认，且不能超范围 ------------------------------

    def test_disposition_requires_confirmed_match_and_cannot_expand(self) -> None:
        key_b = target_key(self.vins["B"], self.serials["B"])
        with self.assertRaises(DomainError):
            self.svc.decide_disposition(
                case_id=self.case_id, kind="hold", targets=[key_b],
                decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
            )
        # 技术人员可在确认时排除个别候选（规则复核）。
        key_c = target_key(self.vins["C"], self.serials["C"])
        self.svc.confirm_matching(
            case_id=self.case_id,
            excluded=[key_c],
            confirmed_by="tech-wang", at=dt("2026-09-13T11:00:00+08:00"),
        )
        case = self.case()
        self.assertNotIn(key_c, case.confirmed)
        with self.assertRaises(DomainError):
            self.svc.decide_disposition(
                case_id=self.case_id, kind="hold", targets=[key_c],
                decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
            )

    # ---- 3. 申诉暂缓行政结论但不解除安全限制 ------------------------------

    def test_appeal_stays_administration_but_holding_remains_effective(self) -> None:
        key_a = target_key(self.vins["A"], self.serials["A"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="hold", targets=[key_a],
            decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
            due_at=self.due,
        )
        self.assertEqual(len(self.world().active_holdings(self.vins["A"])), 1)

        self.svc.file_appeal(
            appeal_id="APPEAL-1", case_id=self.case_id, targets=[key_a],
            filed_by="owner-a", at=dt("2026-09-14T09:00:00+08:00"),
        )
        self.svc.stay_administrative(
            appeal_id="APPEAL-1", at=dt("2026-09-15T09:00:00+08:00")
        )
        world = self.world()
        self.assertTrue(world.appeals["APPEAL-1"].stayed)
        # 关键：行政结论暂缓，安全限制仍在。
        self.assertEqual(len(world.active_holdings(self.vins["A"])), 1)

        self.svc.resume_administrative(
            appeal_id="APPEAL-1", at=dt("2026-09-20T09:00:00+08:00")
        )
        self.assertFalse(self.world().appeals["APPEAL-1"].stayed)
        self.assertEqual(len(self.world().active_holdings(self.vins["A"])), 1)

    # ---- 4. 复检 + 另一人恢复 ---------------------------------------------

    def test_reinspection_then_restore_by_different_person(self) -> None:
        key_d = target_key(self.vins["D"], self.serials["D"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="reinspect", targets=[key_d],
            decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
        )
        # 无新检测不得恢复。
        with self.assertRaises(DomainError):
            self.svc.restore(
                case_id=self.case_id, targets=[key_d], restored_by="reviewer-sun",
                report_no="R-NEW-D", at=dt("2026-09-16T09:00:00+08:00"),
            )

        self.svc.receive_inspection_report(
            report_no="R-NEW-D", vin=self.vins["D"], serial_no=self.serials["D"],
            lab_id="LAB-1", result="pass", content_hash="hash-d-pass",
            received_at=dt("2026-09-16T08:00:00+08:00"),
        )
        self.svc.sign_reinspection(
            case_id=self.case_id, target=key_d, serial_no=self.serials["D"],
            report_no="R-NEW-D", result="pass", signed_by="insp-zhao",
            at=dt("2026-09-16T09:00:00+08:00"),
        )
        # 处置人本人不能恢复。
        with self.assertRaises(DomainError):
            self.svc.restore(
                case_id=self.case_id, targets=[key_d], restored_by="compliance-li",
                report_no="R-NEW-D", at=dt("2026-09-16T10:00:00+08:00"),
            )
        # 复检签署人也不能恢复。
        with self.assertRaises(DomainError):
            self.svc.restore(
                case_id=self.case_id, targets=[key_d], restored_by="insp-zhao",
                report_no="R-NEW-D", at=dt("2026-09-16T10:00:00+08:00"),
            )
        self.svc.restore(
            case_id=self.case_id, targets=[key_d], restored_by="reviewer-sun",
            report_no="R-NEW-D", at=dt("2026-09-16T10:00:00+08:00"),
        )
        self.assertEqual(self.world().active_holdings(self.vins["D"]), [])
        # 重复恢复被拒绝。
        with self.assertRaises(DomainError):
            self.svc.restore(
                case_id=self.case_id, targets=[key_d], restored_by="reviewer-sun",
                report_no="R-NEW-D", at=dt("2026-09-16T11:00:00+08:00"),
            )

    def test_failed_reinspection_cannot_restore(self) -> None:
        key_d = target_key(self.vins["D"], self.serials["D"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="reinspect", targets=[key_d],
            decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
        )
        self.svc.receive_inspection_report(
            report_no="R-NEW-D2", vin=self.vins["D"], serial_no=self.serials["D"],
            lab_id="LAB-1", result="fail", content_hash="hash-d-fail",
            received_at=dt("2026-09-16T08:00:00+08:00"),
        )
        self.svc.sign_reinspection(
            case_id=self.case_id, target=key_d, serial_no=self.serials["D"],
            report_no="R-NEW-D2", result="fail", signed_by="insp-zhao",
            at=dt("2026-09-16T09:00:00+08:00"),
        )
        with self.assertRaises(DomainError):
            self.svc.restore(
                case_id=self.case_id, targets=[key_d], restored_by="reviewer-sun",
                report_no="R-NEW-D2", at=dt("2026-09-16T10:00:00+08:00"),
            )

    # ---- 5. 换件 → 新件检测 → 另一人恢复 ----------------------------------

    def test_replace_part_new_inspection_and_segregated_restore(self) -> None:
        key_f = target_key(self.vins["F"], self.serials["F"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="replace", targets=[key_f],
            decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
        )
        # 换件前不能恢复。
        with self.assertRaises(DomainError):
            self.svc.restore(
                case_id=self.case_id, targets=[key_f], restored_by="reviewer-sun",
                report_no="R-NEW-F", at=dt("2026-09-18T10:00:00+08:00"),
            )
        # 不允许换上仍在暂停批次/用途内的部件。
        with self.assertRaises(DomainError):
            self.svc.replace_part(
                case_id=self.case_id, vin=self.vins["F"],
                old_serial_no=self.serials["F"], new_serial_no="S-F-BAD",
                new_cert_id=self.cert, new_batch_no=self.bad_batch, purpose="street",
                technician="tech-f", shop="改装二厂", work_id="W-F2",
                at=dt("2026-09-17T09:00:00+08:00"),
            )

        self.svc.replace_part(
            case_id=self.case_id, vin=self.vins["F"],
            old_serial_no=self.serials["F"], new_serial_no="S-F2",
            new_cert_id="CERT-BRK-09", new_batch_no="B-2026-09", purpose="street",
            technician="tech-f", shop="改装二厂", work_id="W-F2",
            at=dt("2026-09-17T09:00:00+08:00"),
        )
        # 旧件下拆、新件在岗。
        world = self.world()
        self.assertFalse(world.vehicles[self.vins["F"]].parts[self.serials["F"]].installed)
        self.assertTrue(world.vehicles[self.vins["F"]].parts["S-F2"].installed)

        # 用旧件检测不能恢复。
        self.svc.receive_inspection_report(
            report_no="R-OLD-F", vin=self.vins["F"], serial_no=self.serials["F"],
            lab_id="LAB-1", result="pass", content_hash="hash-old-f",
            received_at=dt("2026-09-17T15:00:00+08:00"),
        )
        self.svc.sign_reinspection(
            case_id=self.case_id, target=key_f, serial_no=self.serials["F"],
            report_no="R-OLD-F", result="pass", signed_by="insp-zhao",
            at=dt("2026-09-17T16:00:00+08:00"),
        )
        with self.assertRaises(DomainError):
            self.svc.restore(
                case_id=self.case_id, targets=[key_f], restored_by="reviewer-sun",
                report_no="R-OLD-F", at=dt("2026-09-17T17:00:00+08:00"),
            )

        # 新件检测合格后，由另一人恢复（非处置人、非签署人、非施工人）。
        self.svc.receive_inspection_report(
            report_no="R-NEW-F", vin=self.vins["F"], serial_no="S-F2",
            lab_id="LAB-1", result="pass", content_hash="hash-new-f",
            received_at=dt("2026-09-18T08:00:00+08:00"),
        )
        self.svc.sign_reinspection(
            case_id=self.case_id, target=key_f, serial_no="S-F2",
            report_no="R-NEW-F", result="pass", signed_by="insp-zhao",
            at=dt("2026-09-18T09:00:00+08:00"),
        )
        with self.assertRaises(DomainError):  # 施工人本人不能恢复
            self.svc.restore(
                case_id=self.case_id, targets=[key_f], restored_by="tech-f",
                report_no="R-NEW-F", at=dt("2026-09-18T10:00:00+08:00"),
            )
        self.svc.restore(
            case_id=self.case_id, targets=[key_f], restored_by="reviewer-sun",
            report_no="R-NEW-F", at=dt("2026-09-18T10:00:00+08:00"),
        )
        self.assertEqual(self.world().active_holdings(self.vins["F"]), [])

    # ---- 6. 转卖通知与责任时间线 ------------------------------------------

    def test_transfer_notifies_new_owner_but_keeps_responsibility_timeline(self) -> None:
        key_a = target_key(self.vins["A"], self.serials["A"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="hold", targets=[key_a],
            decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
        )
        # 处置后转卖：当前通知必须送新车主。
        self.svc.transfer_ownership(
            vin=self.vins["A"], from_owner="owner-a", to_owner="owner-a2",
            at=dt("2026-09-14T14:00:00+08:00"),
        )
        notes = [n for n in self.world().notifications[self.vins["A"]]]
        self.assertIn("owner-a2", {n["recipient"] for n in notes})
        new_owner_notes = [n for n in notes if n["kind"] == "current_notice_new_owner"]
        self.assertEqual(len(new_owner_notes), 1)
        # 安全限制跟随车辆，不随交易消失。
        self.assertEqual(len(self.world().active_holdings(self.vins["A"])), 1)

        # 责任时间线：历任车主区间完整；原施工方仍记录在部件履历中。
        periods = self.world().ownership_periods(self.vins["A"])
        self.assertEqual([p["owner"] for p in periods], ["owner-a", "owner-a2"])
        self.assertIsNone(periods[-1]["to"])
        part = self.world().vehicles[self.vins["A"]].parts[self.serials["A"]]
        self.assertEqual(part.technician, "tech-a")
        self.assertEqual(part.shop, "改装一厂")

    # ---- 7. 离线回传：幂等 + 独立争议 -------------------------------------

    def test_offline_report_dedupe_and_dispute_never_overwrites(self) -> None:
        first = self.svc.receive_inspection_report(
            report_no="R-OFF-1", vin=self.vins["D"], serial_no=self.serials["D"],
            lab_id="LAB-2", result="pass", content_hash="h1", channel="offline",
            received_at=dt("2026-09-16T07:00:00+08:00"),
        )
        self.assertEqual(len(first), 1)
        # 相同编号相同内容：重复离线回传只接受一次。
        again = self.svc.receive_inspection_report(
            report_no="R-OFF-1", vin=self.vins["D"], serial_no=self.serials["D"],
            lab_id="LAB-2", result="pass", content_hash="h1", channel="offline",
            received_at=dt("2026-09-16T07:30:00+08:00"),
        )
        self.assertEqual(again, [])

        # 相同编号、内容变化：并存并形成独立争议。
        changed = self.svc.receive_inspection_report(
            report_no="R-OFF-1", vin=self.vins["D"], serial_no=self.serials["D"],
            lab_id="LAB-2", result="fail", content_hash="h2", channel="offline",
            received_at=dt("2026-09-16T08:30:00+08:00"),
        )
        self.assertEqual({e.event_type for e in changed},
                         {INSPECTION_REPORT_RECEIVED, INSPECTION_DISPUTED})
        evidence = self.world().evidences["R-OFF-1"]
        self.assertEqual([r["content_hash"] for r in evidence.reports], ["h1", "h2"])
        self.assertEqual(len(evidence.disputes), 1)
        self.assertEqual(evidence.disputes[0]["prior_result"], "pass")
        self.assertEqual(evidence.disputes[0]["new_result"], "fail")
        # 先前证据未被覆盖。
        self.assertEqual(evidence.reports[0]["result"], "pass")

    # ---- 8. 期限按原截止点延续 --------------------------------------------

    def test_process_clock_keeps_original_due_after_resume(self) -> None:
        clock_id = "CLOCK-1"
        self.svc.suspend_process(
            clock_id=clock_id, case_id=self.case_id, reason="等待机构离线数据",
            original_due_at=self.due, at=dt("2026-09-14T09:00:00+08:00"),
        )
        self.svc.resume_process(
            clock_id=clock_id, case_id=self.case_id, at=dt("2026-09-22T09:00:00+08:00"),
        )
        clock = self.world().clocks[clock_id]
        self.assertTrue(clock.running)
        self.assertEqual(clock.due_at, self.due)
        self.assertEqual(clock.original_due_at, self.due)

    # ---- 9. 三入口一致 -----------------------------------------------------

    def test_trace_consistent_from_pass_vin_and_serial(self) -> None:
        key_a = target_key(self.vins["A"], self.serials["A"])
        key_f = target_key(self.vins["F"], self.serials["F"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="hold", targets=[key_a],
            decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="replace", targets=[key_f],
            decided_by="compliance-li", at=dt("2026-09-13T12:05:00+08:00"),
        )

        query = TraceQuery(self.world())
        trace_pass = query.trace("pass", "PASS-A")
        trace_vin = query.trace("vin", self.vins["A"])
        trace_serial = query.trace("serial", self.serials["A"])
        self.assertIn(self.case_id, trace_pass["case_ids"])
        self.assertTrue(
            query.consistent_across_entries(
                pass_id="PASS-A", vin=self.vins["A"], serial_no=self.serials["A"]
            )
        )
        stages = {item["stage"] for item in trace_vin["chain"]}
        for expected in ("certification_change", "work", "inspection", "disposition"):
            self.assertIn(expected, stages)
        # 换件后，从新部件序列入口也能追溯到同一案卷。
        self.svc.replace_part(
            case_id=self.case_id, vin=self.vins["F"],
            old_serial_no=self.serials["F"], new_serial_no="S-F2",
            new_cert_id="CERT-BRK-09", new_batch_no="B-2026-09", purpose="street",
            technician="tech-f", shop="改装二厂", work_id="W-F2",
            at=dt("2026-09-17T09:00:00+08:00"),
        )
        trace_new_serial = TraceQuery(self.world()).trace("serial", "S-F2")
        self.assertIn(self.case_id, trace_new_serial["case_ids"])

    # ---- 10. 并发复核 ------------------------------------------------------

    def test_concurrent_dispositions_apply_once_and_never_expand(self) -> None:
        key_c = target_key(self.vins["C"], self.serials["C"])
        key_d = target_key(self.vins["D"], self.serials["D"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        errors: list[Exception] = []

        def decide(target: str, kind: str) -> None:
            try:
                self.svc.decide_disposition(
                    case_id=self.case_id, kind=kind, targets=[target],
                    decided_by=f"compliance-{kind}", at=dt("2026-09-13T12:00:00+08:00"),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=decide, args=(key_c, "hold"))
        t2 = threading.Thread(target=decide, args=(key_d, "reinspect"))
        t3 = threading.Thread(target=decide, args=(key_c, "hold"))  # 重复决定
        t1.start(); t2.start(); t3.start()
        t1.join(); t2.join(); t3.join()
        self.assertEqual(errors, [])

        case = self.case()
        self.assertEqual([d["kind"] for d in case.dispositions[key_c]], ["hold"])
        self.assertEqual([d["kind"] for d in case.dispositions[key_d]], ["reinspect"])
        # 范围始终是四个候选，未因并发扩大。
        self.assertEqual(set(case.candidates),
                         {target_key(self.vins[k], self.serials[k]) for k in ("A", "C", "D", "F")})

    # ---- 11. 部分恢复 ------------------------------------------------------

    def test_partial_restore_does_not_release_other_targets(self) -> None:
        key_c = target_key(self.vins["C"], self.serials["C"])
        key_d = target_key(self.vins["D"], self.serials["D"])
        self.svc.confirm_matching(
            case_id=self.case_id, confirmed_by="tech-wang",
            at=dt("2026-09-13T11:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="hold", targets=[key_c],
            decided_by="compliance-li", at=dt("2026-09-13T12:00:00+08:00"),
        )
        self.svc.decide_disposition(
            case_id=self.case_id, kind="reinspect", targets=[key_d],
            decided_by="compliance-li", at=dt("2026-09-13T12:05:00+08:00"),
        )
        self.svc.receive_inspection_report(
            report_no="R-NEW-D", vin=self.vins["D"], serial_no=self.serials["D"],
            lab_id="LAB-1", result="pass", content_hash="hash-d-pass",
            received_at=dt("2026-09-16T08:00:00+08:00"),
        )
        self.svc.sign_reinspection(
            case_id=self.case_id, target=key_d, serial_no=self.serials["D"],
            report_no="R-NEW-D", result="pass", signed_by="insp-zhao",
            at=dt("2026-09-16T09:00:00+08:00"),
        )
        # 只恢复 D。
        self.svc.restore(
            case_id=self.case_id, targets=[key_d], restored_by="reviewer-sun",
            report_no="R-NEW-D", at=dt("2026-09-16T10:00:00+08:00"),
        )
        world = self.world()
        self.assertEqual(world.active_holdings(self.vins["D"]), [])
        held_c = world.active_holdings(self.vins["C"])
        self.assertEqual(len(held_c), 1)
        self.assertEqual(held_c[0]["target"], key_c)
        # 案卷级恢复记录只包含 D，不外溢到 C。
        restores = world.cases[self.case_id].restores
        self.assertEqual([set(r["targets"]) for r in restores], [{key_d}])

    # ---- 12. 只追加与 event_id 幂等 ---------------------------------------

    def test_events_are_append_only_and_event_id_unique(self) -> None:
        events = self.store.all_events()
        suspension = next(e for e in events if e.event_type == CERTIFICATION_SUSPENDED)
        original_occurred = suspension.occurred_at
        with self.assertRaises(DuplicateEventError):
            self.store.append(suspension, expected_version=self.store.version(suspension.aggregate_id))
        # 原事件未被触碰。
        again = self.store.get_event(suspension.event_id)
        self.assertIsNotNone(again)
        self.assertEqual(again.occurred_at, original_occurred)


if __name__ == "__main__":
    unittest.main()
