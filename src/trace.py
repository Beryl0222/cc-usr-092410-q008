"""统一链路查询：从通行证、车辆或部件任一入口得到同一条事件链路。

三个入口先归一化到共同的关联集合（案件、车辆、部件、通行证），再从同一条
仅追加事件流中按相同规则过滤；因此无论从哪个入口进入，认证变更、施工、检测、
申诉、换件与恢复记录必然一致。
"""

from src.projection import Projection


def _vehicles_touching_serial(projection: Projection, serial: str) -> set[str]:
    part = projection.parts.get(serial)
    if part is None:
        return set()
    vehicles = {b["vehicle_id"] for b in part["bindings"]}
    if part.get("installed_in"):
        vehicles.add(part["installed_in"])
    if part.get("removed_from"):
        vehicles.add(part["removed_from"])
    return vehicles


def trace(store, entry: str) -> dict:
    """返回从通行证号 / 车辆号 / 部件序列号入口看到的完整一致链路。"""
    projection = Projection(store.all_events())

    # 1) 入口归一化到车辆集合
    if entry in projection.passes:
        seed_vehicles = {projection.passes[entry]["vehicle_id"]}
    elif entry in projection.parts:
        seed_vehicles = _vehicles_touching_serial(projection, entry)
    else:
        seed_vehicles = {entry} if entry in projection.vehicles else set()

    # 2) 案件闭包：涉案车辆与涉案部件双向传播
    cases: set[str] = set()
    for _ in range(10):
        before = (len(seed_vehicles), len(cases))
        for case_id, case in projection.cases.items():
            scope_serials = {s for v in case["scope"].values() for s in v["serials"]}
            if set(case["scope"]) & seed_vehicles:
                cases.add(case_id)
            if case_id in cases:
                seed_vehicles.update(case["scope"].keys())
                for s in scope_serials:
                    seed_vehicles |= _vehicles_touching_serial(projection, s)
        if (len(seed_vehicles), len(cases)) == before:
            break

    # 3) 由闭包推出部件、认证与通行证集合（含新旧换件件）
    serials: set[str] = set()
    certs: set[str] = set()
    passes: set[str] = set()
    reports: set[str] = set()
    for s, part in projection.parts.items():
        if _vehicles_touching_serial(projection, s) & seed_vehicles:
            serials.add(s)
            certs.add(part["cert_no"])
    for case_id in cases:
        certs.add(projection.cases[case_id]["cert_no"])
        for v in projection.cases[case_id]["scope"].values():
            serials.update(v["serials"])
    for pid, pass_ in projection.passes.items():
        if pass_["vehicle_id"] in seed_vehicles:
            passes.add(pid)
    for report_no, versions in projection.reports.items():
        if any(v.get("vehicle_id") in seed_vehicles or v.get("serial") in serials for v in versions):
            reports.add(report_no)
    disputes = {d["dispute_id"] for d in projection.disputes if d["report_no"] in reports}

    # 4) 从同一条事件流按相同规则过滤
    chain: list[dict] = []
    for event in store.all_events():
        et = event["event_type"]
        p = event.get("payload", {})
        keep = False
        if et in ("CERTIFICATE_REGISTERED", "CERTIFICATE_SUSPENDED", "CERTIFICATE_REINSTATED"):
            keep = p.get("cert_no") in certs
        elif et in ("VEHICLE_REGISTERED", "OWNERSHIP_TRANSFERRED"):
            keep = p.get("vehicle_id") in seed_vehicles
        elif et in ("PART_SERIAL_REGISTERED", "PART_BOUND"):
            keep = p.get("serial") in serials
        elif et == "WORK_ATTESTED":
            keep = p.get("vehicle_id") in seed_vehicles and p.get("serial") in serials
        elif et in ("INSPECTION_RESULT_RECEIVED", "INSPECTION_SIGNED"):
            keep = p.get("report_no") in reports
        elif et == "PASS_ACTIVATED":
            keep = p.get("pass_id") in passes
        elif et in ("PASS_RESTRICTION_IMPOSED", "PASS_RESTRICTION_LIFTED"):
            keep = p.get("pass_id") in passes or p.get("case_id") in cases
        elif et == "PART_REPLACED":
            keep = p.get("case_id") in cases and p.get("vehicle_id") in seed_vehicles
        elif et in ("FREEZE_SCOPE_DELINEATED", "MATCHING_RULES_CONFIRMED",
                    "DISPOSITION_DECIDED", "PART_QUARANTINED", "PROCESS_PAUSED",
                    "PROCESS_RESUMED", "RESTORATION_COMPLETED",
                    "ADMINISTRATIVE_FINALIZATION"):
            keep = p.get("case_id") in cases
        elif et == "APPEAL_FILED":
            keep = p.get("case_id") in cases and bool(set(p.get("vehicle_ids", [])) & seed_vehicles)
        elif et == "APPEAL_DECIDED":
            keep = p.get("case_id") in cases and p.get("appeal_id") in {
                a_id for a_id, a in projection.appeals.items()
                if a["case_id"] in cases and set(a["vehicle_ids"]) & seed_vehicles
            }
        elif et == "NOTICE_ISSUED":
            keep = p.get("case_id") in cases and p.get("vehicle_id") in seed_vehicles
        elif et in ("ESCALATION_SCHEDULED", "ESCALATION_DELIVERED"):
            keep = p.get("case_id") in cases
        elif et in ("DISPUTE_OPENED", "DISPUTE_RESOLVED"):
            keep = p.get("dispute_id") in disputes
        if keep:
            chain.append(event)

    return {
        "entry": entry,
        "vehicles": sorted(seed_vehicles),
        "serials": sorted(serials),
        "passes": sorted(passes),
        "certificates": sorted(certs),
        "cases": sorted(cases),
        "chain": chain,
        "chain_event_types": [e["event_type"] for e in chain],
    }
