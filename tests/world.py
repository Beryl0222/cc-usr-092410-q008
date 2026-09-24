"""测试夹具：构造覆盖四种车辆状态与转卖情形的标准世界。

车辆布局（车型均为 M1）：
- V1：C1 批次部件 S1，已施工、已取得通行证 P1        -> 换件恢复线
- V2：同车型但使用 C2 批次部件 S2（认证未暂停）      -> 不得入范围
- V3：C1 批次部件 S3，仅配发未施工                  -> 封存
- V4：C1 批次部件 S4，已施工待检测（无通行证）       -> 复检
- V5：C1 批次部件 S5，已发证，暂停前已过户给新车主   -> 暂停通行证 + 申诉
"""

from src.freeze import (
    ComplianceFreezeService,
    DISPOSITION_QUARANTINE_PART,
    DISPOSITION_REINSPECT,
    DISPOSITION_REPLACE_PART,
    DISPOSITION_SUSPEND_PASS,
)
from src.store import EventStore

CERT_SUSPENDED = "CERT-BRAKE-01"
CERT_CLEAN = "CERT-BRAKE-02"
USE = "brake-front"
DUE = "2026-09-25T18:00:00+08:00"
SUSPEND_AT = "2026-09-10T09:00:00+08:00"


def build_world() -> ComplianceFreezeService:
    svc = ComplianceFreezeService(EventStore())
    s = svc

    s.register_model("M1", [USE], "2026-09-01T08:00:00+08:00")
    s.register_certificate(CERT_SUSPENDED, "brake-caliper", [USE], "2026-09-01T08:30:00+08:00")
    s.register_certificate(CERT_CLEAN, "brake-caliper", [USE], "2026-09-01T08:30:00+08:00")

    s.register_vehicle("V1", "M1", "owner-1", "2026-09-01T09:00:00+08:00")
    s.register_vehicle("V2", "M1", "owner-2", "2026-09-01T09:00:00+08:00")
    s.register_vehicle("V3", "M1", "owner-3", "2026-09-01T09:00:00+08:00")
    s.register_vehicle("V4", "M1", "owner-4", "2026-09-01T09:00:00+08:00")
    s.register_vehicle("V5", "M1", "owner-5a", "2026-09-01T09:00:00+08:00")

    s.register_part("S1", CERT_SUSPENDED, "B1", "2026-09-01T10:00:00+08:00")
    s.register_part("S2", CERT_CLEAN, "B2", "2026-09-01T10:00:00+08:00")
    s.register_part("S3", CERT_SUSPENDED, "B1", "2026-09-01T10:00:00+08:00")
    s.register_part("S4", CERT_SUSPENDED, "B1", "2026-09-01T10:00:00+08:00")
    s.register_part("S5", CERT_SUSPENDED, "B1", "2026-09-01T10:00:00+08:00")
    s.register_part("S6", CERT_CLEAN, "B9", "2026-09-01T10:00:00+08:00")

    s.bind_part("S1", "V1", USE, "2026-09-02T08:00:00+08:00")
    s.attest_work("W1", "V1", "S1", "tech-t1", "2026-09-02T11:00:00+08:00")
    s.bind_part("S2", "V2", USE, "2026-09-02T08:00:00+08:00")
    s.attest_work("W2", "V2", "S2", "tech-t2", "2026-09-02T11:00:00+08:00")
    s.bind_part("S3", "V3", USE, "2026-09-03T08:00:00+08:00")  # 未施工
    s.bind_part("S4", "V4", USE, "2026-09-03T08:00:00+08:00")
    s.attest_work("W4", "V4", "S4", "tech-t4", "2026-09-03T11:00:00+08:00")
    s.bind_part("S5", "V5", USE, "2026-09-04T08:00:00+08:00")
    s.attest_work("W5", "V5", "S5", "tech-t5", "2026-09-04T11:00:00+08:00")

    s.activate_pass("P1", "V1", "2026-09-05T10:00:00+08:00")
    s.activate_pass("P5", "V5", "2026-09-05T10:00:00+08:00")

    # V5 在认证暂停前已经转卖
    s.transfer_ownership("V5", "owner-5a", "owner-5b", "2026-09-08T15:00:00+08:00")

    s.suspend_certificate(CERT_SUSPENDED, [USE], SUSPEND_AT, DUE)
    scope_event = s.delineate_scope("CASE-1", CERT_SUSPENDED, "2026-09-10T10:00:00+08:00")
    return svc, scope_event


def confirm_and_decide(svc: ComplianceFreezeService) -> None:
    """技术人员确认 + 合规人员逐车处置的标准流程。"""
    p = svc._projection()
    case = p.cases["CASE-1"]
    svc.confirm_matching_rules(
        "CASE-1", "tech-chief", sorted(case["scope"]), case["scope_hash"],
        "2026-09-10T11:00:00+08:00")
    items = [
        {"vehicle_id": "V1", "action": DISPOSITION_REPLACE_PART},
        {"vehicle_id": "V3", "action": DISPOSITION_QUARANTINE_PART},
        {"vehicle_id": "V4", "action": DISPOSITION_REINSPECT},
        {"vehicle_id": "V5", "action": DISPOSITION_SUSPEND_PASS},
    ]
    version = svc.store.version_of("case-CASE-1")
    svc.decide_disposition("CASE-1", items, "compliance-c1",
                           expected_version=version, at="2026-09-10T14:00:00+08:00")
