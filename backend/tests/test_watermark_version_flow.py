"""水位线配置版本数据的行为测试：并发写入、幂等、快照一致性与回滚。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.services.watermark_version import watermark_versions
from app.store import store


class WatermarkVersionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例重建一次数据仓库，避免相互污染。
        store._tables = {
            name: [dict(row) for row in rows]
            for name, rows in __import__("app.seed", fromlist=["SEED_ROWS"]).SEED_ROWS.items()
        }
        self.client = TestClient(app)

    # ---------------------------------------------------------------- 样例验收
    def test_wate0002_detail_and_recalc_share_v2(self) -> None:
        detail = self.client.get("/api/watermark/2").json()
        self.assertEqual(detail["生效版本"], "v2")
        self.assertEqual(detail["时间字段"], "event_time")
        self.assertEqual(detail["延迟容忍"], "10秒")
        self.assertEqual(detail["空闲超时"], "45秒")
        self.assertTrue(detail["版本一致性"]["ok"])
        self.assertEqual(detail["当前生效版本"]["版本ID"], "WV-0002")

        recalc = self.client.post(
            "/api/window/2/recalculations", json={"values": {"version": "v2"}}
        ).json()
        self.assertTrue(recalc["ok"], recalc["message"])
        record = recalc["entry"]
        self.assertEqual(record["窗口长度"], "60秒")
        self.assertEqual(record["生效版本"], "v2")
        self.assertEqual(record["延迟容忍"], "10秒")
        self.assertEqual(record["空闲超时"], "45秒")
        self.assertEqual(record["水位线版本ID"], "WV-0002")
        self.assertEqual(
            record["水位线配置版本"]["版本ID"], detail["当前生效版本"]["版本ID"]
        )
        self.assertEqual(
            record["水位线配置版本"]["延迟容忍"], detail["当前生效版本"]["延迟容忍"]
        )
        self.assertEqual(
            record["水位线配置版本"]["空闲超时"], detail["当前生效版本"]["空闲超时"]
        )

    def test_wate0001_stays_v1(self) -> None:
        detail = self.client.get("/api/watermark/1").json()
        self.assertEqual(detail["生效版本"], "v1")
        self.assertEqual(detail["延迟容忍"], "5秒")
        self.assertEqual(detail["当前生效版本"]["版本ID"], "WV-0003")
        self.assertEqual(len(detail["版本列表"]), 1)

    # ---------------------------------------------------------------- 版本冲突
    def test_stale_expected_version_is_rejected(self) -> None:
        before = self.client.get("/api/watermark/2").json()
        response = self.client.post(
            "/api/watermark/2/versions",
            json={"values": {"expected_version": "v1", "延迟容忍": "12秒"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("版本冲突", response["message"])
        after = self.client.get("/api/watermark/2").json()
        self.assertEqual(after["生效版本"], "v2")
        self.assertEqual(after["延迟容忍"], before["延迟容忍"])
        self.assertEqual(after["空闲超时"], before["空闲超时"])
        self.assertEqual(after["当前生效版本"]["版本ID"], before["当前生效版本"]["版本ID"])

    def test_missing_expected_version_is_rejected(self) -> None:
        response = self.client.post(
            "/api/watermark/1/versions", json={"values": {"延迟容忍": "8秒"}}
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("expected_version", response["message"])

    def test_concurrent_publish_accepts_only_one_version(self) -> None:
        results: list[dict] = []
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()
            response = self.client.post(
                "/api/watermark/1/versions",
                json={
                    "values": {
                        "expected_version": "v1",
                        "延迟容忍": f"{6 + index}秒",
                        "空闲超时": f"{40 + index}秒",
                    }
                },
            )
            results.append(response.json())

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [item for item in results if item["ok"]]
        self.assertEqual(len(accepted), 1, [item["message"] for item in results])
        self.assertEqual(len(results), 8)
        versions = watermark_versions.versions_for("WATE-0001")
        self.assertEqual(len(versions), 2)
        effective = [row for row in versions if row["status"] == "生效"]
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["生效版本"], "v2")

    # ---------------------------------------------------------------- 幂等
    def test_duplicate_publish_returns_single_record(self) -> None:
        payload = {
            "values": {
                "expected_version": "v2",
                "延迟容忍": "12秒",
                "空闲超时": "50秒",
                "idempotency_key": "publish-WATE-0002-once",
            }
        }
        first = self.client.post("/api/watermark/2/versions", json=payload).json()
        second = self.client.post("/api/watermark/2/versions", json=payload).json()
        self.assertTrue(first["ok"], first["message"])
        self.assertTrue(second["ok"], second["message"])
        self.assertIn("重复提交", second["message"])
        self.assertEqual(
            first["entry"]["水位线配置版本"]["版本ID"],
            second["entry"]["水位线配置版本"]["版本ID"],
        )
        self.assertEqual(len(watermark_versions.versions_for("WATE-0002")), 3)
        self.assertEqual(
            len(watermark_versions.consumers_for(config_no="WATE-0002")), 2
        )
        # 幂等返回同样带出消费侧处理记录，调用方不用再查一次。
        self.assertEqual(len(second["entry"]["消费记录"]), 1)
        self.assertEqual(second["entry"]["消费记录"][0]["延迟容忍"], "12秒")

    def test_derived_idempotency_key_blocks_repeat(self) -> None:
        values = {"expected_version": "v2", "延迟容忍": "12秒", "空闲超时": "50秒"}
        first = self.client.post(
            "/api/watermark/2/versions", json={"values": values}
        ).json()
        second = self.client.post(
            "/api/watermark/2/versions", json={"values": dict(values)}
        ).json()
        self.assertTrue(second["ok"], second["message"])
        self.assertIn("重复提交", second["message"])
        self.assertEqual(
            first["entry"]["水位线配置版本"]["版本ID"],
            second["entry"]["水位线配置版本"]["版本ID"],
        )

    def test_idempotency_key_cannot_be_reused_for_different_payload(self) -> None:
        first = self.client.post(
            "/api/watermark/2/versions",
            json={
                "values": {
                    "expected_version": "v2",
                    "延迟容忍": "12秒",
                    "idempotency_key": "shared-key",
                }
            },
        ).json()
        self.assertTrue(first["ok"], first["message"])
        second = self.client.post(
            "/api/watermark/2/versions",
            json={
                "values": {
                    "expected_version": "v3",
                    "延迟容忍": "15秒",
                    "idempotency_key": "shared-key",
                }
            },
        ).json()
        self.assertFalse(second["ok"])
        self.assertIn("幂等键", second["message"])

    # ---------------------------------------------------------------- 重算
    def test_recalc_rejects_conflicting_version_pointer(self) -> None:
        response = self.client.post(
            "/api/window/2/recalculations",
            json={"values": {"version": "v1", "config_no": "WATE-0002"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("不能回退到旧快照", response["message"])
        self.assertEqual(self.client.get("/api/window/recalculations").json()["total"], 0)

    def test_recalc_rejects_superseded_version_id(self) -> None:
        response = self.client.post(
            "/api/window/2/recalculations",
            json={"values": {"version_id": "WV-0001"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("已被取代", response["message"])

    def test_recalc_uses_single_snapshot_after_publish(self) -> None:
        publish = self.client.post(
            "/api/watermark/2/versions",
            json={"values": {"expected_version": "v2", "延迟容忍": "12秒", "空闲超时": "50秒"}},
        ).json()
        self.assertTrue(publish["ok"], publish["message"])
        version_id = publish["entry"]["水位线配置版本"]["版本ID"]

        first = self.client.post(
            "/api/window/2/recalculations", json={"values": {"version": "v3"}}
        ).json()
        self.assertTrue(first["ok"], first["message"])
        record = first["entry"]
        self.assertEqual(record["生效版本"], "v3")
        self.assertEqual(record["水位线版本ID"], version_id)
        self.assertEqual(record["延迟容忍"], "12秒")
        self.assertEqual(record["空闲超时"], "50秒")
        self.assertEqual(record["水位线配置版本"]["版本ID"], version_id)

        # 不带版本参数时，重算同样落在当前生效版本，并且是同一行。
        second = self.client.post(
            "/api/window/2/recalculations", json={"values": {}}
        ).json()
        self.assertTrue(second["ok"], second["message"])
        self.assertEqual(second["entry"]["水位线版本ID"], version_id)
        self.assertEqual(second["entry"]["延迟容忍"], "12秒")
        self.assertEqual(second["entry"]["空闲超时"], "50秒")

    def test_recalc_is_idempotent(self) -> None:
        body = {"values": {"version": "v2", "window_start": "10:00", "window_end": "10:01"}}
        first = self.client.post("/api/window/2/recalculations", json=body).json()
        second = self.client.post("/api/window/2/recalculations", json=body).json()
        self.assertTrue(second["ok"], second["message"])
        self.assertIn("重复提交", second["message"])
        self.assertEqual(first["entry"]["重算编号"], second["entry"]["重算编号"])
        self.assertEqual(len(store.rows("window_recalc")), 1)

    def test_recalc_requires_matching_config(self) -> None:
        response = self.client.post(
            "/api/window/2/recalculations",
            json={"values": {"config_no": "WATE-0001"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("不匹配", response["message"])

    def test_recalc_locates_config_for_other_stream(self) -> None:
        response = self.client.post(
            "/api/window/3/recalculations", json={"values": {}},
        ).json()
        self.assertTrue(response["ok"], response["message"])
        self.assertEqual(response["entry"]["配置编号"], "WATE-0003")
        self.assertEqual(response["entry"]["生效版本"], "v1")

    # ---------------------------------------------------------------- 校验
    def test_tolerance_below_window_requirement_is_rejected(self) -> None:
        before = self.client.get("/api/watermark/2").json()
        response = self.client.post(
            "/api/watermark/2/versions",
            json={"values": {"expected_version": "v2", "延迟容忍": "6秒"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("WIND-0002", response["message"])
        after = self.client.get("/api/watermark/2").json()
        self.assertEqual(after["延迟容忍"], before["延迟容忍"])
        self.assertEqual(after["当前生效版本"]["版本ID"], before["当前生效版本"]["版本ID"])

    def test_unparsable_duration_is_rejected(self) -> None:
        response = self.client.post(
            "/api/watermark/2/versions",
            json={"values": {"expected_version": "v2", "延迟容忍": "很快"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("延迟容忍", response["message"])

    def test_failed_write_keeps_previous_config_complete(self) -> None:
        before = self.client.get("/api/watermark/2").json()
        response = self.client.post(
            "/api/watermark/2/versions",
            json={"values": {"expected_version": "v2", "空闲超时": "坏值"}},
        ).json()
        self.assertFalse(response["ok"])
        after = self.client.get("/api/watermark/2").json()
        for field in ["时间字段", "延迟容忍", "空闲超时", "发射间隔", "对齐策略", "生效版本"]:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["当前生效版本"], before["当前生效版本"])
        self.assertEqual(after["版本列表"], before["版本列表"])

    # ---------------------------------------------------------------- 同步消费
    def test_publish_syncs_consumer_record(self) -> None:
        publish = self.client.post(
            "/api/watermark/2/versions",
            json={"values": {"expected_version": "v2", "延迟容忍": "12秒", "空闲超时": "50秒"}},
        ).json()
        self.assertTrue(publish["ok"], publish["message"])
        created = publish["entry"]["水位线配置版本"]
        records = self.client.get(
            "/api/watermark/consumers",
            params={"config_no": "WATE-0002", "version_id": created["版本ID"]},
        ).json()
        self.assertEqual(records["total"], 1)
        record = records["items"][0]
        self.assertEqual(record["生效版本"], "v3")
        self.assertEqual(record["延迟容忍"], "12秒")
        self.assertEqual(record["空闲超时"], "50秒")
        self.assertEqual(record["处理结果"], "已接收")

    def test_reopened_detail_matches_consumer_and_recalc(self) -> None:
        publish = self.client.post(
            "/api/watermark/2/versions",
            json={"values": {"expected_version": "v2", "延迟容忍": "12秒", "空闲超时": "50秒"}},
        ).json()
        detail = self.client.get("/api/watermark/2").json()
        version_id = publish["entry"]["水位线配置版本"]["版本ID"]
        self.assertEqual(detail["当前生效版本"]["版本ID"], version_id)
        self.assertEqual(detail["生效版本"], "v3")
        consumer = detail["消费记录"][0]
        self.assertEqual(consumer["延迟容忍"], detail["延迟容忍"])
        self.assertEqual(consumer["空闲超时"], detail["空闲超时"])
        recalc = self.client.post(
            "/api/window/2/recalculations", json={"values": {"version": "v3"}}
        ).json()
        self.assertEqual(recalc["entry"]["延迟容忍"], detail["延迟容忍"])
        self.assertEqual(recalc["entry"]["空闲超时"], detail["空闲超时"])

    # ---------------------------------------------------------------- 状态流转
    def test_action_state_machine_is_monotonic(self) -> None:
        enabled = self.client.post(
            "/api/watermark/1/actions", json={"values": {"action": "启用水位线"}}
        ).json()
        self.assertTrue(enabled["ok"], enabled["message"])
        self.assertEqual(enabled["entry"]["status"], "已启用")
        self.assertEqual(enabled["entry"]["配置状态"], "已启用")

        paused = self.client.post(
            "/api/watermark/1/actions", json={"values": {"action": "暂停水位线"}}
        ).json()
        self.assertTrue(paused["ok"], paused["message"])
        self.assertEqual(paused["entry"]["status"], "已暂停")

        closed = self.client.post(
            "/api/watermark/1/actions", json={"values": {"action": "关闭水位线"}}
        ).json()
        self.assertTrue(closed["ok"], closed["message"])
        self.assertEqual(closed["entry"]["status"], "已关闭")

        repeat = self.client.post(
            "/api/watermark/1/actions", json={"values": {"action": "关闭水位线"}}
        ).json()
        self.assertTrue(repeat["ok"], repeat["message"])
        self.assertEqual(repeat["entry"]["status"], "已关闭")

    def test_illegal_action_is_rejected_without_changing_fields(self) -> None:
        before = self.client.get("/api/watermark/1").json()
        illegal = self.client.post(
            "/api/watermark/1/actions", json={"values": {"action": "关闭水位线"}}
        ).json()
        self.assertFalse(illegal["ok"])
        self.assertIn("顺序", illegal["message"])
        after = self.client.get("/api/watermark/1").json()
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["生效版本"], before["生效版本"])

    def test_closed_config_rejects_new_version(self) -> None:
        self.client.post(
            "/api/watermark/1/actions", json={"values": {"action": "暂停水位线"}}
        )
        self.client.post(
            "/api/watermark/1/actions", json={"values": {"action": "关闭水位线"}}
        )
        response = self.client.post(
            "/api/watermark/1/versions",
            json={"values": {"expected_version": "v1", "延迟容忍": "9秒"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("已关闭", response["message"])

    # ---------------------------------------------------------------- 新配置
    def test_new_config_first_version_starts_at_v1(self) -> None:
        created = self.client.post(
            "/api/watermark",
            json={
                "values": {
                    "配置编号": "WATE-9009",
                    "流名称": "库存变更流",
                    "时间字段": "event_time",
                }
            },
        ).json()
        entry_id = created["entry"]["id"]
        self.assertIsNone(created["entry"]["生效版本"])
        detail = self.client.get(f"/api/watermark/{entry_id}").json()
        self.assertFalse(detail["版本一致性"]["ok"])

        first = self.client.post(
            f"/api/watermark/{entry_id}/versions",
            json={
                "values": {
                    "expected_version": "v0",
                    "延迟容忍": "6秒",
                    "空闲超时": "20秒",
                }
            },
        ).json()
        self.assertTrue(first["ok"], first["message"])
        self.assertEqual(first["entry"]["水位线配置版本"]["生效版本"], "v1")

        reopen = self.client.get(f"/api/watermark/{entry_id}").json()
        self.assertTrue(reopen["版本一致性"]["ok"])
        self.assertEqual(reopen["生效版本"], "v1")
        self.assertEqual(reopen["空闲超时"], "20秒")

    # ---------------------------------------------------------------- 数据链路
    def test_flow_describes_six_steps(self) -> None:
        flow = self.client.get("/api/watermark/flow").json()
        self.assertEqual(len(flow["steps"]), 6)
        self.assertEqual([step["step"] for step in flow["steps"]], [1, 2, 3, 4, 5, 6])
        self.assertIn("watermark_version", flow["tables"])
        self.assertTrue(flow["invariants"])
        self.assertIn("flowchart TD", flow["mermaid"])


if __name__ == "__main__":
    unittest.main()
