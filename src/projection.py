"""读模型：把仅追加的事件流折叠为当前状态与时间线。

投影只依赖事件顺序，不保存任何可被外部改写的状态；事件重放必须得到相同结果。
"""

from collections import defaultdict
from datetime import datetime


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Projection:
    def __init__(self, events: list[dict]) -> None:
        self.models: dict[str, dict] = {}
        self.vehicles: dict[str, dict] = {}
        self.certificates: dict[str, dict] = {}
        self.parts: dict[str, dict] = {}
        self.works: list[dict] = []
        self.reports: dict[str, list[dict]] = defaultdict(list)
        self.disputes: list[dict] = []
        self.passes: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self.appeals: dict[str, dict] = {}
        self.notices: list[dict] = []
        self.escalations: list[dict] = []
        self.events = list(events)
        for seq, event in enumerate(events):
            self._apply(seq, event)

    # ---- 折叠 ----------------------------------------------------------------

    def _apply(self, seq: int, event: dict) -> None:
        et = event["event_type"]
        p = event.get("payload", {})
        at = event["occurred_at"]
        handler = getattr(self, f"_on_{et.lower()}", None)
        if handler:
            handler(seq, event, p, at)

    def _on_model_declared(self, seq, e, p, at):
        self.models[p["model_id"]] = {"model_id": p["model_id"], "allowed_uses": p.get("allowed_uses", [])}

    def _on_vehicle_registered(self, seq, e, p, at):
        self.vehicles[p["vehicle_id"]] = {
            "vehicle_id": p["vehicle_id"],
            "model_id": p["model_id"],
            "owner": p["owner"],
            "ownership": [{"owner": p["owner"], "from": at, "to": None}],
        }

    def _on_ownership_transferred(self, seq, e, p, at):
        vehicle = self.vehicles[p["vehicle_id"]]
        vehicle["ownership"][-1]["to"] = at
        vehicle["owner"] = p["to_owner"]
        vehicle["ownership"].append({"owner": p["to_owner"], "from": at, "to": None})

    def _on_certificate_registered(self, seq, e, p, at):
        self.certificates[p["cert_no"]] = {
            "cert_no": p["cert_no"],
            "part_type": p["part_type"],
            "uses": list(p.get("uses", [])),
            "suspensions": [],
        }

    def _on_certificate_suspended(self, seq, e, p, at):
        self.certificates[p["cert_no"]]["suspensions"].append(
            {
                "suspended_uses": set(p["suspended_uses"]),
                "effective_at": p["effective_at"],
                "remediation_due_at": p["remediation_due_at"],
                "reinstated_at": None,
                "active": True,
            }
        )

    def _on_certificate_reinstated(self, seq, e, p, at):
        for suspension in reversed(self.certificates[p["cert_no"]]["suspensions"]):
            if suspension["active"]:
                suspension["active"] = False
                suspension["reinstated_at"] = p["reinstated_at"]
                break

    def _on_part_serial_registered(self, seq, e, p, at):
        self.parts[p["serial"]] = {
            "serial": p["serial"],
            "cert_no": p["cert_no"],
            "batch_no": p["batch_no"],
            "bindings": [],
            "installed_in": None,
            "removed_from": None,
        }

    def _on_part_bound(self, seq, e, p, at):
        part = self.parts[p["serial"]]
        part["bindings"].append({"vehicle_id": p["vehicle_id"], "cert_use": p["cert_use"], "at": at})

    def _on_work_attested(self, seq, e, p, at):
        work = {
            "work_id": p["work_id"],
            "vehicle_id": p["vehicle_id"],
            "serial": p["serial"],
            "technician_id": p["technician_id"],
            "at": at,
            "seq": seq,
        }
        self.works.append(work)
        part = self.parts[p["serial"]]
        part["installed_in"] = p["vehicle_id"]

    def _on_part_replaced(self, seq, e, p, at):
        old = self.parts[p["old_serial"]]
        old["installed_in"] = None
        old["removed_from"] = p["vehicle_id"]
        new = self.parts[p["new_serial"]]
        new["installed_in"] = p["vehicle_id"]
        if not any(b["vehicle_id"] == p["vehicle_id"] for b in new["bindings"]):
            new["bindings"].append({"vehicle_id": p["vehicle_id"], "cert_use": p.get("cert_use"), "at": at})
        self.works.append(
            {
                "work_id": e["aggregate_id"],
                "vehicle_id": p["vehicle_id"],
                "serial": p["new_serial"],
                "old_serial": p["old_serial"],
                "technician_id": p["replaced_by"],
                "at": at,
                "seq": seq,
                "replacement_for_case": p["case_id"],
            }
        )

    def _on_inspection_result_received(self, seq, e, p, at):
        self.reports[p["report_no"]].append(
            {
                "content_hash": p["content_hash"],
                "result": p["result"],
                "agency": p["agency"],
                "received_at": p["received_at"],
                "vehicle_id": p.get("vehicle_id"),
                "serial": p.get("serial"),
                "seq": seq,
            }
        )

    def _on_dispute_opened(self, seq, e, p, at):
        self.disputes.append(
            {
                "dispute_id": p["dispute_id"],
                "report_no": p["report_no"],
                "prior_content_hash": p["prior_content_hash"],
                "new_content_hash": p["new_content_hash"],
                "new_result": p.get("new_result"),
                "opened_at": p["opened_at"],
                "status": "OPEN",
                "chosen_content_hash": None,
                "seq": seq,
            }
        )

    def _on_dispute_resolved(self, seq, e, p, at):
        for dispute in reversed(self.disputes):
            if dispute["dispute_id"] == p["dispute_id"] and dispute["status"] == "OPEN":
                dispute["status"] = "RESOLVED"
                dispute["chosen_content_hash"] = p["chosen_content_hash"]
                break

    def _on_inspection_signed(self, seq, e, p, at):
        # 基线事件：检测结论同样作为证据保留
        if not any(r["result"] == p["result"] for r in self.reports[p["report_no"]]):
            self.reports[p["report_no"]].append(
                {
                    "content_hash": f"signed:{p['report_no']}",
                    "result": p["result"],
                    "agency": p.get("inspector_id"),
                    "received_at": at,
                    "vehicle_id": p["vehicle_id"],
                    "serial": p.get("serial"),
                    "seq": seq,
                }
            )

    def _on_pass_activated(self, seq, e, p, at):
        self.passes[p["pass_id"]] = {
            "pass_id": p["pass_id"],
            "vehicle_id": p["vehicle_id"],
            "restrictions": [],
        }

    def _on_pass_restriction_imposed(self, seq, e, p, at):
        pass_ = self.passes[p["pass_id"]]
        pass_["restrictions"].append(
            {
                "case_id": p["case_id"],
                "restriction": p["restriction"],
                "effective_at": p["effective_at"],
                "lifted": False,
                "lift_event": None,
            }
        )

    def _on_pass_restriction_lifted(self, seq, e, p, at):
        pass_ = self.passes[p["pass_id"]]
        for restriction in reversed(pass_["restrictions"]):
            if restriction["case_id"] == p["case_id"] and not restriction["lifted"]:
                restriction["lifted"] = True
                restriction["lift_event"] = e["event_id"]
                break

    def _on_freeze_scope_delineated(self, seq, e, p, at):
        self.cases[p["case_id"]] = {
            "case_id": p["case_id"],
            "cert_no": p["cert_no"],
            "suspended_uses": set(p["suspended_uses"]),
            "effective_at": p.get("effective_at"),
            "scope": {v["vehicle_id"]: dict(v) for v in p["vehicles"]},
            "scope_hash": p.get("scope_hash"),
            "confirmed": False,
            "confirmed_by": None,
            "dispositions": {},
            "remediation": {},
            "restored": set(),
            "finalized": {},
            "paused": False,
            "paused_since": None,
        }

    def _on_matching_rules_confirmed(self, seq, e, p, at):
        case = self.cases[p["case_id"]]
        case["confirmed"] = True
        case["confirmed_by"] = p["technician_id"]

    def _on_disposition_decided(self, seq, e, p, at):
        case = self.cases[p["case_id"]]
        for item in p["items"]:
            case["dispositions"][item["vehicle_id"]] = {
                "action": item["action"],
                "decided_by": p["decided_by"],
                "at": at,
                "due_at": p.get("remediation_due_at"),
            }

    def _on_part_quarantined(self, seq, e, p, at):
        case = self.cases[p["case_id"]]
        for serial in p["serials"]:
            case["remediation"].setdefault(serial, {})["quarantined"] = True

    def _on_appeal_filed(self, seq, e, p, at):
        self.appeals[p["appeal_id"]] = {
            "appeal_id": p["appeal_id"],
            "case_id": p["case_id"],
            "vehicle_ids": list(p["vehicle_ids"]),
            "filed_by": p["filed_by"],
            "status": "OPEN",
            "outcome": None,
        }

    def _on_appeal_decided(self, seq, e, p, at):
        appeal = self.appeals[p["appeal_id"]]
        appeal["status"] = "DECIDED"
        appeal["outcome"] = p["outcome"]

    def _on_administrative_finalization(self, seq, e, p, at):
        self.cases[p["case_id"]]["finalized"][p["vehicle_id"]] = {
            "conclusion": p["conclusion"],
            "decided_by": p["decided_by"],
            "at": at,
        }

    def _on_process_paused(self, seq, e, p, at):
        case = self.cases[p["case_id"]]
        case["paused"] = True
        case["paused_since"] = p["paused_at"]

    def _on_process_resumed(self, seq, e, p, at):
        case = self.cases[p["case_id"]]
        case["paused"] = False
        case["paused_since"] = None
        case["original_due_at"] = p["original_due_at"]

    def _on_notice_issued(self, seq, e, p, at):
        self.notices.append({**p, "at": at, "seq": seq})

    def _on_escalation_scheduled(self, seq, e, p, at):
        self.escalations.append(
            {
                "case_id": p["case_id"],
                "level": p["level"],
                "due_at": p["due_at"],
                "anchor_due_at": p["anchor_due_at"],
                "delivered": False,
                "delivered_at": None,
            }
        )

    def _on_escalation_delivered(self, seq, e, p, at):
        for escalation in reversed(self.escalations):
            if escalation["case_id"] == p["case_id"] and escalation["level"] == p["level"]:
                escalation["delivered"] = True
                escalation["delivered_at"] = p["delivered_at"]
                escalation["due_at"] = p["due_at"]
                break

    def _on_restoration_completed(self, seq, e, p, at):
        self.cases[p["case_id"]]["restored"].update(p["vehicle_ids"])

    # ---- 查询 ----------------------------------------------------------------

    def active_suspension(self, cert_no: str) -> dict | None:
        cert = self.certificates.get(cert_no)
        if not cert:
            return None
        for suspension in reversed(cert["suspensions"]):
            if suspension["active"]:
                return suspension
        return None

    def cert_usable_for(self, cert_no: str, use: str) -> bool:
        suspension = self.active_suspension(cert_no)
        return suspension is None or use not in suspension["suspended_uses"]

    def pass_usable(self, pass_id: str) -> bool:
        pass_ = self.passes.get(pass_id)
        if not pass_:
            return False
        return all(r["lifted"] for r in pass_["restrictions"])

    def pass_for_vehicle(self, vehicle_id: str) -> str | None:
        for pass_id, pass_ in self.passes.items():
            if pass_["vehicle_id"] == vehicle_id:
                return pass_id
        return None

    def vehicles_by_serial(self, serial: str) -> list[str]:
        part = self.parts.get(serial)
        if not part:
            return []
        vehicles = [b["vehicle_id"] for b in part["bindings"]]
        if part.get("removed_from") and part["removed_from"] not in vehicles:
            vehicles.append(part["removed_from"])
        if part.get("installed_in") and part["installed_in"] not in vehicles:
            vehicles.append(part["installed_in"])
        return vehicles

    def owner_at(self, vehicle_id: str, when: str) -> str | None:
        vehicle = self.vehicles.get(vehicle_id)
        if not vehicle:
            return None
        moment = _parse(when)
        for period in vehicle["ownership"]:
            start = _parse(period["from"])
            end = _parse(period["to"])
            if start <= moment and (end is None or moment < end):
                return period["owner"]
        return vehicle["ownership"][-1]["owner"]

    def open_appeals(self, case_id: str, vehicle_id: str) -> list[dict]:
        return [
            a
            for a in self.appeals.values()
            if a["case_id"] == case_id and vehicle_id in a["vehicle_ids"] and a["status"] == "OPEN"
        ]

    def report_eligible_as_basis(self, report_no: str, vehicle_id: str, not_before: str) -> bool:
        """检测结果能否作为恢复依据：合格、新于暂停、无未决争议。

        同编号内容变化形成争议时，两个版本都留证；争议裁决后以被选中的内容为准，
        后到的内容不会自动覆盖先前证据。
        """
        versions = self.reports.get(report_no, [])
        if not versions:
            return False
        if any(d["report_no"] == report_no and d["status"] == "OPEN" for d in self.disputes):
            return False
        resolved = [d for d in self.disputes if d["report_no"] == report_no and d["status"] == "RESOLVED"]
        if resolved:
            chosen = resolved[-1]["chosen_content_hash"]
            effective = next((v for v in versions if v["content_hash"] == chosen), None)
        else:
            effective = versions[-1]
        if effective is None or effective["result"] != "PASS" or effective["vehicle_id"] != vehicle_id:
            return False
        if _parse(effective["received_at"]) <= _parse(not_before):
            return False
        return True

    def effective_report(self, report_no: str) -> dict | None:
        """返回当前有效的报告版本（有争议时取裁决选中内容，否则取唯一/最新内容）。"""
        versions = self.reports.get(report_no, [])
        if not versions:
            return None
        if any(d["report_no"] == report_no and d["status"] == "OPEN" for d in self.disputes):
            return None
        resolved = [d for d in self.disputes if d["report_no"] == report_no and d["status"] == "RESOLVED"]
        if resolved:
            chosen = resolved[-1]["chosen_content_hash"]
            return next((v for v in versions if v["content_hash"] == chosen), None)
        return versions[-1]

    def disposition_action(self, case_id: str, vehicle_id: str) -> str | None:
        disposition = self.cases.get(case_id, {}).get("dispositions", {}).get(vehicle_id)
        return disposition["action"] if disposition else None
