"""场景八：并发复核——两个合规人员基于过期版本决策时必须冲突重检。"""

import unittest

from src.freeze import (
    DISPOSITION_QUARANTINE_PART,
    DISPOSITION_REINSPECT,
    DISPOSITION_REPLACE_PART,
    DISPOSITION_SUSPEND_PASS,
)
from src.store import ConcurrencyConflict
from tests.world import build_world

ITEMS = [
    {"vehicle_id": "V1", "action": DISPOSITION_REPLACE_PART},
    {"vehicle_id": "V3", "action": DISPOSITION_QUARANTINE_PART},
    {"vehicle_id": "V4", "action": DISPOSITION_REINSPECT},
    {"vehicle_id": "V5", "action": DISPOSITION_SUSPEND_PASS},
]


class ConcurrencyReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_world()
        p = self.svc._projection()
        self.case_hash = p.cases["CASE-1"]["scope_hash"]
        self.svc.confirm_matching_rules(
            "CASE-1", "tech-chief", ["V1", "V3", "V4", "V5"], self.case_hash,
            "2026-09-10T11:00:00+08:00")
        # 两名合规人员在同一版本上各自复核
        self.version = self.svc.store.version_of("case-CASE-1")

    def test_second_writer_based_on_stale_version_conflicts(self) -> None:
        self.svc.decide_disposition(
            "CASE-1", ITEMS, "compliance-c1",
            expected_version=self.version, at="2026-09-10T14:00:00+08:00")
        # 第二人仍持旧版本：并发冲突，整批不写入，必须重新读取再决策
        with self.assertRaises(ConcurrencyConflict):
            self.svc.decide_disposition(
                "CASE-1", ITEMS, "compliance-c2",
                expected_version=self.version, at="2026-09-10T14:05:00+08:00")
        # 冲突写入被原子回滚：案件仍只有第一次处置，通知没有翻倍
        notices = [e for e in self.svc.store.all_events() if e["event_type"] == "NOTICE_ISSUED"]
        self.assertEqual(len(notices), 4)

    def test_stale_writer_cannot_sneak_expanded_scope_through(self) -> None:
        # 即便绕过版本检查，扩大到同车型 V2 的处置仍被领域规则拒绝
        expanded = ITEMS + [{"vehicle_id": "V2", "action": DISPOSITION_REINSPECT}]
        from src.freeze import DomainError
        with self.assertRaises(DomainError):
            self.svc.decide_disposition(
                "CASE-1", expanded, "compliance-c2",
                expected_version=self.version, at="2026-09-10T14:05:00+08:00")
        self.assertEqual(self.svc.store.version_of("case-CASE-1"), self.version)


if __name__ == "__main__":
    unittest.main()
