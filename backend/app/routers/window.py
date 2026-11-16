"""窗口计算配置接口：维护窗口定义，覆盖启用窗口、暂停窗口、关闭窗口等动作。

重算相关接口只读取 watermark_version 中已落库的版本快照，不读窗口定义里的
延迟容忍，避免和详情里的水位线版本混用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.watermark_version import watermark_versions
from app.services.window import WindowService
from app.services.window_recalc import RecalcConflict, window_recalc

router = APIRouter(prefix="/api/window", tags=["窗口计算配置"])

service = WindowService()

LIST_FIELDS = ["窗口编号", "数据流名称", "窗口类型", "窗口长度", "滑动步长", "延迟容忍", "触发策略", "窗口状态"]
STATUSES = ["草稿", "生效中", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按窗口编号检索"),
    status: str | None = Query(default=None, description="草稿、生效中、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按窗口编号与状态过滤窗口计算配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出窗口计算配置清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "window", "total": total, "items": items}


@router.get("/recalculations", response_model=PageResult[dict])
def list_recalculations(
    window_id: int | None = Query(default=None, description="按窗口记录ID过滤"),
    keyword: str | None = Query(default=None, description="按窗口编号或配置编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """窗口重算记录：每条记录只关联一条水位线配置版本行。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = window_recalc.list_recalculations(
        window_id=window_id, keyword=keyword, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条窗口定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"窗口定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条窗口定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="窗口定义已登记", entry=entry)


@router.post("/{entry_id}/recalculations", response_model=ActionResult)
def recalculate(entry_id: int, payload: EntryPayload) -> ActionResult:
    """按已落库的水位线配置版本重算窗口。

    允许的 values：config_no（同流存在多条水位线配置时必填）、version（期望的生效版本，
    如 v2）、version_id（版本ID）、window_start/window_end、idempotency_key。
    版本指向与已落库快照不一致时返回 ok=false，不会退回其它版本取字段。
    """
    try:
        record, message = window_recalc.recalculate(entry_id, dict(payload.values))
    except RecalcConflict as conflict:
        return ActionResult(ok=False, message=str(conflict))
    if record is None:
        return ActionResult(ok=False, message=message)
    version, _ = watermark_versions.locate_version(
        str(record.get("配置编号")),
        version_id=record.get("水位线版本ID"),
    )
    entry = {
        **record,
        "水位线配置版本": version,
    }
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条窗口定义执行启用窗口、暂停窗口、关闭窗口；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
