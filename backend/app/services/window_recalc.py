"""窗口重算：只读取已落库的水位线配置版本快照。

重算不读水位线详情缓存，也不从窗口定义里取延迟参数；一次重算关联处理只定位
一条 watermark_version 记录，空闲超时与延迟容忍都取自同一行。
"""
from __future__ import annotations

import copy
from typing import Any

from app.services.watermark_version import (
    CONFIG_LOCK,
    VERSION_MODULE,
    now_iso,
    parse_duration_seconds,
    watermark_versions,
)
from app.store import store

MODULE = "window"
RECALC_MODULE = "window_recalc"


class RecalcConflict(Exception):
    """请求里的版本指向与已落库快照冲突，拒绝混用字段。"""


class WindowRecalcService:
    def _window(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    @staticmethod
    def _window_idempotency_key(
        window: dict[str, Any],
        body: dict[str, Any],
        snapshot_id: str,
    ) -> str:
        """幂等键按「窗口 + 已定位的版本快照 + 重算区间」生成。

        请求里带不带 version/version_id 都会归一到同一条快照，因此重复提交
        无论是显式指定版本还是走默认生效版本，都只会留下一条重算记录。
        """
        supplied = str(body.get("idempotency_key") or "").strip()
        if supplied:
            return supplied
        return (
            f"{window.get('窗口编号')}:{snapshot_id}:"
            f"{body.get('window_start') or ''}:{body.get('window_end') or ''}"
        )

    def list_recalculations(
        self,
        *,
        window_id: int | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(RECALC_MODULE))
        if window_id is not None:
            window = self._window(window_id)
            if window is not None:
                rows = [
                    row
                    for row in rows
                    if str(row.get("窗口编号")) == str(window.get("窗口编号"))
                ]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("窗口编号", ""))
                or keyword in str(row.get("配置编号", ""))
            ]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def recalculate(
        self,
        entry_id: int,
        body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        body = body or {}
        with CONFIG_LOCK:
            window = self._window(entry_id)
            if window is None:
                return None, f"窗口定义 {entry_id} 不存在或已归档"
            if window.get("status") in {"已关闭"}:
                return None, "窗口定义已关闭，不能发起重算"

            stream_name = str(window.get("数据流名称") or "")
            candidates = sorted(
                {
                    str(row.get("配置编号"))
                    for row in store.rows(VERSION_MODULE)
                    if str(row.get("流名称")) == stream_name
                }
            )
            requested_config = str(body.get("config_no") or "").strip()
            if requested_config:
                if requested_config not in candidates:
                    return (
                        None,
                        f"配置 {requested_config} 与窗口 {window.get('窗口编号')} 的数据流"
                        f"「{stream_name}」不匹配",
                    )
                config_no = requested_config
            elif len(candidates) == 1:
                config_no = candidates[0]
            elif not candidates:
                return (
                    None,
                    f"数据流「{stream_name}」还没有已落库的水位线配置版本，"
                    "请先发布 WATE 水位线配置版本",
                )
            else:
                joined = "、".join(candidates)
                return (
                    None,
                    f"数据流「{stream_name}」对应多条水位线配置（{joined}），"
                    "请在请求中指定 config_no 以唯一定位",
                )

            version, message = watermark_versions.locate_version(
                config_no,
                version_id=body.get("version_id"),
                version_label=body.get("version"),
            )
            if version is None:
                return None, message

            if version.get("status") != "生效":
                return (
                    None,
                    f"水位线版本 {version.get('生效版本')}（{version.get('版本ID')}）已被取代，"
                    f"窗口重算必须使用已落库的生效版本，不能回退到旧快照",
                )

            # 版本指针一旦给出，就必须和定位到的快照完全一致，绝不退回其它版本取字段。
            if body.get("version") not in {None, ""}:
                requested_label = str(body.get("version")).strip().lower()
                if requested_label != str(version.get("生效版本")).lower():
                    raise RecalcConflict(
                        f"窗口重算请求的水位线版本 {body.get('version')} 与已落库的生效版本 "
                        f"{version.get('生效版本')} 不一致，拒绝混用空闲超时和延迟容忍"
                    )
            if body.get("version_id") not in {None, ""}:
                requested_id = str(body.get("version_id")).strip()
                if requested_id not in {
                    str(version.get("版本ID")),
                    str(version.get("id")),
                }:
                    raise RecalcConflict(
                        f"窗口重算请求的水位线版本ID {requested_id} 与已落库快照 "
                        f"{version.get('版本ID')} 不一致"
                    )

            snapshot_id = str(version.get("版本ID"))
            snapshot_label = str(version.get("生效版本"))
            version_row_id = int(version.get("id", 0))

            recalc_rows = store.rows(RECALC_MODULE)
            effective_key = self._window_idempotency_key(window, body, snapshot_id)
            for row in recalc_rows:
                if str(row.get("幂等键")) != effective_key:
                    continue
                if str(row.get("水位线版本ID")) != snapshot_id:
                    raise RecalcConflict(
                        f"幂等键 {effective_key} 已绑定水位线版本 {row.get('水位线版本ID')}，"
                        f"本次请求指向 {snapshot_id}，拒绝覆盖"
                    )
                return (
                    copy.deepcopy(row),
                    f"重复提交，已幂等返回重算记录 {row.get('重算编号')}"
                    f"（水位线版本 {row.get('生效版本')}）",
                )

            tolerance_seconds = parse_duration_seconds(version.get("延迟容忍"))
            idle_seconds = parse_duration_seconds(version.get("空闲超时"))
            if tolerance_seconds is None or idle_seconds is None:
                return (
                    None,
                    f"水位线版本 {snapshot_id} 缺少可用的延迟容忍或空闲超时，无法重算",
                )

            next_id = max((int(row.get("id", 0)) for row in recalc_rows), default=0) + 1
            record = {
                "id": next_id,
                "status": "已完成",
                "pending": False,
                "abnormal": False,
                "重算编号": f"WREC-{next_id:04d}",
                "窗口编号": window.get("窗口编号"),
                "数据流名称": stream_name,
                "窗口类型": window.get("窗口类型"),
                "窗口长度": window.get("窗口长度"),
                "滑动步长": window.get("滑动步长"),
                "窗口延迟容忍": window.get("延迟容忍"),
                "配置编号": config_no,
                "水位线版本ID": snapshot_id,
                "水位线版本行": version_row_id,
                "生效版本": snapshot_label,
                "时间字段": version.get("时间字段"),
                "延迟容忍": version.get("延迟容忍"),
                "空闲超时": version.get("空闲超时"),
                "延迟容忍秒": tolerance_seconds,
                "空闲超时秒": idle_seconds,
                "触发策略": window.get("触发策略"),
                "重算窗口起始": body.get("window_start"),
                "重算窗口结束": body.get("window_end"),
                "快照来源": f"{VERSION_MODULE}#{snapshot_id}",
                "幂等键": effective_key,
                "执行结果": "重算完成",
                "重算时间": now_iso(),
            }
            recalc_rows.append(record)
            latest = max(
                (
                    row
                    for row in recalc_rows
                    if str(row.get("窗口编号")) == str(window.get("窗口编号"))
                ),
                key=lambda row: int(row.get("id", 0)),
            )
            window["最近重算"] = latest.get("重算编号")
            window["重算水位线版本"] = snapshot_label
            return (
                copy.deepcopy(record),
                f"窗口 {window.get('窗口编号')} 已按水位线版本 {snapshot_label}"
                f"（延迟容忍 {record['延迟容忍']}、空闲超时 {record['空闲超时']}）完成重算",
            )


window_recalc = WindowRecalcService()
