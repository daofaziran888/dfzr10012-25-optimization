"""水位线管理接口：维护水位线配置，覆盖启用水位线、暂停水位线、关闭水位线等动作。

除常规 CRUD 外，这里暴露水位线配置版本的写入、查询、消费记录与链路核对接口：
版本写入是详情与窗口重算共用的唯一快照来源。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.watermark import WatermarkService
from app.services.watermark_version import watermark_versions

router = APIRouter(prefix="/api/watermark", tags=["水位线管理"])

service = WatermarkService()

LIST_FIELDS = ["配置编号", "流名称", "时间字段", "延迟容忍", "空闲超时", "发射间隔", "对齐策略", "配置状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤水位线管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出水位线管理清单：返回当前过滤条件下的全量数据。

    必须声明在 /{entry_id} 之前，否则 "export" 会被当成 entry_id 解析成 422。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "watermark", "total": total, "items": items}


@router.get("/flow")
def config_version_flow() -> dict[str, Any]:
    """水位线配置版本数据链路：环节、边、涉及表与不变量，可逐项核对。"""
    return watermark_versions.flow()


@router.get("/consumers", response_model=PageResult[dict])
def list_consumer_records(
    config_no: str | None = Query(default=None, description="按配置编号过滤"),
    version_id: str | None = Query(default=None, description="按水位线版本ID过滤"),
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """另一业务入口的消费侧处理记录；与版本写入同步接收同一份快照。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = watermark_versions.consumers_for(
        config_no=config_no, version_id=version_id, keyword=keyword
    )
    total = len(rows)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=total, page=page, size=size)


@router.get("/versions", response_model=PageResult[dict])
def list_versions(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="生效、已被取代"),
    version: str | None = Query(default=None, description="生效版本，如 v1、v2"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """水位线配置版本数据列表：保留生效版本、空闲超时和延迟容忍。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = watermark_versions.list_versions(
        keyword=keyword, status=status, version=version, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条水位线配置明细；明细带出当前生效版本，重开详情与重算看到同一快照。"""
    entry = service.get_detail(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"水位线配置 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions", response_model=dict)
def get_entry_versions(entry_id: int) -> dict[str, Any]:
    """单条水位线配置的全部版本快照，按版本号倒序。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"水位线配置 {entry_id} 不存在或已归档")
    config_no = str(entry.get("配置编号"))
    versions = watermark_versions.versions_for(config_no)
    effective = watermark_versions.effective_version(config_no)
    return {
        "配置编号": config_no,
        "生效版本": effective.get("生效版本") if effective else None,
        "version": effective.get("生效版本") if effective else None,
        "版本列表": versions,
    }


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条水位线配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="水位线配置已登记", entry=entry)


@router.post("/{entry_id}/versions", response_model=ActionResult)
def publish_version(entry_id: int, payload: EntryPayload) -> ActionResult:
    """校验配置版本后写入水位线配置版本数据。

    * expected_version 必须与当前生效版本一致，并发提交只会有一个被接受；
    * 版本行保留生效版本、空闲超时和延迟容忍，并同步写消费侧处理记录；
    * 同一个幂等键重复提交只返回已有记录，不再新增版本；
    * 任何校验/写入失败都不会改动上一版完整配置。
    """
    values = dict(payload.values)
    version, message, extra = watermark_versions.publish(
        entry_id,
        values,
        expected_version=values.get("expected_version"),
        idempotency_key=values.get("idempotency_key") or payload.remark,
    )
    if version is None:
        return ActionResult(ok=False, message=message)
    entry = dict((extra or {}).get("config") or {})
    entry["水位线配置版本"] = version
    entry["消费记录"] = (extra or {}).get("consumer_records", [])
    entry["上一版"] = (extra or {}).get("previous_version")
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条水位线配置执行启用水位线、暂停水位线、关闭水位线；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
