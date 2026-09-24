"""场景六：检测机构离线回传。

- 相同编号 + 相同内容只接受一次（幂等键去重）。
- 相同编号、内容变化：新结果形成独立争议，先前证据保留不被覆盖。
"""

import unittest

from src.freeze import DomainError
from src.projection import Projection
from src.store import DuplicateEvent
from tests.world import build_world


class OfflineReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_world()

    def test_same_number_same_content_accepted_once_even_from_offline_retry(self) -> None:
        kwargs = dict(
            report_no="RPT-X1", agency="lab-A", content_hash="hash-v1",
            result="PASS", received_at="2026-09-16T10:00:00+08:00",
            vehicle_id="V1", serial="S1")
        first = self.svc.receive_inspection_report(**kwargs)
        self.assertEqual(first, "ACCEPTED")
        # 离线补发：同编号同内容，无论回传几次都只接受一次
        with self.assertRaises(DuplicateEvent):
            self.svc.receive_inspection_report(**kwargs)
        with self.assertRaises(DuplicateEvent):
            self.svc.receive_inspection_report(**kwargs)
        p = self.svc._projection()
        self.assertEqual(len(p.reports["RPT-X1"]), 1)

    def test_changed_content_opens_independent_dispute_without_overwriting(self) -> None:
        self.svc.receive_inspection_report(
            "RPT-X2", "lab-A", "hash-v1", "PASS", "2026-09-16T10:00:00+08:00",
            vehicle_id="V1", serial="S1")
        outcome = self.svc.receive_inspection_report(
            "RPT-X2", "lab-A", "hash-v2", "FAIL", "2026-09-17T10:00:00+08:00",
            vehicle_id="V1", serial="S1")
        self.assertEqual(outcome, "DISPUTE_OPENED")
        p = self.svc._projection()
        # 两个版本都在，先前证据未被覆盖
        hashes = [v["content_hash"] for v in p.reports["RPT-X2"]]
        self.assertEqual(hashes, ["hash-v1", "hash-v2"])
        dispute = p.disputes[-1]
        self.assertEqual(dispute["prior_content_hash"], "hash-v1")
        self.assertEqual(dispute["new_content_hash"], "hash-v2")
        self.assertEqual(dispute["status"], "OPEN")

    def test_open_dispute_blocks_restoration_until_resolved(self) -> None:
        from tests.world import confirm_and_decide
        confirm_and_decide(self.svc)
        self.svc.replace_part("CASE-1", "V1", "S1", "S6", "mechanic-m1",
                              "2026-09-15T10:00:00+08:00")
        self.svc.receive_inspection_report(
            "RPT-D", "lab-A", "hash-a", "PASS", "2026-09-16T10:00:00+08:00",
            vehicle_id="V1", serial="S6")
        self.svc.receive_inspection_report(
            "RPT-D", "lab-A", "hash-b", "FAIL", "2026-09-16T12:00:00+08:00",
            vehicle_id="V1", serial="S6")
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V1", "restorer-r1", "RPT-D",
                             "2026-09-16T15:00:00+08:00")
        # 裁决采用先前合格内容后，可以恢复；后来的 FAIL 没有覆盖先前证据
        dispute_id = self.svc._projection().disputes[-1]["dispute_id"]
        self.svc.resolve_dispute(dispute_id, "hash-a", "arbiter-1",
                                 "2026-09-16T18:00:00+08:00")
        self.svc.restore("CASE-1", "V1", "restorer-r1", "RPT-D",
                         "2026-09-16T19:00:00+08:00")
        self.assertTrue(self.svc._projection().pass_usable("P1"))

    def test_dispute_resolution_must_choose_between_existing_evidences(self) -> None:
        self.svc.receive_inspection_report(
            "RPT-X3", "lab-A", "hash-a", "PASS", "2026-09-16T10:00:00+08:00")
        self.svc.receive_inspection_report(
            "RPT-X3", "lab-A", "hash-b", "FAIL", "2026-09-16T12:00:00+08:00")
        dispute_id = self.svc._projection().disputes[-1]["dispute_id"]
        with self.assertRaises(DomainError):
            self.svc.resolve_dispute(dispute_id, "hash-never-existed", "arbiter-1")

    def test_replay_is_deterministic(self) -> None:
        self.svc.receive_inspection_report(
            "RPT-X4", "lab-A", "hash-a", "PASS", "2026-09-16T10:00:00+08:00",
            vehicle_id="V1")
        self.svc.receive_inspection_report(
            "RPT-X4", "lab-A", "hash-b", "FAIL", "2026-09-17T10:00:00+08:00",
            vehicle_id="V1")
        p1 = Projection(self.svc.store.all_events())
        p2 = Projection(self.svc.store.all_events())
        self.assertEqual(len(p1.reports["RPT-X4"]), len(p2.reports["RPT-X4"]))
        self.assertEqual(len(p1.disputes), len(p2.disputes))


if __name__ == "__main__":
    unittest.main()
