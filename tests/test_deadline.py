"""场景七：整改期限与升级通知按原截止点延续，进程恢复不重新计时。"""

import unittest

from src.freeze import DomainError
from tests.world import DUE, build_world, confirm_and_decide


class DeadlineContinuityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_world()
        confirm_and_decide(self.svc)

    def test_remediation_deadline_is_anchored_to_suspension_due_date(self) -> None:
        decision = next(
            e for e in self.svc.store.all_events()
            if e["event_type"] == "DISPOSITION_DECIDED")
        self.assertEqual(decision["payload"]["remediation_due_at"], DUE)
        scheduled = self.svc._projection().escalations[-1]
        self.assertEqual(scheduled["due_at"], DUE)
        self.assertEqual(scheduled["anchor_due_at"], DUE)

    def test_pause_resume_does_not_reset_deadline(self) -> None:
        self.svc.pause_process("CASE-1", "2026-09-12T08:00:00+08:00")
        # 暂停期间升级通知暂缓投递
        with self.assertRaises(DomainError):
            self.svc.deliver_escalation("CASE-1", 1, "2026-09-20T09:00:00+08:00")
        self.svc.resume_process("CASE-1", "2026-09-22T08:00:00+08:00")
        p = self.svc._projection()
        self.assertEqual(p.cases["CASE-1"].get("original_due_at"), DUE)
        # 恢复后按原排程补投，due_at 仍是原截止点，不因暂停时长顺延
        self.svc.deliver_escalation("CASE-1", 1, "2026-09-22T09:00:00+08:00")
        delivered = [e for e in self.svc._projection().escalations if e["delivered"]][-1]
        self.assertEqual(delivered["due_at"], DUE)

    def test_per_vehicle_due_dates_unchanged_after_long_pause(self) -> None:
        self.svc.pause_process("CASE-1", "2026-09-12T08:00:00+08:00")
        self.svc.resume_process("CASE-1", "2026-09-24T08:00:00+08:00")
        dispositions = self.svc._projection().cases["CASE-1"]["dispositions"]
        for vehicle_id, decision in dispositions.items():
            self.assertEqual(decision["due_at"], DUE)

    def test_duplicate_pause_or_resume_is_rejected(self) -> None:
        self.svc.pause_process("CASE-1", "2026-09-12T08:00:00+08:00")
        with self.assertRaises(DomainError):
            self.svc.pause_process("CASE-1", "2026-09-12T09:00:00+08:00")
        self.svc.resume_process("CASE-1", "2026-09-22T08:00:00+08:00")
        with self.assertRaises(DomainError):
            self.svc.resume_process("CASE-1", "2026-09-22T09:00:00+08:00")

    def test_escalation_cannot_be_delivered_twice(self) -> None:
        self.svc.deliver_escalation("CASE-1", 1, "2026-09-24T09:00:00+08:00")
        with self.assertRaises(DomainError):
            self.svc.deliver_escalation("CASE-1", 1, "2026-09-24T10:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
