"""认证暂停影响冻结服务。

处置原则（与业务约定一一对应）：

1. 不按车型一刀切：沿“部件证书 → 序列号 → 绑定/施工记录”圈定车辆，并按证书被
   暂停的用途（cert_use）精确过滤；同车型但使用其他批次/证书的车辆不入范围。
2. 先由技术人员确认匹配规则（范围哈希锁定，不可扩大），合规人员再逐车决定
   暂停、复检或换件；两个角色不得为同一人。
3. 换件后必须由“另一人”依据新的合格检测结果恢复；恢复可逐车部分进行，已恢复
   车辆与仍受限车辆互不影响。
4. 申诉只暂缓行政结论；通行证安全限制在客观整改完成前持续有效。
5. 所有权变化只追加过户记录：通知送达当前车主，原施工方与历任车主的责任时间线
   原样保留。
6. 检测机构离线回传：相同编号 + 相同内容只接受一次；相同编号内容变化时，新结果
   形成独立争议，先前证据不被覆盖。
7. 整改期限与升级排程一律锚定证书暂停时的原截止点；进程暂停/恢复不重新计时。
"""

import hashlib
import itertools
import json
from datetime import datetime

from src.events import (
    DISPOSITION_QUARANTINE_PART,
    DISPOSITION_REINSPECT,
    DISPOSITION_REPLACE_PART,
    DISPOSITION_SUSPEND_PASS,
    DISPOSITIONS,
    STATE_ALREADY_RESOLD,
    STATE_AWAITING_INSPECTION,
    STATE_NOT_INSTALLED,
    STATE_PASS_HELD,
)
from src.projection import Projection
from src.store import EventStore


class DomainError(RuntimeError):
    """业务规则被违反。"""


