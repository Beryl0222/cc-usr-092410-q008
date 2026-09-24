"""场景一：精确圈定——不按车型一刀切，按证书用途与施工记录匹配。"""

import unittest

from src.events import (
    STATE_AWAITING_INSPECTION,
    STATE_NOT_INSTALLED,
    STATE_PASS_HELD,
)
from tests.world import CERT_SUSPENDED, USE, build_world


class ScopeDelineationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.scope_event = build_world()
        self.vehicles = {v["vehicle_id"]: v for v in self.scope_event["payload"]["vehicles"]}

    def test_scope_contains_exactly_cert_affected_vehicles(self) -> None:
        # 同车型 V2 使用未暂停认证 C2，绝不能入范围
        self.assertEqual(set(self.vehicles), {"V1", "V3", "V4", "V5"})
        self.assertNotIn("V2", self.vehicles)

    def test_scope_distinguishes_lifecycle_states(self) -> None:
        self.assertEqual(self.vehicles["V1"]["state"], STATE_PASS_HELD)
        self.assertEqual(self.vehicles["V3"]["state"], STATE_NOT_INSTALLED)
        self.assertEqual(self.vehicles["V4"]["state"], STATE_AWAITING_INSPECTION)
        self.assertEqual(self.vehicles["V5"]["state"], STATE_PASS_HELD)

    def test_scope_traces_serials_not_model(self) -> None:
        self.assertEqual(self.vehicles["V1"]["serials"], ["S1"])
        self.assertEqual(self.vehicles["V4"]["serials"], ["S4"])
        self.assertEqual(self.vehicles["V1"]["cert_uses"], [USE])

    def test_resold_vehicle_is_flagged_but_still_in_scope(self) -> None:
        v5 = self.vehicles["V5"]
        self.assertTrue(v5["resold"])
        self.assertEqual(v5["current_owner"], "owner-5b")

    def test_scope_hash_is_stable_and_deterministic(self) -> None:
        # 重放同一事件流必须得到相同哈希
        from src.projection import Projection
        projected = Projection(self.svc.store.all_events())
        self.assertEqual(projected.cases["CASE-1"]["scope_hash"], self.scope_event["payload"]["scope_hash"])

    def test_other_cert_suspension_does_not_pull_in_same_model_vehicles(self) -> None:
        from tests.world import CERT_CLEAN
        # 暂停“另一个”认证时，V2 才入范围，V1/V3/V4/V5 不入
        self.svc.suspend_certificate(
            CERT_CLEAN, [USE], "2026-09-11T09:00:00+08:00", "2026-09-30T18:00:00+08:00")
        event = self.svc.delineate_scope("CASE-2", CERT_CLEAN, "2026-09-11T10:00:00+08:00")
        scoped = {v["vehicle_id"] for v in event["payload"]["vehicles"]}
        self.assertEqual(scoped, {"V2"})

    def test_cannot_delineate_without_active_suspension(self) -> None:
        from src.freeze import DomainError
        with self.assertRaises(DomainError):
            self.svc.delineate_scope("CASE-X", "CERT-NONE", "2026-09-11T10:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
