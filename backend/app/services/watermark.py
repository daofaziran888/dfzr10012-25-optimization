"""水位线管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.watermark_version import CONFIG_LOCK, watermark_versions
from app.store import store

MODULE = "watermark"
REQUIRED_FIELDS = ["配置编号", "流名称", "时间字段"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
# 动作 -> (要求的前置状态, 目标状态)；状态只能沿 草稿→已启用→已暂停→已关闭 前进。
ACTION_RULES = {
    "启用水位线": ("草稿", "已启用"),
    "暂停水位线": ("已启用", "已暂停"),
    "关闭水位线": ("已暂停", "已关闭"),
}
NEGATIVE_ACTIONS = []


class WatermarkService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        # 列表只做展示，但读到的仍然是版本快照，避免详情重开后看到旧字段。
        with CONFIG_LOCK:
            for row in rows:
                watermark_versions.sync_entry(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_detail(self, entry_id: int) -> dict[str, Any] | None:
        return watermark_versions.detail(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["配置状态"] = STATUS_ORDER[0]
        entry["生效版本"] = None
        entry["版本号"] = 0
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"水位线配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于水位线管理可执行范围"
        required, target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or STATUS_ORDER[0])
        if current == target:
            # 重复执行同一个动作不再改字段，保持幂等。
            entry["配置状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            return entry, f"水位线配置已是{target}，重复动作未改动字段"
        if current != required:
            return None, (
                f"水位线配置当前为 {current}，{action}要求先处于 {required}，"
                "请按 草稿→已启用→已暂停→已关闭 的顺序操作"
            )
        entry["status"] = target
        entry["配置状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"水位线配置已{action}"
