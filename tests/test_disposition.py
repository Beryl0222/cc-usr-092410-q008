"""场景二：技师确认匹配规则，合规人员再决定暂停/复检/换件。"""

import unittest

from src.freeze import (
    DomainError,
    DISPOSITION_QUARANTINE_PART,
    DISPOSITION_REINSPECT,
    DISPOSITION_REPLACE_PART,
    DISPOSITION_SUSPEND_PASS,
)
from tests.world import build_world, confirm_and_decide


class DispositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_world()
        self.case_hash = self.svc._projection().cases["CASE-1"]["scope_hash"]

    def _confirm(self, vehicle_ids=None, technician="tech-chief", scope_hash=None):
        ids = vehicle_ids or ["V1", "V3", "V4", "V5"]
        self.svc.confirm_matching_rules(
            "CASE-1", technician, ids, scope_hash or self.case_hash,
            "2026-09-10T11:00:00+08:00")

    def test_disposition_requires_technician_confirmation_first(self) -> None:
        items = [{"vehicle_id": "V1", "action": DISPOSITION_REPLACE_PART}]
        with self.assertRaises(DomainError):
            self.svc.decide_disposition("CASE-1", items, "compliance-c1",
                                        at="2026-09-10T14:00:00+08:00")

    def test_confirmation_rejects_tampered_hash(self) -> None:
        with self.assertRaises(DomainError):
            self._confirm(scope_hash="deadbeef")

    def test_confirmation_rejects_scope_expansion_or_shrink(self) -> None:
        with self.assertRaises(DomainError):
            self._confirm(vehicle_ids=["V1", "V2", "V3", "V4", "V5"])  # 塞入同车型 V2
        with self.assertRaises(DomainError):
            self._confirm(vehicle_ids=["V1", "V3", "V4"])  # 漏掉 V5

    def test_compliance_officer_cannot_be_confirming_technician(self) -> None:
        self._confirm(technician="same-person")
        items = [
            {"vehicle_id": "V1", "action": DISPOSITION_REPLACE_PART},
            {"vehicle_id": "V3", "action": DISPOSITION_QUARANTINE_PART},
            {"vehicle_id": "V4", "action": DISPOSITION_REINSPECT},
            {"vehicle_id": "V5", "action": DISPOSITION_SUSPEND_PASS},
        ]
        with self.assertRaises(DomainError):
            self.svc.decide_disposition("CASE-1", items, "same-person",
                                        at="2026-09-10T14:00:00+08:00")

    def test_disposition_must_cover_exactly_the_scope(self) -> None:
        self._confirm()
        too_few = [
            {"vehicle_id": "V1", "action": DISPOSITION_REPLACE_PART},
            {"vehicle_id": "V3", "action": DISPOSITION_QUARANTINE_PART},
            {"vehicle_id": "V5", "action": DISPOSITION_SUSPEND_PASS},
        ]
        with self.assertRaises(DomainError):
            self.svc.decide_disposition("CASE-1", too_few, "compliance-c1",
                                        at="2026-09-10T14:00:00+08:00")

    def test_actions_must_match_vehicle_states(self) -> None:
        self._confirm()
        bad = [
            {"vehicle_id": "V1", "action": DISPOSITION_QUARANTINE_PART},  # 已施工不能只封存
            {"vehicle_id": "V3", "action": DISPOSITION_REINSPECT},        # 未施工不能复检
            {"vehicle_id": "V4", "action": DISPOSITION_SUSPEND_PASS},     # 无通行证可暂停
            {"vehicle_id": "V5", "action": DISPOSITION_SUSPEND_PASS},
        ]
        with self.assertRaises(DomainError):
            self.svc.decide_disposition("CASE-1", bad, "compliance-c1",
                                        at="2026-09-10T14:00:00+08:00")

    def test_valid_dispositions_impose_safety_restrictions_and_notice_current_owner(self) -> None:
        confirm_and_decide(self.svc)
        p = self.svc._projection()
        # P1（换件）与 P5（暂停）都被施加安全限制；V4 无通行证不产生限制
        self.assertFalse(p.pass_usable("P1"))
        self.assertFalse(p.pass_usable("P5"))
        recipients = {(n["vehicle_id"], n["recipient_owner"]) for n in p.notices}
        # V5 已转卖，通知必须送达新车主 owner-5b，而非原车主
        self.assertIn(("V5", "owner-5b"), recipients)
        self.assertNotIn(("V5", "owner-5a"), recipients)
        # 整改截止锚定证书暂停时的原截止点
        case = p.cases["CASE-1"]
        self.assertEqual(case["dispositions"]["V1"]["due_at"], "2026-09-25T18:00:00+08:00")

    def test_decisions_are_append_only_and_separation_enforced_by_events(self) -> None:
        confirm_and_decide(self.svc)
        types = [e["event_type"] for e in self.svc.store.all_events()]
        self.assertEqual(types.count("DISPOSITION_DECIDED"), 1)
        # 不能对同一案件再做第二次处置（处置集合要求恰好等于范围，已处置不影响事件层，
        # 但技师确认不可重复，因此第二次必然被拒）
        with self.assertRaises(DomainError):
            self.svc.decide_disposition(
                "CASE-1", [], "compliance-c2", at="2026-09-10T15:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
