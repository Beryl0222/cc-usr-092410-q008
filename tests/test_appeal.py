"""场景四：申诉只暂缓行政结论，不能解除仍有效的安全限制。"""

import unittest

from src.freeze import DomainError
from tests.world import build_world, confirm_and_decide


class AppealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_world()
        confirm_and_decide(self.svc)

    def test_open_appeal_stays_administrative_conclusion_but_restriction_remains(self) -> None:
        p = self.svc._projection()
        self.assertFalse(p.pass_usable("P5"))
        self.svc.file_appeal("APL-1", "CASE-1", ["V5"], "owner-5b",
                             "2026-09-11T09:00:00+08:00")
        # 行政结论被暂缓
        with self.assertRaises(DomainError):
            self.svc.finalize_administrative(
                "CASE-1", "V5", "CONFIRMED_VIOLATION", "compliance-c1",
                "2026-09-12T09:00:00+08:00")
        # 安全限制仍然有效，通行证不可用
        self.assertFalse(self.svc._projection().pass_usable("P5"))

    def test_appeal_rejected_allows_finalization_only_after_remediation(self) -> None:
        self.svc.file_appeal("APL-1", "CASE-1", ["V5"], "owner-5b",
                             "2026-09-11T09:00:00+08:00")
        self.svc.decide_appeal("APL-1", "REJECTED", "appeal-board-1",
                               "2026-09-13T09:00:00+08:00")
        # 申诉驳回但 V5 尚未整改，行政结案仍不允许
        with self.assertRaises(DomainError):
            self.svc.finalize_administrative(
                "CASE-1", "V5", "CONFIRMED_VIOLATION", "compliance-c1",
                "2026-09-13T10:00:00+08:00")
        # 完成换件 + 新检测 + 另一人恢复后，方可行政结案
        self.svc.replace_part("CASE-1", "V5", "S5", "S6", "mechanic-m5",
                              "2026-09-15T10:00:00+08:00")
        # 但 S6 已被 V1 换件场景共用会冲突？本用例中 V1 未换件，S6 空闲
        self.svc.receive_inspection_report(
            "RPT-V5", "lab-A", "hash-v5", "PASS", "2026-09-16T10:00:00+08:00",
            vehicle_id="V5", serial="S6")
        self.svc.restore("CASE-1", "V5", "restorer-r5", "RPT-V5",
                         "2026-09-16T16:00:00+08:00")
        self.svc.finalize_administrative(
            "CASE-1", "V5", "CONFIRMED_VIOLATION", "compliance-c1",
            "2026-09-17T09:00:00+08:00")
        p = self.svc._projection()
        self.assertTrue(p.pass_usable("P5"))
        self.assertEqual(p.cases["CASE-1"]["finalized"]["V5"]["conclusion"],
                         "CONFIRMED_VIOLATION")

    def test_appeal_cannot_target_vehicle_outside_scope(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.file_appeal("APL-X", "CASE-1", ["V2"], "owner-2",
                                 "2026-09-11T09:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
