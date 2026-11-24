"""交接包服务：批次打包、清单校验与下游交接。

把已确认批次连同字段映射、校验摘要和清单打包成交接包，
供下游数仓直接消费并回溯来源。退回后批次不得再次交接。

v1 交接包只携带「清单文件数 + 校验摘要」汇总字段；v2 逐文件清单由
``app.manifest`` 内核与 ``handoff_migration`` 服务承载。本服务始终保留
v1 旧字段查询能力，迁移与回滚都不破坏接收端的旧核对路径。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService

# v1 交接包的汇总字段：迁移期间接收端仍依赖这些旧字段核对
LEGACY_V1_FIELDS = ["交接版本", "清单文件数", "校验摘要", "记录行数"]


class HandoffPackService(BaseModuleService):
    MODULE = "handoff_pack"
    ENTITY = "交接包"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "交接编号", "关联批次", "目标系统", "清单文件数", "记录行数",
        "校验摘要", "交接版本", "交接时间",
    ]
    REQUIRED_FIELDS = ["交接编号", "关联批次", "目标系统"]
    STATUS_ORDER = ["待打包", "打包中", "已交接", "交接退回"]
    ACTION_RULES = {"启动打包": "打包中", "确认交接": "已交接", "记录退回": "交接退回"}
    NEGATIVE_ACTIONS = []

    def reject_handoff(self, pack_id: int, reason: str) -> dict[str, Any]:
        """驳回交接包，退回后批次不得进入交接。"""
        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        self.run_action(pack_id, "记录退回")
        return {"ok": True, "pack_id": pack_id, "reason": reason, "action": "交接退回"}

    def get_delivered_packs(self, target_system: str | None = None) -> list[dict[str, Any]]:
        """获取已交接的包列表，可按目标系统筛选。"""
        items, _ = self.list_entries(size=10000)
        delivered = [it for it in items if it.get("status") == "已交接"]
        if target_system:
            delivered = [it for it in delivered if it.get("目标系统") == target_system]
        return delivered

    def get_legacy_summary(self, pack_id: int) -> dict[str, Any] | None:
        """复核 v1 旧包摘要：旧字段始终可查，迁移/回滚都不改写原值。"""
        entry = self.get_entry(pack_id)
        if entry is None:
            return None
        return {field: entry.get(field, "") for field in LEGACY_V1_FIELDS}

    def receiver_reconcile_view(self, pack_id: int) -> dict[str, Any]:
        """接收端核对视图：v2 已发布读逐文件结果，否则回落 v1 旧摘要。

        延迟导入迁移服务，避免服务注册阶段的循环依赖。
        """
        from app.services.handoff_migration import migration_service

        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        view = migration_service.receiver_view(pack_id)
        view["ok"] = True
        view["pack_status"] = entry.get("status", "")
        return view