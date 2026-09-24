"""场景三：换件后由另一人依据新检测恢复；支持部分恢复且范围不扩大。"""

import unittest

from src.freeze import DomainError
from tests.world import build_world, confirm_and_decide


def _new_pass_report(svc, report_no, vehicle_id, serial, at, agency="lab-A", content_hash=None):
    return svc.receive_inspection_report(
        report_no, agency, content_hash or f"hash-{report_no}", "PASS", at,
        vehicle_id=vehicle_id, serial=serial)


class ReplacementRestorationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_world()
        confirm_and_decide(self.svc)

    def _replace_v1(self, at="2026-09-15T10:00:00+08:00", actor="mechanic-m1"):
        return self.svc.replace_part("CASE-1", "V1", "S1", "S6", actor, at)

    def test_cannot_replace_with_part_under_same_suspended_certificate(self) -> None:
        # S3/S4/S5 都在 CERT-BRAKE-01 下
        with self.assertRaises(DomainError):
            self.svc.replace_part("CASE-1", "V1", "S1", "S3", "mechanic-m1",
                                  "2026-09-15T10:00:00+08:00")

    def test_replace_swaps_install_state_and_records_work_history(self) -> None:
        self._replace_v1()
        p = self.svc._projection()
        self.assertEqual(p.parts["S1"]["installed_in"], None)
        self.assertEqual(p.parts["S1"]["removed_from"], "V1")
        self.assertEqual(p.parts["S6"]["installed_in"], "V1")
        # 原施工记录仍保留
        self.assertTrue(any(w["serial"] == "S1" and w["technician_id"] == "tech-t1" for w in p.works))

    def test_restoration_requires_different_person_from_replacer(self) -> None:
        self._replace_v1()
        _new_pass_report(self.svc, "RPT-01", "V1", "S6", "2026-09-16T10:00:00+08:00")
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V1", "mechanic-m1", "RPT-01",
                             "2026-09-16T15:00:00+08:00")

    def test_restoration_requires_new_pass_report_after_suspension(self) -> None:
        self._replace_v1()
        # 暂停前的旧合格结论不能作为依据
        self.svc.receive_inspection_report(
            "RPT-OLD", "lab-A", "hash-old", "PASS", "2026-09-09T10:00:00+08:00",
            vehicle_id="V1", serial="S1")
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V1", "restorer-r1", "RPT-OLD",
                             "2026-09-16T15:00:00+08:00")
        # FAIL 也不行
        self.svc.receive_inspection_report(
            "RPT-FAIL", "lab-A", "hash-fail", "FAIL", "2026-09-16T11:00:00+08:00",
            vehicle_id="V1", serial="S6")
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V1", "restorer-r1", "RPT-FAIL",
                             "2026-09-16T15:00:00+08:00")

    def test_inspection_agency_cannot_self_restore(self) -> None:
        self._replace_v1()
        _new_pass_report(self.svc, "RPT-01", "V1", "S6", "2026-09-16T10:00:00+08:00",
                         agency="lab-A")
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V1", "lab-A", "RPT-01",
                             "2026-09-16T15:00:00+08:00")

    def test_partial_restoration_lifts_only_one_vehicle_and_scope_does_not_expand(self) -> None:
        self._replace_v1()
        _new_pass_report(self.svc, "RPT-01", "V1", "S6", "2026-09-16T10:00:00+08:00")
        self.svc.restore("CASE-1", "V1", "restorer-r1", "RPT-01",
                         "2026-09-16T15:00:00+08:00")
        p = self.svc._projection()
        # 只有 V1 恢复，P1 解除限制
        self.assertTrue(p.pass_usable("P1"))
        self.assertFalse(p.pass_usable("P5"))
        self.assertEqual(p.cases["CASE-1"]["restored"], {"V1"})
        # 范围集合本身不被恢复操作改变
        self.assertEqual(set(p.cases["CASE-1"]["scope"]), {"V1", "V3", "V4", "V5"})
        # 不能重复恢复同一车辆
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V1", "restorer-r2", "RPT-01",
                             "2026-09-17T10:00:00+08:00")

    def test_reinspect_vehicle_restored_by_third_party_with_new_report(self) -> None:
        # V4 走复检线：原施工 tech-t4、检测 lab-B，恢复必须由第三方完成
        self.svc.receive_inspection_report(
            "RPT-V4", "lab-B", "hash-v4", "PASS", "2026-09-14T10:00:00+08:00",
            vehicle_id="V4", serial="S4")
        # 原施工人本人不能恢复
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V4", "tech-t4", "RPT-V4",
                             "2026-09-14T15:00:00+08:00")
        # 检测机构本人也不能恢复
        with self.assertRaises(DomainError):
            self.svc.restore("CASE-1", "V4", "lab-B", "RPT-V4",
                             "2026-09-14T15:30:00+08:00")
        # 第三方恢复成功
        self.svc.restore("CASE-1", "V4", "restorer-r2", "RPT-V4",
                         "2026-09-14T16:00:00+08:00")
        p = self.svc._projection()
        self.assertEqual(p.cases["CASE-1"]["restored"], {"V4"})
        # 复检不改变部件安装关系
        self.assertEqual(p.parts["S4"]["installed_in"], "V4")

    def test_quarantine_vehicle_is_remediated_without_restoration_event(self) -> None:
        # V3 未施工，封存部件即完成处置；无通行证可解除
        p = self.svc._projection()
        self.assertTrue(p.cases["CASE-1"]["remediation"]["S3"]["quarantined"])


if __name__ == "__main__":
    unittest.main()
