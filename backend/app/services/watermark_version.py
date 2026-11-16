"""水位线配置版本数据：校验、落库、同步消费与单一快照读取。

这里把「水位线配置版本」当作唯一可信源：

* 发布版本时必须带上 `expected_version`，与当前生效版本不一致就拒绝，
  因此并发写入只会有一个配置版本被接受；
* 版本行完整保留生效版本、空闲超时和延迟容忍，窗口重算只从这里读取；
* 幂等键（未显式传入时按配置 + 基础版本 + 载荷摘要生成）保证重复提交
  只留下一条记录；
* 所有校验都在写入前完成，写入过程中任何异常都会回滚配置行与版本行，
  上一版完整配置不受影响。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

WATERMARK_MODULE = "watermark"
WINDOW_MODULE = "window"
VERSION_MODULE = "watermark_version"
CONSUMER_MODULE = "watermark_consume"

# 版本的业务字段：生效版本、空闲超时与延迟容忍必须成组保留，不能跨版本拼装。
SNAPSHOT_FIELDS = ["时间字段", "延迟容忍", "空闲超时", "发射间隔", "对齐策略"]
VERSION_FIELDS = ["配置编号", "流名称", "生效版本", "版本号", *SNAPSHOT_FIELDS]
CONFIG_POINTER_FIELDS = [*SNAPSHOT_FIELDS, "生效版本", "版本号"]

# 版本写入与窗口重算读取共用一把锁，避免「读取版本」和「写入新版本」之间被插队。
CONFIG_LOCK = threading.RLock()

# 另一个业务入口：与版本写入同步接收同一份快照，消费侧维护自己的处理记录。
SUBSCRIBERS = ["延迟追踪"]

DURATION_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(毫秒|ms|秒|s|分钟|min|m|小时|h)?\s*$",
    re.IGNORECASE,
)
DURATION_FACTORS = {
    "毫秒": 0.001,
    "ms": 0.001,
    "秒": 1.0,
    "s": 1.0,
    "分钟": 60.0,
    "min": 60.0,
    "m": 60.0,
    "小时": 3600.0,
    "h": 3600.0,
}

# 可核对的数据链路，和 docs/watermark-config-version-flow.md 中的流程图一一对应。
FLOW_STEPS: list[dict[str, Any]] = [
    {
        "step": 1,
        "环节": "读取水位线配置",
        "数据源": WATERMARK_MODULE,
        "入口": "GET /api/watermark/{id}",
        "说明": "按配置编号读取 WATE 水位线配置，详情与发布共用同一条配置。",
    },
    {
        "step": 2,
        "环节": "读取窗口定义延迟参数",
        "数据源": WINDOW_MODULE,
        "入口": "GET /api/window?keyword=WIND",
        "说明": "按数据流名称读取 WIND 窗口定义的延迟容忍，校验水位线是否覆盖窗口延迟。",
    },
    {
        "step": 3,
        "环节": "校验配置版本",
        "数据源": VERSION_MODULE,
        "入口": "POST /api/watermark/{id}/versions",
        "说明": "expected_version 必须等于当前生效版本；幂等键命中时直接返回已有记录。",
    },
    {
        "step": 4,
        "环节": "写入水位线配置版本数据",
        "数据源": VERSION_MODULE,
        "入口": "POST /api/watermark/{id}/versions",
        "说明": "落库生效版本、空闲超时、延迟容忍以及时间字段等完整快照。",
    },
    {
        "step": 5,
        "环节": "同步消费侧处理记录",
        "数据源": CONSUMER_MODULE,
        "入口": "GET /api/watermark/consumers",
        "说明": "另一业务入口同步接收同一份版本数据，按版本ID去重并维护处理结果。",
    },
    {
        "step": 6,
        "环节": "窗口重算读取已落库快照",
        "数据源": "window_recalc",
        "入口": "POST /api/window/{id}/recalculations",
        "说明": "重算只定位一条生效版本行，空闲超时与延迟容忍取自同一行，不读详情缓存。",
    },
]
FLOW_EDGES = [[1, 2], [2, 3], [3, 4], [4, 5], [4, 6]]
FLOW_TABLES = [
    WATERMARK_MODULE,
    WINDOW_MODULE,
    VERSION_MODULE,
    CONSUMER_MODULE,
    "window_recalc",
]
FLOW_INVARIANTS = [
    "并发写入只认一个配置版本：expected_version 与当前生效版本不一致即拒绝。",
    "窗口重算只读取已落库的 watermark_version 快照，不读取详情缓存或窗口定义中的延迟参数。",
    "窗口重算关联处理只采用一条已定位的版本数据，空闲超时和延迟容忍来自同一行。",
    "重复提交按幂等键返回已有记录，不新增版本记录或重算记录。",
    "写入失败时回滚版本行和配置行，保留上一版完整配置。",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration_seconds(value: Any) -> float | None:
    """把「10秒」「500毫秒」「10s」这类配置解析为秒；无法识别时返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value >= 0 else None
    text = str(value).strip()
    if not text or text in {"—", "-", "无"}:
        return None
    match = DURATION_PATTERN.match(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "秒").lower()
    return amount * DURATION_FACTORS.get(unit, 1.0)


