"""场景十：认证层面的暂停与恢复，以及信封基础契约回归。"""

import json
import unittest
from pathlib import Path

from src.freeze import ComplianceFreezeService, DomainError
from src.store import EventStore
from src.validator import validate_event


class CertificateLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ComplianceFreezeService(EventStore())
        self.svc.register_certificate("C1", "brake-disc", ["brake-front", "brake-rear"],
                                      "2026-09-01T08:30:00+08:00")

    def test_suspension_uses_must_be_within_granted_uses(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.suspend_certificate(
                "C1", ["steering"], "2026-09-10T09:00:00+08:00", "2026-09-25T18:00:00+08:00")

    def test_double_suspension_rejected_and_reinstatement_closes_it(self) -> None:
        self.svc.suspend_certificate(
            "C1", ["brake-front"], "2026-09-10T09:00:00+08:00", "2026-09-25T18:00:00+08:00")
        with self.assertRaises(DomainError):
            self.svc.suspend_certificate(
                "C1", ["brake-front"], "2026-09-10T10:00:00+08:00", "2026-09-26T18:00:00+08:00")
        # 未暂停的用途仍然可用——按用途精确生效
        p = self.svc._projection()
        self.assertFalse(p.cert_usable_for("C1", "brake-front"))
        self.assertTrue(p.cert_usable_for("C1", "brake-rear"))
        self.svc.reinstate_certificate("C1", "2026-09-20T09:00:00+08:00")
        p2 = self.svc._projection()
        self.assertTrue(p2.cert_usable_for("C1", "brake-front"))
        with self.assertRaises(DomainError):
            self.svc.reinstate_certificate("C1", "2026-09-20T10:00:00+08:00")


class EnvelopeRegressionTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads(
            (Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_validator_flags_unknown_type_and_missing_payload(self) -> None:
        bad = {
            "event_id": "x", "event_type": "NOPE", "aggregate_type": "freeze_case",
            "aggregate_id": "a", "occurred_at": "2026-09-10T09:00:00+08:00",
            "version": 1, "summary": "x",
        }
        self.assertTrue(validate_event(bad))
        missing_payload = {
            "event_id": "x", "event_type": "PART_BOUND", "aggregate_type": "part_instance",
            "aggregate_id": "a", "occurred_at": "2026-09-10T09:00:00+08:00",
            "version": 1, "summary": "x",
        }
        self.assertTrue(any("payload" in e for e in validate_event(missing_payload)))


if __name__ == "__main__":
    unittest.main()