def _canonical_hash(payload: object) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ComplianceFreezeService:
    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()
        self._ids = itertools.count(1)

    # ---- 内部工具 ------------------------------------------------------------

    def _nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"

    def _projection(self) -> Projection:
        return Projection(self.store.all_events())

    def _append(self, event_type, aggregate_type, aggregate_id, occurred_at, summary, payload,
                expected_version=None, idempotency_key=None):
        record = {
            "event_id": self._nid("evt"),
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "summary": summary,
            "payload": payload,
        }
        return self.store.append(record, expected_version, idempotency_key)

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat()

    # ---- 基础登记 ------------------------------------------------------------

    def register_model(self, model_id: str, allowed_uses: list[str], at: str | None = None):
        return self._append(
            "MODEL_DECLARED", "model_config", f"model-{model_id}", at or self._now(),
            f"登记车型配置 {model_id}", {"model_id": model_id, "allowed_uses": allowed_uses})

    def register_vehicle(self, vehicle_id: str, model_id: str, owner: str, at: str | None = None):
        return self._append(
            "VEHICLE_REGISTERED", "vehicle_profile", f"vehicle-{vehicle_id}", at or self._now(),
            f"登记车辆 {vehicle_id}（车型 {model_id}，车主 {owner}）",
            {"vehicle_id": vehicle_id, "model_id": model_id, "owner": owner})

    def register_certificate(self, cert_no: str, part_type: str, uses: list[str], at: str | None = None):
        return self._append(
            "CERTIFICATE_REGISTERED", "part_certificate", f"cert-{cert_no}", at or self._now(),
            f"登记部件认证 {cert_no}（{part_type}，用途 {uses}）",
            {"cert_no": cert_no, "part_type": part_type, "uses": uses})

    def suspend_certificate(self, cert_no: str, suspended_uses: list[str],
                            effective_at: str, remediation_due_at: str):
        p = self._projection()
        cert = p.certificates.get(cert_no)
        if cert is None:
            raise DomainError(f"认证不存在：{cert_no}")
        unknown = set(suspended_uses) - set(cert["uses"])
        if unknown:
            raise DomainError(f"认证 {cert_no} 未授权用途：{sorted(unknown)}")
        if p.active_suspension(cert_no) is not None:
            raise DomainError(f"认证 {cert_no} 已处于暂停状态，应先恢复或追加后继记录")
        return self._append(
            "CERTIFICATE_SUSPENDED", "part_certificate", f"cert-{cert_no}", effective_at,
            f"暂停认证 {cert_no} 的用途 {suspended_uses}，整改截止 {remediation_due_at}",
            {"cert_no": cert_no, "suspended_uses": suspended_uses,
             "effective_at": effective_at, "remediation_due_at": remediation_due_at})

    def reinstate_certificate(self, cert_no: str, reinstated_at: str):
        p = self._projection()
        if p.active_suspension(cert_no) is None:
            raise DomainError(f"认证 {cert_no} 当前没有生效中的暂停")
        return self._append(
            "CERTIFICATE_REINSTATED", "part_certificate", f"cert-{cert_no}", reinstated_at,
            f"恢复认证 {cert_no}", {"cert_no": cert_no, "reinstated_at": reinstated_at})

    def register_part(self, serial: str, cert_no: str, batch_no: str, at: str | None = None):
        p = self._projection()
        if cert_no not in p.certificates:
            raise DomainError(f"认证不存在：{cert_no}")
        return self._append(
            "PART_SERIAL_REGISTERED", "part_instance", f"part-{serial}", at or self._now(),
            f"登记部件序列号 {serial}（批次 {batch_no}，认证 {cert_no}）",
            {"serial": serial, "cert_no": cert_no, "batch_no": batch_no})

    def bind_part(self, serial: str, vehicle_id: str, cert_use: str, at: str | None = None):
        """部件配发/绑定到车辆但尚未施工（未施工状态的来源）。"""
        p = self._projection()
        part = p.parts.get(serial)
        if part is None:
            raise DomainError(f"部件不存在：{serial}")
        cert = p.certificates[part["cert_no"]]
        if cert_use not in cert["uses"]:
            raise DomainError(f"用途 {cert_use} 不在认证 {part['cert_no']} 授权范围内")
        if vehicle_id not in p.vehicles:
            raise DomainError(f"车辆不存在：{vehicle_id}")
        return self._append(
            "PART_BOUND", "part_instance", f"part-{serial}", at or self._now(),
            f"部件 {serial} 按用途 {cert_use} 配发至车辆 {vehicle_id}（未施工）",
            {"serial": serial, "vehicle_id": vehicle_id, "cert_use": cert_use})

    def attest_work(self, work_id: str, vehicle_id: str, serial: str, technician_id: str, at: str):
        p = self._projection()
        if vehicle_id not in p.vehicles:
            raise DomainError(f"车辆不存在：{vehicle_id}")
        part = p.parts.get(serial)
        if part is None:
            raise DomainError(f"部件不存在：{serial}")
        return self._append(
            "WORK_ATTESTED", "work_record", f"work-{work_id}", at,
            f"施工方 {technician_id} 为车辆 {vehicle_id} 安装部件 {serial}",
            {"work_id": work_id, "vehicle_id": vehicle_id, "serial": serial,
             "technician_id": technician_id})

    def activate_pass(self, pass_id: str, vehicle_id: str, at: str):
        p = self._projection()
        if vehicle_id not in p.vehicles:
            raise DomainError(f"车辆不存在：{vehicle_id}")
        existing = p.pass_for_vehicle(vehicle_id)
        if existing:
            raise DomainError(f"车辆 {vehicle_id} 已持有通行证 {existing}")
        return self._append(
            "PASS_ACTIVATED", "compliance_pass", f"pass-{pass_id}", at,
            f"车辆 {vehicle_id} 取得通行证 {pass_id}", {"pass_id": pass_id, "vehicle_id": vehicle_id})

    def transfer_ownership(self, vehicle_id: str, from_owner: str, to_owner: str, transferred_at: str):
        p = self._projection()
        vehicle = p.vehicles.get(vehicle_id)
        if vehicle is None:
            raise DomainError(f"车辆不存在：{vehicle_id}")
        if vehicle["owner"] != from_owner:
            raise DomainError(
                f"当前车主为 {vehicle['owner']}，与申报的出让方 {from_owner} 不一致")
        return self._append(
            "OWNERSHIP_TRANSFERRED", "vehicle_profile", f"vehicle-{vehicle_id}", transferred_at,
            f"车辆 {vehicle_id} 所有权由 {from_owner} 转移给 {to_owner}",
            {"vehicle_id": vehicle_id, "from_owner": from_owner, "to_owner": to_owner,
             "transferred_at": transferred_at})

    # ---- 影响圈定 ------------------------------------------------------------

    def delineate_scope(self, case_id: str, cert_no: str, at: str | None = None) -> dict:
        """沿部件序列号与施工记录圈定受影响车辆，按证书用途精确过滤。"""
        p = self._projection()
        suspension = p.active_suspension(cert_no)
        if suspension is None:
            raise DomainError(f"认证 {cert_no} 没有生效中的暂停，无需圈定")
        suspended_uses = suspension["suspended_uses"]

        affected: dict[str, dict] = {}
        for serial, part in p.parts.items():
            if part["cert_no"] != cert_no:
                continue
            uses_on_vehicle: dict[str, str] = {}
            for binding in part["bindings"]:
                if binding["cert_use"] in suspended_uses:
                    uses_on_vehicle[binding["vehicle_id"]] = binding["cert_use"]
            # 施工记录可能留下已解绑的历史安装，仍须纳入安全范围
            for work in p.works:
                if work["serial"] == serial:
                    bound_use = next(
                        (b["cert_use"] for b in part["bindings"] if b["vehicle_id"] == work["vehicle_id"]),
                        None,
                    )
                    if bound_use in suspended_uses:
                        uses_on_vehicle.setdefault(work["vehicle_id"], bound_use)
            for vehicle_id, cert_use in uses_on_vehicle.items():
                entry = affected.setdefault(
                    vehicle_id,
                    {"vehicle_id": vehicle_id, "serials": [], "uses": set()},
                )
                entry["serials"].append(serial)
                entry["uses"].add(cert_use)

        vehicles = []
        for vehicle_id, entry in sorted(affected.items()):
            installed = any(
                w["vehicle_id"] == vehicle_id and w["serial"] in entry["serials"] for w in p.works
            )
            if not installed:
                state = STATE_NOT_INSTALLED
            elif p.pass_for_vehicle(vehicle_id):
                state = STATE_PASS_HELD
            else:
                state = STATE_AWAITING_INSPECTION
            current_owner = p.vehicles[vehicle_id]["owner"]
            first_period = p.vehicles[vehicle_id]["ownership"][0]
            resold = current_owner != first_period["owner"]
            vehicles.append({
                "vehicle_id": vehicle_id,
                "state": state,
                "serials": sorted(entry["serials"]),
                "cert_uses": sorted(entry["uses"]),
                "resold": resold,
                "current_owner": current_owner,
                "overlay": STATE_ALREADY_RESOLD if resold else None,
            })

        scope_hash = _canonical_hash(
            [{"vehicle_id": v["vehicle_id"], "serials": v["serials"], "uses": v["cert_uses"]} for v in vehicles]
        )
        event = self._append(
            "FREEZE_SCOPE_DELINEATED", "freeze_case", f"case-{case_id}", at or self._now(),
            f"认证 {cert_no} 暂停影响圈定：{len(vehicles)} 辆车（按用途 {sorted(suspended_uses)} 精确匹配）",
            {"case_id": case_id, "cert_no": cert_no,
             "suspended_uses": sorted(suspended_uses), "vehicles": vehicles,
             "scope_hash": scope_hash,
             "effective_at": suspension["effective_at"],
             "remediation_due_at": suspension["remediation_due_at"]})
        return event

    # ---- 技师确认 + 合规处置 -------------------------------------------------

    def confirm_matching_rules(self, case_id: str, technician_id: str,
                               confirmed_vehicle_ids: list[str], scope_hash: str, at: str | None = None):
        """技术人员确认匹配规则；确认集合必须与圈定范围逐车一致，哈希不符即拒绝。"""
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在：{case_id}")
        if case["confirmed"]:
            raise DomainError("匹配规则已经确认，确认记录不可改写")
        if scope_hash != case["scope_hash"]:
            raise DomainError("范围哈希与圈定结果不一致，疑似范围被扩大或缩小")
        if set(confirmed_vehicle_ids) != set(case["scope"]):
            raise DomainError("确认车辆集合与圈定范围不一致")
        return self._append(
            "MATCHING_RULES_CONFIRMED", "freeze_case", f"case-{case_id}", at or self._now(),
            f"技术人员 {technician_id} 确认案件 {case_id} 的部件序列与施工匹配规则",
            {"case_id": case_id, "scope_hash": scope_hash,
             "vehicle_ids": sorted(confirmed_vehicle_ids), "technician_id": technician_id})

    def decide_disposition(self, case_id: str, items: list[dict], decided_by: str,
                           expected_version: int | None = None, at: str | None = None) -> list[dict]:
        """合规人员逐车决定暂停 / 复检 / 换件；处置集合必须恰好等于已确认范围。"""
        # 并发复核先于领域判断：持过期版本的调用方必须重新读取
        if expected_version is not None:
            current_version = self.store.version_of(f"case-{case_id}")
            if current_version != expected_version:
                from src.store import ConcurrencyConflict
                raise ConcurrencyConflict(
                    f"案件 {case_id} 版本冲突：期望 {expected_version}，实际 {current_version}")
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在：{case_id}")
        if not case["confirmed"]:
            raise DomainError("匹配规则未经技术人员确认，不得处置")
        if case["dispositions"]:
            raise DomainError("处置已经作出；后续变化须以整改、换件、恢复等后继事件表达，不得重新处置")
        if decided_by == case["confirmed_by"]:
            raise DomainError("匹配规则确认人与合规处置决定人不得为同一人")
        if len({i["vehicle_id"] for i in items}) != len(items):
            raise DomainError("同一车辆出现多条处置")
        chosen = {i["vehicle_id"]: i["action"] for i in items}
        if set(chosen) != set(case["scope"]):
            raise DomainError("处置必须覆盖且不得超出圈定范围（不能按车型扩大）")
        for vehicle_id, action in chosen.items():
            if action not in DISPOSITIONS:
                raise DomainError(f"未知处置动作：{action}")
            state = case["scope"][vehicle_id]["state"]
            if action == DISPOSITION_QUARANTINE_PART and state != STATE_NOT_INSTALLED:
                raise DomainError(f"车辆 {vehicle_id} 已施工，不能仅封存部件")
            if action == DISPOSITION_REINSPECT and state == STATE_NOT_INSTALLED:
                raise DomainError(f"车辆 {vehicle_id} 尚未施工，应封存部件而非复检")
            if action == DISPOSITION_SUSPEND_PASS and state != STATE_PASS_HELD:
                raise DomainError(f"车辆 {vehicle_id} 未持有通行证，无法暂停通行证")

        suspension = p.active_suspension(case["cert_no"])
        due_at = suspension["remediation_due_at"]
        records: list[tuple[dict, int | None, str | None]] = []

        decision = {
            "event_id": self._nid("evt"),
            "event_type": "DISPOSITION_DECIDED",
            "aggregate_type": "freeze_case",
            "aggregate_id": f"case-{case_id}",
            "occurred_at": at or self._now(),
            "summary": f"合规人员 {decided_by} 对案件 {case_id} 的 {len(items)} 辆车作出处置",
            "payload": {
                "case_id": case_id,
                "items": sorted(items, key=lambda i: i["vehicle_id"]),
                "decided_by": decided_by,
                "remediation_due_at": due_at,
            },
        }
        records.append((decision, expected_version, None))

        for item in sorted(items, key=lambda i: i["vehicle_id"]):
            vehicle_id, action = item["vehicle_id"], item["action"]
            owner = case["scope"][vehicle_id]["current_owner"]
            pass_id = p.pass_for_vehicle(vehicle_id)
            if action in (DISPOSITION_SUSPEND_PASS, DISPOSITION_REPLACE_PART,
                          DISPOSITION_REINSPECT) and pass_id:
                records.append(({
                    "event_id": self._nid("evt"),
                    "event_type": "PASS_RESTRICTION_IMPOSED",
                    "aggregate_type": "compliance_pass",
                    "aggregate_id": f"pass-{pass_id}",
                    "occurred_at": at or self._now(),
                    "summary": f"通行证 {pass_id} 因案件 {case_id} 被暂停使用（安全限制）",
                    "payload": {
                        "pass_id": pass_id, "case_id": case_id,
                        "restriction": f"SUSPENDED_UNDER_{case_id}",
                        "effective_at": suspension["effective_at"],
                    },
                }, None, None))
            if action == DISPOSITION_QUARANTINE_PART:
                records.append(({
                    "event_id": self._nid("evt"),
                    "event_type": "PART_QUARANTINED",
                    "aggregate_type": "freeze_case",
                    "aggregate_id": f"case-{case_id}",
                    "occurred_at": at or self._now(),
                    "summary": f"案件 {case_id} 封存车辆 {vehicle_id} 名下未施工部件",
                    "payload": {"case_id": case_id,
                                "serials": case["scope"][vehicle_id]["serials"]},
                }, None, None))
            notice_id = self._nid("notice")
            records.append(({
                "event_id": self._nid("evt"),
                "event_type": "NOTICE_ISSUED",
                "aggregate_type": "notice",
                "aggregate_id": f"notice-{notice_id}",
                "occurred_at": at or self._now(),
                "summary": f"向当前车主 {owner} 送达车辆 {vehicle_id} 的处置通知",
                "payload": {"notice_id": notice_id, "case_id": case_id,
                            "vehicle_id": vehicle_id, "recipient_owner": owner,
                            "kind": "DISPOSITION", "action": action},
            }, None, None))

        # 升级排程锚定原整改截止点，不自行另设期限
        records.append(({
            "event_id": self._nid("evt"),
            "event_type": "ESCALATION_SCHEDULED",
            "aggregate_type": "escalation",
            "aggregate_id": f"escalation-{case_id}",
            "occurred_at": at or self._now(),
            "summary": f"案件 {case_id} 升级通知按原截止点 {due_at} 排程",
            "payload": {"case_id": case_id, "level": 1, "due_at": due_at,
                        "anchor_due_at": due_at},
        }, None, None))

        return self.store.append_many(records)

    # ---- 申诉与行政结论 ------------------------------------------------------

    def file_appeal(self, appeal_id: str, case_id: str, vehicle_ids: list[str],
                    filed_by: str, at: str | None = None):
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在：{case_id}")
        outside = set(vehicle_ids) - set(case["scope"])
        if outside:
            raise DomainError(f"申诉车辆不在案件范围内：{sorted(outside)}")
        return self._append(
            "APPEAL_FILED", "appeal", f"appeal-{appeal_id}", at or self._now(),
            f"{filed_by} 就案件 {case_id} 中 {sorted(vehicle_ids)} 提出申诉（仅暂缓行政结论）",
            {"appeal_id": appeal_id, "case_id": case_id,
             "vehicle_ids": sorted(vehicle_ids), "filed_by": filed_by})

    def decide_appeal(self, appeal_id: str, outcome: str, decided_by: str, at: str | None = None):
        if outcome not in ("UPHELD", "REJECTED"):
            raise DomainError("申诉结论必须是 UPHELD 或 REJECTED")
        p = self._projection()
        appeal = p.appeals.get(appeal_id)
        if appeal is None:
            raise DomainError(f"申诉不存在：{appeal_id}")
        if appeal["status"] != "OPEN":
            raise DomainError("申诉已裁决，记录不可改写")
        return self._append(
            "APPEAL_DECIDED", "appeal", f"appeal-{appeal_id}", at or self._now(),
            f"申诉 {appeal_id} 裁决为 {outcome}（安全限制以整改与检测为准，不受申诉影响）",
            {"appeal_id": appeal_id, "outcome": outcome, "decided_by": decided_by})

    def finalize_administrative(self, case_id: str, vehicle_id: str, conclusion: str,
                                decided_by: str, at: str | None = None):
        """作出行政结论：申诉未决时暂缓；安全限制是否解除与此完全独立。"""
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None or vehicle_id not in case["scope"]:
            raise DomainError(f"车辆 {vehicle_id} 不在案件 {case_id} 范围内")
        if p.open_appeals(case_id, vehicle_id):
            raise DomainError("存在未决申诉，行政结论暂缓作出")
        disposition = case["dispositions"].get(vehicle_id)
        remediated = (
            vehicle_id in case["restored"]
            or (disposition and disposition["action"] == DISPOSITION_QUARANTINE_PART)
        )
        if not remediated:
            raise DomainError("整改尚未完成，不能作出结案式行政结论")
        return self._append(
            "ADMINISTRATIVE_FINALIZATION", "freeze_case", f"case-{case_id}", at or self._now(),
            f"车辆 {vehicle_id} 行政结论：{conclusion}",
            {"case_id": case_id, "vehicle_id": vehicle_id,
             "conclusion": conclusion, "decided_by": decided_by})

    # ---- 换件与恢复 ----------------------------------------------------------

    def replace_part(self, case_id: str, vehicle_id: str, old_serial: str, new_serial: str,
                     replaced_by: str, at: str):
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None or vehicle_id not in case["scope"]:
            raise DomainError(f"车辆 {vehicle_id} 不在案件 {case_id} 范围内")
        action = p.disposition_action(case_id, vehicle_id)
        if action not in (DISPOSITION_REPLACE_PART, DISPOSITION_SUSPEND_PASS):
            raise DomainError(f"车辆 {vehicle_id} 的处置不是换件，不能执行换件")
        if old_serial not in case["scope"][vehicle_id]["serials"]:
            raise DomainError(f"旧件 {old_serial} 不在圈定范围内")
        old_part = p.parts.get(old_serial)
        if old_part is None or old_part.get("installed_in") != vehicle_id:
            raise DomainError(f"旧件 {old_serial} 当前未安装在车辆 {vehicle_id} 上")
        new_part = p.parts.get(new_serial)
        if new_part is None:
            raise DomainError(f"新件 {new_serial} 未登记")
        if new_part["cert_no"] == case["cert_no"]:
            raise DomainError("不能以同一被暂停认证下的部件替换")
        if not p.cert_usable_for(new_part["cert_no"], case["scope"][vehicle_id]["cert_uses"][0]):
            raise DomainError(f"新件认证 {new_part['cert_no']} 对该用途仍不可用")
        owner = p.vehicles[vehicle_id]["owner"]
        notice_id = self._nid("notice")
        records = [
            ({
                "event_id": self._nid("evt"),
                "event_type": "PART_REPLACED",
                "aggregate_type": "work_record",
                "aggregate_id": f"work-replace-{case_id}-{vehicle_id}",
                "occurred_at": at,
                "summary": f"{replaced_by} 将车辆 {vehicle_id} 的 {old_serial} 换为 {new_serial}",
                "payload": {"case_id": case_id, "vehicle_id": vehicle_id,
                            "old_serial": old_serial, "new_serial": new_serial,
                            "replaced_by": replaced_by, "cert_use": case["scope"][vehicle_id]["cert_uses"][0]},
            }, None, None),
            ({
                "event_id": self._nid("evt"),
                "event_type": "NOTICE_ISSUED",
                "aggregate_type": "notice",
                "aggregate_id": f"notice-{notice_id}",
                "occurred_at": at,
                "summary": f"向当前车主 {owner} 送达车辆 {vehicle_id} 的换件完成通知",
                "payload": {"notice_id": notice_id, "case_id": case_id,
                            "vehicle_id": vehicle_id, "recipient_owner": owner,
                            "kind": "PART_REPLACED"},
            }, None, None),
        ]
        return self.store.append_many(records)

    def restore(self, case_id: str, vehicle_id: str, restored_by: str, report_no: str, at: str):
        """由换件/整改人之外的另一人，依据新的合格检测结果恢复；只恢复指定车辆。"""
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None or vehicle_id not in case["scope"]:
            raise DomainError(f"车辆 {vehicle_id} 不在案件 {case_id} 范围内")
        if vehicle_id in case["restored"]:
            raise DomainError(f"车辆 {vehicle_id} 已经恢复，不得重复恢复")
        suspension = p.active_suspension(case["cert_no"])
        # 认证整体恢复或单车客观整改完成均可，但必须有暂停之后的新检测依据
        effective_at = case.get("effective_at") or (suspension["effective_at"] if suspension else None)
        replacement = next(
            (e for e in reversed(self.store.all_events())
             if e["event_type"] == "PART_REPLACED"
             and e["payload"]["case_id"] == case_id
             and e["payload"]["vehicle_id"] == vehicle_id),
            None,
        )
        if replacement is not None:
            forbidden_actors = {replacement["payload"]["replaced_by"]}
        else:
            disposition = case["dispositions"].get(vehicle_id)
            if disposition is None or disposition["action"] != DISPOSITION_REINSPECT:
                raise DomainError("车辆既未换件也非复检处置，缺少恢复前提")
            # 复检场景：恢复人不得是原施工方，也不得是出具新检测的机构
            work = next((w for w in reversed(p.works) if w["vehicle_id"] == vehicle_id), None)
            forbidden_actors = set()
            if work:
                forbidden_actors.add(work["technician_id"])
        if restored_by in forbidden_actors:
            raise DomainError("恢复人必须是换件/整改经办人之外的另一人")
        if not p.report_eligible_as_basis(report_no, vehicle_id, effective_at):
            raise DomainError(
                f"检测报告 {report_no} 不能作为恢复依据：须为暂停后新作出的合格结论且无未决争议")
        basis = p.effective_report(report_no)
        if basis and basis.get("agency") == restored_by:
            raise DomainError("恢复人不得是出具新检测结果的机构本人")

        records: list[tuple[dict, int | None, str | None]] = []
        pass_id = p.pass_for_vehicle(vehicle_id)
        if pass_id is not None and not p.pass_usable(pass_id):
            records.append(({
                "event_id": self._nid("evt"),
                "event_type": "PASS_RESTRICTION_LIFTED",
                "aggregate_type": "compliance_pass",
                "aggregate_id": f"pass-{pass_id}",
                "occurred_at": at,
                "summary": f"{restored_by} 依据新检测 {report_no} 恢复通行证 {pass_id}（车辆 {vehicle_id}）",
                "payload": {"pass_id": pass_id, "case_id": case_id,
                            "restored_by": restored_by, "basis_report_no": report_no},
            }, None, None))
        records.append(({
            "event_id": self._nid("evt"),
            "event_type": "RESTORATION_COMPLETED",
            "aggregate_type": "freeze_case",
            "aggregate_id": f"case-{case_id}",
            "occurred_at": at,
            "summary": f"车辆 {vehicle_id} 由 {restored_by} 依据 {report_no} 完成恢复（部分恢复，不影响其他车辆）",
            "payload": {"case_id": case_id, "vehicle_ids": [vehicle_id],
                        "restored_by": restored_by, "basis_report_no": report_no},
        }, None, None))
        return self.store.append_many(records)

    # ---- 进程暂停/恢复与升级（期限锚定原截止点）-----------------------------

    def pause_process(self, case_id: str, paused_at: str):
        p = self._projection()
        if case_id not in p.cases:
            raise DomainError(f"案件不存在：{case_id}")
        if p.cases[case_id]["paused"]:
            raise DomainError("进程已处于暂停状态")
        return self._append(
            "PROCESS_PAUSED", "freeze_case", f"case-{case_id}", paused_at,
            f"案件 {case_id} 进程暂停；整改期限不重新计时",
            {"case_id": case_id, "paused_at": paused_at})

    def resume_process(self, case_id: str, resumed_at: str):
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在：{case_id}")
        if not case["paused"]:
            raise DomainError("进程未暂停，无需恢复")
        original_due = p.active_suspension(case["cert_no"])["remediation_due_at"]
        return self._append(
            "PROCESS_RESUMED", "freeze_case", f"case-{case_id}", resumed_at,
            f"案件 {case_id} 进程恢复；整改截止仍为 {original_due}，不重新计时",
            {"case_id": case_id, "resumed_at": resumed_at, "original_due_at": original_due})

    def deliver_escalation(self, case_id: str, level: int, delivered_at: str):
        """投递升级通知。暂停期间暂缓投递；恢复后按原定时间投递，不重置截止点。"""
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在：{case_id}")
        if case["paused"]:
            raise DomainError("进程暂停期间升级通知暂缓投递，恢复后按原时间补投")
        scheduled = next(
            (e for e in reversed(p.escalations) if e["case_id"] == case_id and e["level"] == level),
            None,
        )
        if scheduled is None:
            raise DomainError(f"案件 {case_id} 没有 {level} 级升级排程")
        if scheduled["delivered"]:
            raise DomainError("该升级通知已投递，不能重复投递")
        return self._append(
            "ESCALATION_DELIVERED", "escalation", f"escalation-{case_id}", delivered_at,
            f"案件 {case_id} 的 {level} 级升级通知按原定时间 {scheduled['due_at']} 投递",
            {"case_id": case_id, "level": level,
             "due_at": scheduled["due_at"], "delivered_at": delivered_at})

    # ---- 过户后的当前通知转投 ------------------------------------------------

    def forward_pending_notices(self, case_id: str, at: str) -> list[dict]:
        """所有权变化后，把尚未恢复车辆的当前通知送达新车主。"""
        p = self._projection()
        case = p.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在：{case_id}")
        records = []
        for vehicle_id, scope_entry in sorted(case["scope"].items()):
            if vehicle_id in case["restored"]:
                continue
            new_owner = p.vehicles[vehicle_id]["owner"]
            prior_recipients = {
                n["recipient_owner"]
                for n in p.notices
                if n["case_id"] == case_id and n["vehicle_id"] == vehicle_id
            }
            # 仅在所有权确已变化（当前车主未收过本案通知）时转投
            if new_owner in prior_recipients:
                continue
            forward_notice_id = self._nid("notice")
            records.append(({
                "event_id": self._nid("evt"),
                "event_type": "NOTICE_ISSUED",
                "aggregate_type": "notice",
                "aggregate_id": f"notice-{forward_notice_id}",
                "occurred_at": at,
                "summary": f"车辆 {vehicle_id} 已过户，当前处置通知转送给新车主 {new_owner}",
                "payload": {"notice_id": forward_notice_id, "case_id": case_id,
                            "vehicle_id": vehicle_id, "recipient_owner": new_owner,
                            "kind": "OWNER_FORWARD"},
            }, None, None))
        return self.store.append_many(records) if records else []

    # ---- 检测机构离线回传 ----------------------------------------------------

    def receive_inspection_report(self, report_no: str, agency: str, content_hash: str,
                                  result: str, received_at: str,
                                  vehicle_id: str | None = None, serial: str | None = None) -> str:
        """接收离线回传。

        返回 ACCEPTED / DISPUTE_OPENED；相同编号且相同内容重复回传抛 DuplicateEvent。
        """
        if result not in ("PASS", "FAIL"):
            raise DomainError("检测结果必须是 PASS 或 FAIL")
        p = self._projection()
        prior = p.reports.get(report_no, [])
        key = f"report:{report_no}:{content_hash}"
        record = {
            "event_id": self._nid("evt"),
            "event_type": "INSPECTION_RESULT_RECEIVED",
            "aggregate_type": "inspection_report",
            "aggregate_id": f"report-{report_no}",
            "occurred_at": received_at,
            "summary": f"检测机构 {agency} 离线回传报告 {report_no}（{result}）",
            "payload": {"report_no": report_no, "agency": agency, "content_hash": content_hash,
                        "result": result, "received_at": received_at,
                        "vehicle_id": vehicle_id, "serial": serial},
        }
        self.store.append(record, idempotency_key=key)

        if prior and prior[-1]["content_hash"] != content_hash:
            dispute_id = self._nid("dispute")
            self._append(
                "DISPUTE_OPENED", "dispute", dispute_id, received_at,
                f"报告 {report_no} 内容发生变化，形成独立争议；先前证据保留不覆盖",
                {"dispute_id": dispute_id, "report_no": report_no,
                 "prior_content_hash": prior[-1]["content_hash"],
                 "new_content_hash": content_hash, "new_result": result,
                 "opened_at": received_at,
                 "vehicle_id": vehicle_id})
            return "DISPUTE_OPENED"
        return "ACCEPTED"

    def resolve_dispute(self, dispute_id: str, chosen_content_hash: str,
                        resolved_by: str, at: str | None = None):
        p = self._projection()
        dispute = next((d for d in p.disputes if d["dispute_id"] == dispute_id and d["status"] == "OPEN"), None)
        if dispute is None:
            raise DomainError(f"未决争议不存在：{dispute_id}")
        allowed = {dispute["prior_content_hash"], dispute["new_content_hash"]}
        if chosen_content_hash not in allowed:
            raise DomainError("裁决只能在先后两份内容之间选择，两个版本均已留证")
        return self._append(
            "DISPUTE_RESOLVED", "dispute", dispute_id, at or self._now(),
            f"争议 {dispute_id} 裁决采用内容 {chosen_content_hash[:12]}…",
            {"dispute_id": dispute_id, "chosen_content_hash": chosen_content_hash,
             "resolved_by": resolved_by})
