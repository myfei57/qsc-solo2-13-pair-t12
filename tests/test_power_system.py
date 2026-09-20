"""供电与保安电源：负荷分级、晃电/停电切负荷、发电机自启动与顺序复电。"""

from __future__ import annotations

import unittest

from flashsmelter.errors import GuardViolation, NotFoundError, ValidationError
from flashsmelter.power import (
    EV_ATS_CLOSE,
    EV_BLACKOUT_ENTER,
    EV_LOAD_ENERGIZED,
    EV_LOAD_SHED,
    EV_SAG_ENTER,
    LEDGER_STREAM,
)
from flashsmelter.application import Application
from flashsmelter.config import Settings
from flashsmelter.runtime import ManualClock

from .helpers import make_app, make_root


class LoadCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.power = self.app.power

    def test_default_catalog_grades(self) -> None:
        grades = self.power.grade_summary()
        self.assertEqual(7, grades["security"]["devices"])
        self.assertEqual(4, grades["critical"]["devices"])
        self.assertEqual(3, grades["normal"]["devices"])
        self.assertEqual(1, grades["optional"]["devices"])
        # 默认全部带电，总保安负荷不超过发电机容量。
        self.assertEqual(15, self.power.status()["counts"]["energized"])
        genset_kw = sum(
            d["kw"] for d in self.power.loads() if d["grade"] == "security" and d["source"] == "genset"
        )
        self.assertLessEqual(genset_kw, self.app.settings.power_genset_capacity_kw)

    def test_register_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.power.register_load(
                "ops", device_id="x", name="X", grade="security", kw=10.0,
                sag_policy="manual", source="ups", restore_order=50,
            )
        with self.assertRaises(ValidationError):
            self.power.register_load(
                "ops", device_id="x", name="X", grade="normal", kw=10.0,
                sag_policy="keep", source="grid", restore_order=50,
            )
        with self.assertRaises(ValidationError):
            self.power.register_load(
                "ops", device_id="x", name="X", grade="critical", kw=10.0,
                sag_policy="restart", source="grid", sag_group=0, restore_order=50,
            )

    def test_register_overcapacity_rejected(self) -> None:
        with self.assertRaises(ValidationError) as exc:
            self.power.register_load(
                "ops", device_id="big-pump", name="大容量泵", grade="security", kw=9000.0,
                sag_policy="keep", source="genset", restore_order=80,
            )
        self.assertIn("capacity_kw", exc.exception.details)

    def test_registered_load_starts_blocked(self) -> None:
        self.power.register_load(
            "ops", device_id="mixer-7", name="七号搅拌机", grade="normal", kw=12.0,
            sag_policy="manual", source="grid", restore_order=950,
        )
        device = next(d for d in self.power.loads() if d["device_id"] == "mixer-7")
        self.assertFalse(device["energized"])
        self.assertTrue(device["blocked"])
        with self.assertRaises(NotFoundError):
            self.power.cut("ops", device_id="ghost", reason="测试")


class SagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.power = self.app.power
        self.s = self.app.settings

    def test_sag_keeps_security_and_sheds_by_grade(self) -> None:
        self.power.scan("scada", grid_voltage=0.60)
        self.assertEqual("sag", self.power.state)
        energized = {d["device_id"] for d in self.power.loads() if d["energized"]}
        # 恰好 7 台保安负荷全部保持。
        self.assertEqual(7, len(energized))
        for device in self.power.loads():
            if device["grade"] == "security":
                self.assertIn(device["device_id"], energized)
        # 切除事件必须按 可拉闸 → 一般 → 重要 的级别顺序，级别内按复电序倒序。
        sheds = [e for e in self.power.ledger(event=EV_LOAD_SHED)]
        shed_grades = [e["details"]["grade"] for e in sheds]
        self.assertEqual(sorted(shed_grades, key=("optional", "normal", "critical").index), shed_grades)
        orders = {d["device_id"]: d["restore_order"] for d in self.power.loads()}
        by_grade: dict[str, list[int]] = {}
        for event in sheds:
            by_grade.setdefault(event["details"]["grade"], []).append(orders[event["device_id"]])
        for seq in by_grade.values():
            self.assertEqual(sorted(seq, reverse=True), seq)
        incident = self.power.status()["incident"]
        self.assertEqual("sag", incident["kind"])
        self.assertTrue(incident["id"].startswith("SAG-"))

    def test_sag_recovery_batched_auto_restart(self) -> None:
        self.power.scan("scada", grid_voltage=0.60)
        self.app.clock.advance(1)
        self.power.scan("scada", grid_voltage=0.95)
        self.assertEqual("idle", self.power.state)
        # 手动送电类（球磨机、行车）不进自启动队列。
        pending = {item[1] for item in self.power.status()["pending_restarts"]}
        self.assertNotIn("ball-mill", pending)
        self.assertNotIn("matte-ladle-crane", pending)
        # 第一批立即可送；逐批推进。
        self.app.clock.advance(self.s.power_sag_group_delay_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.95)
        self.assertTrue(
            next(d for d in self.power.loads() if d["device_id"] == "forced-draft-fan")["energized"]
        )
        self.app.clock.advance(self.s.power_sag_group_delay_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.95)
        self.app.clock.advance(self.s.power_sag_group_delay_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.95)
        status = self.power.status()
        # 15 台 - 2 台手动类 - 2 台新登记（本例无）= 13 台带电。
        self.assertEqual(13, status["counts"]["energized"])
        self.assertEqual(2, status["counts"]["de_energized"])
        self.assertIsNone(status["incident"])

    def test_sag_escalates_to_blackout_on_timeout(self) -> None:
        self.power.scan("scada", grid_voltage=0.60)
        self.app.clock.advance(self.s.power_sag_to_blackout_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.60)
        self.assertEqual("blackout", self.power.state)
        incidents = [e["incident_id"] for e in self.power.ledger(event=EV_BLACKOUT_ENTER)]
        self.assertTrue(incidents[-1].startswith("OUT-"))

    def test_sag_noise_below_threshold_ignored(self) -> None:
        self.power.scan("scada", grid_voltage=0.97)
        self.assertEqual("idle", self.power.state)
        self.assertEqual(0, len(self.power.ledger(event=EV_SAG_ENTER)))


class BlackoutAndGensetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.power = self.app.power
        self.s = self.app.settings

    def _blackout(self) -> None:
        self.power.scan("scada", grid_voltage=0.0)

    def test_blackout_ups_survives_genset_carries_security(self) -> None:
        self._blackout()
        self.assertEqual("blackout", self.power.state)
        self.assertEqual("starting", self.power.genset_status()["state"])
        ups = {d["device_id"] for d in self.power.loads() if d["energized"]}
        self.assertEqual(
            {"emergency-lighting", "dcs-control-power", "burner-plc-io"}, ups
        )
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("genset", self.power.state)
        self.assertTrue(self.power.genset_status()["ats_closed"])
        self.assertEqual(330.0, self.power.genset_status()["load_kw"])
        self.assertEqual(7, self.power.status()["counts"]["energized"])
        self.assertTrue(any(e["event"] == EV_ATS_CLOSE for e in self.power.ledger()))

    def test_genset_failure_manual_reset(self) -> None:
        self._blackout()
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0, genset_fault=True)
        self.assertEqual("genset_failed", self.power.state)
        with self.assertRaises(ValidationError):
            self.power.reset("ops", note="  ")
        # 冷却时间未到且无故障信号，扫描不会自动重试（但人工复位直接重试）。
        self.power.reset("ops", note="更换启动电瓶")
        self.assertEqual("blackout", self.power.state)
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("genset", self.power.state)
        self.assertEqual(1, self.power.genset_status()["failure_count"])

    def test_genset_failure_auto_retry_after_cooldown(self) -> None:
        self._blackout()
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0, genset_fault=True)
        self.assertEqual("genset_failed", self.power.state)
        self.app.clock.advance(1)
        self.power.scan("scada", grid_voltage=0.0, genset_fault=True)
        self.assertEqual("genset_failed", self.power.state)
        self.app.clock.advance(self.s.power_genset_retry_cooldown_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0, genset_fault=False)
        self.assertEqual("blackout", self.power.state)
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("genset", self.power.state)

    def test_grid_recovery_requires_stable_confirmation(self) -> None:
        self._blackout()
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("genset", self.power.state)
        # 电压恢复但未持续确认，不动作。
        self.power.scan("scada", grid_voltage=0.95)
        self.assertEqual("genset", self.power.state)
        # 中途又跌下去，确认计时作废。
        self.power.scan("scada", grid_voltage=0.0)
        self.power.scan("scada", grid_voltage=0.95)
        self.app.clock.advance(self.s.power_grid_return_confirm_seconds - 1)
        self.power.scan("scada", grid_voltage=0.95)
        self.assertEqual("genset", self.power.state)
        self.app.clock.advance(1.1)
        self.power.scan("scada", grid_voltage=0.95)
        self.assertEqual("restoring", self.power.state)

    def test_genset_test_and_test_interruption(self) -> None:
        self.power.genset_test("ops")
        self.assertEqual("testing", self.power.state)
        with self.assertRaises(GuardViolation):
            self.power.genset_test("ops")
        # 正常结束。
        self.power.genset_test_end("ops")
        self.assertEqual("idle", self.power.state)
        self.assertEqual("stopped", self.power.genset_status()["state"])
        # 试机期间真停电：发电机无需再等启动时长即可带保安负荷。
        self.power.genset_test("ops")
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("blackout", self.power.state)
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("genset", self.power.state)
        self.assertTrue(self.power.genset_status()["ats_closed"])


class OrderedRestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.power = self.app.power
        self.s = self.app.settings

    def _to_restoring(self) -> None:
        self.power.scan("scada", grid_voltage=0.0)
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0)
        self.power.scan("scada", grid_voltage=0.95)
        self.app.clock.advance(self.s.power_grid_return_confirm_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.95)
        self.assertEqual("restoring", self.power.state)

    def test_one_device_per_step_in_restore_order(self) -> None:
        self._to_restoring()
        fed_order: list[str] = []
        # 第一台在进入 restoring 时立即送出，之后每步一台。
        for _ in range(20):
            self.app.clock.advance(self.s.power_restore_step_seconds + 0.1)
            self.power.scan("scada", grid_voltage=0.95)
            if self.power.state == "idle":
                break
        fed = [
            e["device_id"]
            for e in self.power.ledger(event=EV_LOAD_ENERGIZED)
            if e["reason"] == "ordered-restore"
        ]
        fed_order = list(fed)
        orders = {d["device_id"]: d["restore_order"] for d in self.power.loads()}
        self.assertEqual(fed_order, sorted(fed_order, key=lambda d: orders[d]))
        self.assertEqual("idle", self.power.state)
        # 自动复电共 10 台：发电机保安 4 台重送 + 非手动的市电负荷 6 台。
        self.assertEqual(10, len(fed_order))
        # 手动送电类仍未恢复。
        for device_id in ("ball-mill", "matte-ladle-crane"):
            self.assertFalse(
                next(d for d in self.power.loads() if d["device_id"] == device_id)["energized"]
            )

    def test_restore_interrupted_starts_new_incident(self) -> None:
        self._to_restoring()
        first_incident = self.power.status()["incident"]["id"]
        self.app.clock.advance(self.s.power_restore_step_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.95)
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("blackout", self.power.state)
        self.assertNotEqual(first_incident, self.power.status()["incident"]["id"])
        self.assertEqual([], self.power.status()["restore_queue"])


class ManualInterlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.power = self.app.power
        self.s = self.app.settings

    def test_manual_cut_requires_reason_and_blocks_auto_restore(self) -> None:
        with self.assertRaises(ValidationError):
            self.power.cut("ops", device_id="concentrate-belt", reason=" ")
        self.power.cut("ops", device_id="concentrate-belt", reason="皮带检修挂牌")
        belt = next(d for d in self.power.loads() if d["device_id"] == "concentrate-belt")
        self.assertFalse(belt["energized"])
        self.assertTrue(belt["blocked"])
        # 走一遍停电复电，检修设备不应被自动带上。
        self.power.scan("scada", grid_voltage=0.0)
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0)
        self.power.scan("scada", grid_voltage=0.95)
        self.app.clock.advance(self.s.power_grid_return_confirm_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.95)
        self.power.restore_now("ops")
        belt = next(d for d in self.power.loads() if d["device_id"] == "concentrate-belt")
        self.assertFalse(belt["energized"])
        self.power.feed("ops", device_id="concentrate-belt")
        belt = next(d for d in self.power.loads() if d["device_id"] == "concentrate-belt")
        self.assertTrue(belt["energized"])
        self.assertFalse(belt["blocked"])

    def test_manual_feed_blocked_in_sag_and_blackout(self) -> None:
        self.power.scan("scada", grid_voltage=0.6)
        with self.assertRaises(GuardViolation):
            self.power.feed("ops", device_id="ball-mill")
        # 保安负荷在晃电中允许人工送电。
        self.power.cut("ops", device_id="emergency-lighting", reason="试灯")
        self.power.feed("ops", device_id="emergency-lighting")
        self.assertTrue(
            next(d for d in self.power.loads() if d["device_id"] == "emergency-lighting")["energized"]
        )
        self.app.clock.advance(self.s.power_sag_to_blackout_seconds + 1)
        self.power.scan("scada", grid_voltage=0.0)
        with self.assertRaises(GuardViolation):
            self.power.feed("ops", device_id="forced-draft-fan")

    def test_manual_feed_on_genset_only_for_security(self) -> None:
        self.power.scan("scada", grid_voltage=0.0)
        self.app.clock.advance(self.s.power_genset_start_seconds + 0.1)
        self.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("genset", self.power.state)
        with self.assertRaises(GuardViolation):
            self.power.feed("ops", device_id="forced-draft-fan")


class LedgerAndDurabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.power = self.app.power
        self.s = self.app.settings

    def test_every_stop_and_feed_recorded_per_device(self) -> None:
        self.power.scan("scada", grid_voltage=0.6)
        self.app.clock.advance(1)
        self.power.scan("scada", grid_voltage=0.95)
        self.app.clock.advance(self.s.power_sag_group_delay_seconds * 4 + 1)
        self.power.scan("scada", grid_voltage=0.95)
        belt_events = self.power.ledger(device_id="concentrate-belt")
        kinds = [(e["event"], e["incident_kind"]) for e in belt_events]
        self.assertIn((EV_LOAD_SHED, "sag"), kinds)
        self.assertIn((EV_LOAD_ENERGIZED, "sag"), kinds)
        for event in belt_events:
            self.assertIn("at", event)
            self.assertTrue(event["incident_id"].startswith("SAG-"))

    def test_ledger_incident_filter(self) -> None:
        self.power.scan("scada", grid_voltage=0.0)
        incident = self.power.status()["incident"]["id"]
        events = self.power.ledger(incident_id=incident)
        self.assertTrue(events)
        self.assertTrue(all(e["incident_id"] == incident for e in events))

    def test_state_restored_after_restart_mid_blackout(self) -> None:
        root = make_root()
        clock = ManualClock()
        first = Application(Settings(root=root), clock=clock)
        st = first.settings
        first.power.scan("scada", grid_voltage=0.0)
        clock.advance(st.power_genset_start_seconds + 0.1)
        first.power.scan("scada", grid_voltage=0.0)
        self.assertEqual("genset", first.power.state)
        energized_before = {
            d["device_id"] for d in first.power.loads() if d["energized"]
        }

        second_clock = ManualClock(clock.timestamp())
        second = Application(Settings(root=root), clock=second_clock)
        self.assertEqual("genset", second.power.state)
        self.assertTrue(second.power.genset_status()["ats_closed"])
        energized_after = {d["device_id"] for d in second.power.loads() if d["energized"]}
        self.assertEqual(energized_before, energized_after)
        self.assertEqual(1, second.power.status()["counts"]["blackout_events"])
        # 重启后可继续复电流程。
        second.power.scan("scada", grid_voltage=0.95)
        second_clock.advance(st.power_grid_return_confirm_seconds + 0.1)
        second.power.scan("scada", grid_voltage=0.95)
        self.assertEqual("restoring", second.power.state)
        second.power.restore_now("ops")
        self.assertEqual("idle", second.power.state)
        # 台账为追加流水，重启后仍可完整回溯。
        self.assertGreaterEqual(
            second.store.stream_length(LEDGER_STREAM),
            first.store.stream_length(LEDGER_STREAM),
        )

    def test_ledger_stream_name(self) -> None:
        self.assertEqual("power/ledger", LEDGER_STREAM)


if __name__ == "__main__":
    unittest.main()