def payload_digest(config_no: str, values: dict[str, Any]) -> str:
    filtered = {
        key: value
        for key, value in values.items()
        if key not in {"expected_version", "idempotency_key"}
    }
    raw = json.dumps(
        {"配置编号": config_no, "values": filtered},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_version(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "0", "v0"}:
        return "v0"
    return text


class WatermarkVersionService:
    """水位线配置版本数据服务。"""

    # ------------------------------------------------------------------ 查询
    def versions_for(self, config_no: str) -> list[dict[str, Any]]:
        rows = [
            row
            for row in store.rows(VERSION_MODULE)
            if str(row.get("配置编号")) == str(config_no)
        ]
        return sorted(rows, key=lambda row: int(row.get("版本号", 0)), reverse=True)

    def effective_version(self, config_no: str) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.versions_for(config_no)
            if row.get("status") == "生效" and not row.get("pending")
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("版本号", 0)))

    def locate_version(
        self,
        config_no: str,
        *,
        version_id: Any = None,
        version_label: Any = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """定位唯一一条版本记录；不按字段回退，也不跨版本拼装。"""
        versions = self.versions_for(config_no)
        if version_id not in {None, ""}:
            wanted = str(version_id).strip()
            matches = [
                row
                for row in versions
                if str(row.get("版本ID")) == wanted or str(row.get("id")) == wanted
            ]
        elif version_label not in {None, ""}:
            wanted_label = _normalize_version(version_label)
            matches = [
                row
                for row in versions
                if _normalize_version(row.get("生效版本")) == wanted_label
            ]
        else:
            effective = self.effective_version(config_no)
            if effective is None:
                return None, f"水位线配置 {config_no} 还没有已落库的生效版本"
            matches = [effective]
        if not matches:
            return None, f"水位线配置 {config_no} 下找不到指定的配置版本"
        if len(matches) > 1:
            labels = "、".join(str(row.get("生效版本")) for row in matches)
            return None, f"水位线配置 {config_no} 存在多条候选版本（{labels}），无法唯一定位"
        return matches[0], ""

    def list_versions(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(VERSION_MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if version:
            rows = [row for row in rows if str(row.get("生效版本")) == str(version)]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def consumers_for(
        self,
        *,
        config_no: str | None = None,
        version_id: str | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(CONSUMER_MODULE))
        if config_no:
            rows = [row for row in rows if str(row.get("配置编号")) == str(config_no)]
        if version_id:
            rows = [row for row in rows if str(row.get("水位线版本ID")) == str(version_id)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        return rows

    # ------------------------------------------------------------------ 详情
    @staticmethod
    def _apply_snapshot(entry: dict[str, Any], version: dict[str, Any]) -> None:
        """用版本快照回填配置行，详情、列表与重算统一读同一份数据。"""
        for field in CONFIG_POINTER_FIELDS:
            if version.get(field) is not None:
                entry[field] = version[field]

    def sync_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        current = self.effective_version(str(entry.get("配置编号", "")))
        if current is not None:
            self._apply_snapshot(entry, current)
        return current

    def detail(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(WATERMARK_MODULE, entry_id)
        if entry is None:
            return None
        with CONFIG_LOCK:
            current = self.sync_entry(entry)
            versions = self.versions_for(str(entry.get("配置编号", "")))
            consumers = self.consumers_for(config_no=str(entry.get("配置编号", "")))
        payload = copy.deepcopy(entry)
        payload["当前生效版本"] = copy.deepcopy(current) if current else None
        payload["版本列表"] = copy.deepcopy(versions)
        payload["消费记录"] = copy.deepcopy(consumers)
        mismatched = (
            []
            if current is None
            else [
                field
                for field in ["时间字段", "延迟容忍", "空闲超时", "生效版本"]
                if str(payload.get(field)) != str(current.get(field))
            ]
        )
        payload["版本一致性"] = {
            "ok": current is not None and not mismatched,
            "生效版本": current.get("生效版本") if current else None,
            "水位线版本ID": current.get("版本ID") if current else None,
            "不一致字段": mismatched,
            "说明": "详情与窗口重算都读取 watermark_version 中同一条生效版本快照",
        }
        return payload

    def flow(self) -> dict[str, Any]:
        """返回可核对的数据链路与流程图，便于页面/脚本逐环节比对。"""
        return {
            "module": VERSION_MODULE,
            "steps": copy.deepcopy(FLOW_STEPS),
            "edges": copy.deepcopy(FLOW_EDGES),
            "tables": list(FLOW_TABLES),
            "invariants": list(FLOW_INVARIANTS),
            "mermaid": (
                "flowchart TD\n"
                "  A[WATE 水位线配置] --> C{校验配置版本}\n"
                "  B[WIND 窗口定义延迟参数] --> C\n"
                "  C -- expected_version 一致 --> D[写入 watermark_version<br/>生效版本/空闲超时/延迟容忍]\n"
                "  C -- 冲突或校验失败 --> G[拒绝写入<br/>保留上一版完整配置]\n"
                "  D --> E[同步 watermark_consume<br/>消费侧处理记录]\n"
                "  D --> F[窗口重算 window_recalc<br/>只读已落库快照]\n"
                "  E --> H[详情重开与重算看到同一生效版本]\n"
                "  F --> H"
            ),
        }

    # ------------------------------------------------------------------ 发布
    def publish(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        expected_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """校验配置版本后写入版本数据；失败时不改动上一版完整配置。"""
        with CONFIG_LOCK:
            entry = store.find(WATERMARK_MODULE, entry_id)
            if entry is None:
                return None, f"水位线配置 {entry_id} 不存在或已归档", None
            if entry.get("配置状态") == "已关闭" or entry.get("status") == "已关闭":
                return None, "水位线配置已关闭，不能写入新的配置版本", None

            config_no = str(entry.get("配置编号", ""))
            current = self.effective_version(config_no)
            current_label = _normalize_version(current.get("生效版本")) if current else "v0"

            raw_expected = (
                expected_version
                if expected_version is not None
                else values.get("expected_version")
            )
            if raw_expected is None or not str(raw_expected).strip():
                return (
                    None,
                    "缺少配置版本校验字段 expected_version，请先读取详情再提交",
                    None,
                )
            expected = _normalize_version(raw_expected)
            # 幂等优先：同一个提交（显式键或派生键）只保留一份版本记录。
            digest = payload_digest(config_no, values)
            key = str(idempotency_key or values.get("idempotency_key") or "").strip()
            effective_key = key or f"{config_no}:{expected}:{digest}"
            for row in store.rows(VERSION_MODULE):
                if str(row.get("幂等键")) != effective_key:
                    continue
                if str(row.get("配置编号")) != config_no or row.get("载荷摘要") != digest:
                    return (
                        None,
                        f"幂等键 {effective_key} 已被其它配置版本占用，拒绝重复写入",
                        None,
                    )
                self.sync_entry(entry)
                return (
                    copy.deepcopy(row),
                    f"重复提交，已幂等返回版本 {row.get('生效版本')}（{row.get('版本ID')}）",
                    {
                        "config": copy.deepcopy(entry),
                        "consumer_records": copy.deepcopy(
                            self.consumers_for(version_id=str(row.get("版本ID")))
                        ),
                        "previous_version": row.get("基础版本"),
                    },
                )

            if expected != current_label:
                return (
                    None,
                    f"版本冲突：当前生效版本为 {current_label}，提交基于 {expected}，"
                    "请重新读取配置详情后再提交",
                    None,
                )

            candidate = {field: entry.get(field) for field in SNAPSHOT_FIELDS}
            for field in SNAPSHOT_FIELDS:
                if field in values:
                    candidate[field] = values[field]
            if values.get("流名称") not in {None, ""} and str(values["流名称"]) != str(
                entry.get("流名称")
            ):
                return None, "提交的流名称与水位线配置不一致，拒绝跨流写入版本", None

            time_field = str(candidate.get("时间字段") or "").strip()
            if not time_field:
                return None, "缺少时间字段，无法写入水位线配置版本", None
            candidate["时间字段"] = time_field

            tolerance_seconds = parse_duration_seconds(candidate.get("延迟容忍"))
            idle_seconds = parse_duration_seconds(candidate.get("空闲超时"))
            if tolerance_seconds is None or tolerance_seconds <= 0:
                return None, f"延迟容忍「{candidate.get('延迟容忍')}」无法解析为正的时长", None
            if idle_seconds is None or idle_seconds <= 0:
                return None, f"空闲超时「{candidate.get('空闲超时')}」无法解析为正的时长", None

            window_tolerances = self._window_tolerances(str(entry.get("流名称", "")))
            blocked = [
                item
                for item in window_tolerances
                if item["seconds"] is not None and item["seconds"] > tolerance_seconds
            ]
            if blocked:
                detail_text = "、".join(
                    f"{item['窗口编号']}({item['raw']})" for item in blocked
                )
                return (
                    None,
                    f"延迟容忍 {candidate.get('延迟容忍')} 小于窗口定义的延迟容忍：{detail_text}",
                    None,
                )

            versions = store.rows(VERSION_MODULE)
            next_number = max(
                (
                    int(row.get("版本号", 0))
                    for row in versions
                    if str(row.get("配置编号")) == config_no
                ),
                default=0,
            ) + 1
            next_id = max((int(row.get("id", 0)) for row in versions), default=0) + 1
            version = {
                "id": next_id,
                "status": "生效",
                "pending": False,
                "abnormal": False,
                "版本ID": f"WV-{next_id:04d}",
                "配置编号": config_no,
                "流名称": entry.get("流名称"),
                "生效版本": f"v{next_number}",
                "版本号": next_number,
                "时间字段": candidate["时间字段"],
                "延迟容忍": candidate.get("延迟容忍"),
                "空闲超时": candidate.get("空闲超时"),
                "发射间隔": candidate.get("发射间隔"),
                "对齐策略": candidate.get("对齐策略"),
                "校验结果": "通过",
                "窗口延迟容忍": self._window_tolerance_text(window_tolerances),
                "基础版本": current_label,
                "幂等键": effective_key,
                "载荷摘要": digest,
                "写入时间": now_iso(),
            }

            consumers = store.rows(CONSUMER_MODULE)
            config_snapshot = copy.deepcopy(entry)
            previous_statuses = {
                int(row.get("id", 0)): row.get("status")
                for row in versions
                if str(row.get("配置编号")) == config_no
            }
            consume_before = len(consumers)
            versions_before = len(versions)
            try:
                for row in versions:
                    if (
                        str(row.get("配置编号")) == config_no
                        and row.get("status") == "生效"
                    ):
                        row["status"] = "已被取代"
                versions.append(version)
                for subscriber in SUBSCRIBERS:
                    consumers.append(
                        {
                            "id": max(
                                (int(item.get("id", 0)) for item in consumers),
                                default=0,
                            )
                            + 1,
                            "status": "已消费",
                            "pending": False,
                            "abnormal": False,
                            "消费编号": f"WCON-{max((int(item.get('id', 0)) for item in consumers), default=0) + 1:04d}",
                            "消费方": subscriber,
                            "配置编号": config_no,
                            "流名称": entry.get("流名称"),
                            "水位线版本ID": version["版本ID"],
                            "生效版本": version["生效版本"],
                            "时间字段": version["时间字段"],
                            "延迟容忍": version["延迟容忍"],
                            "空闲超时": version["空闲超时"],
                            "幂等键": f"{subscriber}:{version['版本ID']}",
                            "处理结果": "已接收",
                            "消费时间": now_iso(),
                        }
                    )
                self._apply_snapshot(entry, version)
            except Exception as error:  # pragma: no cover - 防御性回滚
                del versions[versions_before:]
                del consumers[consume_before:]
                for row in versions:
                    if int(row.get("id", 0)) in previous_statuses:
                        row["status"] = previous_statuses[int(row.get("id", 0))]
                entry.clear()
                entry.update(config_snapshot)
                return None, f"写入水位线配置版本失败，已保留上一版完整配置：{error}", None

            return (
                copy.deepcopy(version),
                f"水位线配置 {config_no} 已写入版本 {version['生效版本']}"
                f"（{version['版本ID']}），详情与窗口重算共用该快照",
                {
                    "config": copy.deepcopy(entry),
                    "consumer_records": copy.deepcopy(
                        consumers[-len(SUBSCRIBERS):] if SUBSCRIBERS else []
                    ),
                    "previous_version": current_label,
                },
            )

    @staticmethod
    def _window_tolerances(stream_name: str) -> list[dict[str, Any]]:
        windows = [
            row
            for row in store.rows(WINDOW_MODULE)
            if str(row.get("数据流名称")) == str(stream_name)
            and row.get("窗口状态") != "已关闭"
        ]
        result: list[dict[str, Any]] = []
        for row in windows:
            raw = row.get("延迟容忍")
            result.append(
                {
                    "窗口编号": row.get("窗口编号"),
                    "raw": raw,
                    "seconds": parse_duration_seconds(raw),
                }
            )
        return result

    @staticmethod
    def _window_tolerance_text(items: list[dict[str, Any]]) -> str:
        if not items:
            return "无同流窗口定义"
        values = []
        for item in items:
            raw = str(item["raw"])
            if raw not in values:
                values.append(raw)
        return "、".join(values)


watermark_versions = WatermarkVersionService()
