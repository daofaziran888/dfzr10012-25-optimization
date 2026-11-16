"""窗口计算配置业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "window"
REQUIRED_FIELDS = ["窗口编号", "数据流名称", "窗口类型"]
STATUS_ORDER = ["草稿", "生效中", "已暂停", "已关闭"]
# 动作 -> (要求的前置状态, 目标状态)；状态只能沿 草稿→生效中→已暂停→已关闭 前进。
ACTION_RULES = {
    "启用窗口": ("草稿", "生效中"),
    "暂停窗口": ("生效中", "已暂停"),
    "关闭窗口": ("已暂停", "已关闭"),
}
NEGATIVE_ACTIONS = []


class WindowService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("窗口编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

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
        entry["窗口状态"] = STATUS_ORDER[0]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"窗口定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于窗口计算配置可执行范围"
        required, target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or STATUS_ORDER[0])
        if current == target:
            entry["窗口状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            return entry, f"窗口定义已是{target}，重复动作未改动字段"
        if current != required:
            return None, (
                f"窗口定义当前为 {current}，{action}要求先处于 {required}，"
                "请按 草稿→生效中→已暂停→已关闭 的顺序操作"
            )
        entry["status"] = target
        entry["窗口状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"窗口定义已{action}"
