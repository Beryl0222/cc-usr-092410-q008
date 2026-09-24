"""领域事件目录：事件类型、聚合类型与载荷必填项。

记录一经接收即不可变：标识、发生时间、版本均不得原地改写；更正与状态变化
一律追加后继事件。事件类型按“认证暂停影响冻结服务”的完整链路扩展。
"""

# 聚合类型
AGGREGATE_TYPES = (
    "vehicle_profile",       # 车辆（含车型、历任车主）
    "model_config",          # 车型配置
    "modification_plan",     # 改装方案（基线保留）
    "part_certificate",      # 部件批次认证（按用途授权）
    "part_instance",         # 部件序列号实例
    "work_record",           # 施工履历
    "inspection_decision",   # 检测结论（基线保留）
    "inspection_report",     # 检测机构回传报告（离线回传）
    "compliance_pass",       # 改装合规通行证
    "freeze_case",           # 一次认证暂停影响处置案件
    "appeal",                # 申诉
    "dispute",               # 检测争议（同编号不同内容）
    "notice",                # 通知（送达当前车主）
    "escalation",            # 升级通知
)

# 基线事件
_BASELINE = {
    "PLAN_DECLARED": ("vehicle_profile", frozenset()),
    "PART_BOUND": ("part_instance", frozenset({"serial", "vehicle_id", "cert_use"})),
    "WORK_ATTESTED": ("work_record", frozenset({"work_id", "vehicle_id", "serial", "technician_id"})),
    "INSPECTION_SIGNED": ("inspection_decision", frozenset({"report_no", "vehicle_id", "result", "inspector_id"})),
    "PASS_ACTIVATED": ("compliance_pass", frozenset({"pass_id", "vehicle_id"})),
}

# 冻结服务事件
_FREEZE = {
    # 基础登记
    "MODEL_DECLARED": ("model_config", frozenset({"model_id"})),
    "VEHICLE_REGISTERED": ("vehicle_profile", frozenset({"vehicle_id", "model_id", "owner"})),
    "CERTIFICATE_REGISTERED": (
        "part_certificate",
        frozenset({"cert_no", "part_type", "uses"}),
    ),
    "CERTIFICATE_SUSPENDED": (
        "part_certificate",
        frozenset({"cert_no", "suspended_uses", "effective_at", "remediation_due_at"}),
    ),
    "CERTIFICATE_REINSTATED": ("part_certificate", frozenset({"cert_no", "reinstated_at"})),
    "PART_SERIAL_REGISTERED": (
        "part_instance",
        frozenset({"serial", "cert_no", "batch_no"}),
    ),
    # 影响圈定与技师确认
    "FREEZE_SCOPE_DELINEATED": (
        "freeze_case",
        frozenset({"case_id", "cert_no", "suspended_uses", "vehicles"}),
    ),
    "MATCHING_RULES_CONFIRMED": (
        "freeze_case",
        frozenset({"case_id", "scope_hash", "vehicle_ids", "technician_id"}),
    ),
    # 合规处置
    "DISPOSITION_DECIDED": (
        "freeze_case",
        frozenset({"case_id", "items", "decided_by", "remediation_due_at"}),
    ),
    "PART_QUARANTINED": ("freeze_case", frozenset({"case_id", "serials"})),
    "PASS_RESTRICTION_IMPOSED": (
        "compliance_pass",
        frozenset({"pass_id", "case_id", "restriction", "effective_at"}),
    ),
    # 换件
    "PART_REPLACED": (
        "work_record",
        frozenset({"case_id", "vehicle_id", "old_serial", "new_serial", "replaced_by"}),
    ),
    # 恢复（另一人、依据新检测、可部分恢复）
    "PASS_RESTRICTION_LIFTED": (
        "compliance_pass",
        frozenset({"pass_id", "case_id", "restored_by", "basis_report_no"}),
    ),
    "RESTORATION_COMPLETED": (
        "freeze_case",
        frozenset({"case_id", "vehicle_ids", "restored_by"}),
    ),
    # 申诉：只暂缓行政结论，不解除安全限制
    "APPEAL_FILED": ("appeal", frozenset({"appeal_id", "case_id", "vehicle_ids", "filed_by"})),
    "APPEAL_DECIDED": ("appeal", frozenset({"appeal_id", "outcome", "decided_by"})),
    "ADMINISTRATIVE_FINALIZATION": (
        "freeze_case",
        frozenset({"case_id", "vehicle_id", "conclusion", "decided_by"}),
    ),
    # 所有权变化：通知送达新车主，责任时间线追加但不改写
    "OWNERSHIP_TRANSFERRED": (
        "vehicle_profile",
        frozenset({"vehicle_id", "from_owner", "to_owner", "transferred_at"}),
    ),
    "NOTICE_ISSUED": (
        "notice",
        frozenset({"notice_id", "case_id", "vehicle_id", "recipient_owner", "kind"}),
    ),
    # 进程暂停 / 恢复：期限锚定原截止点，不重新计时
    "PROCESS_PAUSED": ("freeze_case", frozenset({"case_id", "paused_at"})),
    "PROCESS_RESUMED": ("freeze_case", frozenset({"case_id", "resumed_at", "original_due_at"})),
    # 升级通知：按原截止点排程与补投
    "ESCALATION_SCHEDULED": (
        "escalation",
        frozenset({"case_id", "level", "due_at", "anchor_due_at"}),
    ),
    "ESCALATION_DELIVERED": (
        "escalation",
        frozenset({"case_id", "level", "due_at", "delivered_at"}),
    ),
    # 检测机构离线回传：同编号同内容只接受一次；内容变化形成独立争议
    "INSPECTION_RESULT_RECEIVED": (
        "inspection_report",
        frozenset({"report_no", "agency", "content_hash", "result", "received_at"}),
    ),
    "DISPUTE_OPENED": (
        "dispute",
        frozenset({"dispute_id", "report_no", "prior_content_hash", "new_content_hash", "opened_at"}),
    ),
    "DISPUTE_RESOLVED": (
        "dispute",
        frozenset({"dispute_id", "chosen_content_hash", "resolved_by"}),
    ),
}

EVENT_CATALOG = {**_BASELINE, **_FREEZE}

# 处置动作
DISPOSITION_SUSPEND_PASS = "SUSPEND_PASS"
DISPOSITION_REINSPECT = "REINSPECT"
DISPOSITION_REPLACE_PART = "REPLACE_PART"
DISPOSITION_QUARANTINE_PART = "QUARANTINE_PART"

DISPOSITIONS = frozenset(
    {
        DISPOSITION_SUSPEND_PASS,
        DISPOSITION_REINSPECT,
        DISPOSITION_REPLACE_PART,
        DISPOSITION_QUARANTINE_PART,
    }
)

# 圈定时车辆相对涉案部件所处的状态
STATE_NOT_INSTALLED = "NOT_INSTALLED"        # 已绑定/配发但未施工
STATE_AWAITING_INSPECTION = "AWAITING_INSPECTION"  # 已施工，待检测
STATE_PASS_HELD = "PASS_HELD"                # 已取得通行证
STATE_ALREADY_RESOLD = "ALREADY_RESOLD"      # 已经转卖（叠加标记，不影响安全处置）
