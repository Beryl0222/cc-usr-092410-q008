"""场景九：从通行证、车辆或部件任一入口查询，得到同一条一致链路。"""

import unittest

from src.trace import trace
from tests.world import build_world, confirm_and_decide


def _build_resolved_world():
    svc, _ = build_world()
    confirm_and_decide(svc)
    # V1：换件 → 新检测 → 另一人恢复
    svc.replace_part("CASE-1", "V1", "S1", "S6", "mechanic-m1",
                     "2026-09-15T10:00:00+08:00")
    svc.receive_inspection_report(
        "RPT-01", "lab-A", "hash-01", "PASS", "2026-09-16T10:00:00+08:00",
        vehicle_id="V1", serial="S6")
    svc.restore("CASE-1", "V1", "restorer-r1", "RPT-01",
                "2026-09-16T16:00:00+08:00")
    # V4：复检 → 另一人恢复
    svc.receive_inspection_report(
        "RPT-V4", "lab-B", "hash-v4", "PASS", "2026-09-14T10:00:00+08:00",
        vehicle_id="V4", serial="S4")
    svc.restore("CASE-1", "V4", "restorer-r2", "RPT-V4",
                "2026-09-14T16:00:00+08:00")
    # V5：申诉进行中（行政结论暂缓，安全限制保留）
    svc.file_appeal("APL-1", "CASE-1", ["V5"], "owner-5b",
                    "2026-09-11T09:00:00+08:00")
    return svc


class UnifiedTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _build_resolved_world()

    def test_three_entry_points_return_identical_chain(self) -> None:
        via_pass = trace(self.svc.store, "P1")["chain_event_types"]
        via_vehicle = trace(self.svc.store, "V5")["chain_event_types"]
        via_old_part = trace(self.svc.store, "S5")["chain_event_types"]
        via_new_part = trace(self.svc.store, "S6")["chain_event_types"]
        # 通行证 / 车辆 / 旧件 / 新件四个入口看到的是同一条案件链路
        self.assertEqual(via_pass, via_vehicle)
        self.assertEqual(via_pass, via_old_part)
        self.assertEqual(via_pass, via_new_part)

    def test_chain_covers_full_lifecycle(self) -> None:
        result = trace(self.svc.store, "V1")
        types_ = set(result["chain_event_types"])
        for expected in (
            "CERTIFICATE_SUSPENDED",
            "FREEZE_SCOPE_DELINEATED",
            "MATCHING_RULES_CONFIRMED",
            "DISPOSITION_DECIDED",
            "PASS_RESTRICTION_IMPOSED",
            "PART_REPLACED",
            "INSPECTION_RESULT_RECEIVED",
            "PASS_RESTRICTION_LIFTED",
            "RESTORATION_COMPLETED",
            "APPEAL_FILED",
            "NOTICE_ISSUED",
            "ESCALATION_SCHEDULED",
            "OWNERSHIP_TRANSFERRED",
        ):
            self.assertIn(expected, types_, f"链路缺少 {expected}")

    def test_chain_respects_event_order(self) -> None:
        types_ = trace(self.svc.store, "V1")["chain_event_types"]
        self.assertLess(types_.index("CERTIFICATE_SUSPENDED"),
                        types_.index("FREEZE_SCOPE_DELINEATED"))
        self.assertLess(types_.index("MATCHING_RULES_CONFIRMED"),
                        types_.index("DISPOSITION_DECIDED"))
        self.assertLess(types_.index("PART_REPLACED"),
                        types_.index("PASS_RESTRICTION_LIFTED"))

    def test_unaffected_vehicle_chain_is_isolated(self) -> None:
        result = trace(self.svc.store, "V2")
        self.assertEqual(result["cases"], [])
        self.assertNotIn("FREEZE_SCOPE_DELINEATED", result["chain_event_types"])
        self.assertNotIn("CERTIFICATE_SUSPENDED", result["chain_event_types"])
        # 从干净部件 S2 进入得到完全相同的隔离链路
        self.assertEqual(trace(self.svc.store, "S2")["chain_event_types"],
                         result["chain_event_types"])

    def test_partial_restoration_state_is_consistent_across_entries(self) -> None:
        # V1/V4 已恢复，V3 封存，V5 仍受限且申诉中——从任何入口读到的投影一致
        for entry in ("P1", "P5", "V1", "V5", "S6", "S5"):
            projection_view = trace(self.svc.store, entry)
            self.assertEqual(set(projection_view["vehicles"]),
                             {"V1", "V3", "V4", "V5"})
        p = self.svc._projection()
        self.assertTrue(p.pass_usable("P1"))
        self.assertFalse(p.pass_usable("P5"))
        self.assertEqual(p.open_appeals("CASE-1", "V5")[0]["appeal_id"], "APL-1")


if __name__ == "__main__":
    unittest.main()
