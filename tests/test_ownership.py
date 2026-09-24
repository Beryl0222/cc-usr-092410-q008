"""场景五：所有权变化——当前通知送新车主，原施工方与历任车主责任时间线保留。"""

import unittest

from tests.world import build_world, confirm_and_decide


class OwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_world()
        confirm_and_decide(self.svc)

    def test_ownership_timeline_is_appended_and_complete(self) -> None:
        # 处置后 V1 再过户一次：原车主、新车主责任期间都在时间线上
        self.svc.transfer_ownership("V1", "owner-1", "owner-1b",
                                    "2026-09-12T10:00:00+08:00")
        p = self.svc._projection()
        owners = [period["owner"] for period in p.vehicles["V1"]["ownership"]]
        self.assertEqual(owners, ["owner-1", "owner-1b"])
        self.assertEqual(p.owner_at("V1", "2026-09-09T10:00:00+08:00"), "owner-1")
        self.assertEqual(p.owner_at("V1", "2026-09-13T10:00:00+08:00"), "owner-1b")
        # 原施工方责任记录未被过户改写
        self.assertTrue(any(
            w["serial"] == "S1" and w["technician_id"] == "tech-t1" for w in p.works))

    def test_forwarded_notice_goes_to_new_owner_only_for_unrestored_vehicles(self) -> None:
        # V1 完成整改恢复后过户：不再转投；V4 未恢复，过户后必须转投新车主
        self.svc.replace_part("CASE-1", "V1", "S1", "S6", "mechanic-m1",
                              "2026-09-15T10:00:00+08:00")
        self.svc.receive_inspection_report(
            "RPT-01", "lab-A", "hash-01", "PASS", "2026-09-16T10:00:00+08:00",
            vehicle_id="V1", serial="S6")
        self.svc.restore("CASE-1", "V1", "restorer-r1", "RPT-01",
                         "2026-09-16T16:00:00+08:00")
        self.svc.transfer_ownership("V1", "owner-1", "owner-1b",
                                    "2026-09-17T10:00:00+08:00")
        self.svc.transfer_ownership("V4", "owner-4", "owner-4b",
                                    "2026-09-17T11:00:00+08:00")
        forwarded = self.svc.forward_pending_notices("CASE-1", "2026-09-17T12:00:00+08:00")
        recipients = {e["payload"]["vehicle_id"]: e["payload"]["recipient_owner"]
                      for e in forwarded}
        self.assertNotIn("V1", recipients)       # 已恢复，不再打扰新车主
        self.assertEqual(recipients.get("V4"), "owner-4b")

    def test_forward_is_idempotent_per_new_owner(self) -> None:
        self.svc.transfer_ownership("V4", "owner-4", "owner-4b",
                                    "2026-09-12T10:00:00+08:00")
        first = self.svc.forward_pending_notices("CASE-1", "2026-09-12T12:00:00+08:00")
        second = self.svc.forward_pending_notices("CASE-1", "2026-09-12T13:00:00+08:00")
        self.assertTrue(any(e["payload"]["vehicle_id"] == "V4" for e in first))
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
