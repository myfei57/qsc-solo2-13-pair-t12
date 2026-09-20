"""供电与保安电源：负荷分级、失电按级切负荷、柴发顶上、按序复电与停送记录。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.errors import GuardViolation, StateTransitionError, ValidationError

from .helpers import make_app, make_root

TIER1_IDS = {"exhaust-fan", "cooling-water-pump", "lube-oil-pump", "dcs-ups", "emergency-lighting"}
TIER2_IDS = {"oxygen-fan", "boiler-feed-pump", "burner-fan"}
TIER3_IDS = {"conc-feeder", "casting-crane", "converter-drive", "workshop-power"}


def blackout(app, *, cause="市电I段失压"):
    """失电并等柴发达到并网条件。"""

    app.power.grid_loss("ops", cause=cause)
    app.clock.advance(app.settings.power_generator_start_seconds + 1)
    app.power.generator_running("ops")
    return app.power.status()


def restore_all(app):
    """过确认窗口后按序送完全部负荷。"""

    app.clock.advance(app.settings.power_grid_confirm_seconds + 1)
    while app.power.status()["pending_restore"]:
        app.power.restore_next("ops")
        app.clock.advance(app.settings.power_restore_step_seconds + 1)
    return app.power.status()


class LoadSheddingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_grid_loss_sheds_by_tier_and_logs_each_load(self) -> None:
        status = self.app.power.status()
        self.assertEqual("grid", status["state"])
        self.assertEqual(12, status["on_count"])

        status = self.app.power.grid_loss("ops", cause="市电I段失压")
        self.assertEqual("shedding", status["state"])
        self.assertEqual("starting", status["generator"]["state"])
        self.assertEqual(5, status["on_count"])
        self.assertEqual(7, status["off_count"])
        by_id = {load["load_id"]: load for load in status["loads"]}
        for load_id in TIER1_IDS:
            self.assertTrue(by_id[load_id]["on"], load_id)
        for load_id in TIER2_IDS | TIER3_IDS:
            self.assertFalse(by_id[load_id]["on"], load_id)
            self.assertEqual("shed", by_id[load_id]["last_action"])
            self.assertIsNotNone(by_id[load_id]["last_changed_at"])

        episode = status["episode"]
        self.assertEqual("PE-0001", episode["episode_id"])
        self.assertEqual("市电I段失压", episode["cause"])
        self.assertIsNotNone(episode["lost_at"])
        self.assertEqual(7, len(episode["shed_loads"]))

        shed_events = [event for event in self.app.power.events(limit=50) if event["kind"] == "shed"]
        self.assertEqual(7, len(shed_events))
        self.assertTrue(all(event["episode_id"] == "PE-0001" for event in shed_events))
        self.assertTrue(all(event["at"] for event in shed_events))
        # 三级全部先于二级切除。
        tiers = [event["tier"] for event in shed_events]
        self.assertEqual([3, 3, 3, 3, 2, 2, 2], tiers)

    def test_grid_loss_requires_normal_or_restoring_state(self) -> None:
        self.app.power.grid_loss("ops")
        with self.assertRaises(StateTransitionError):
            self.app.power.grid_loss("ops")

    def test_security_load_cannot_be_shed_manually(self) -> None:
        with self.assertRaises(GuardViolation) as blocked:
            self.app.power.shed_load("ops", load_id="dcs-ups", reason="检修")
        self.assertIn("保安负荷禁止切除", str(blocked.exception))

    def test_manual_shed_and_restore_are_logged(self) -> None:
        status = self.app.power.shed_load("ops", load_id="workshop-power", reason="检修停电")
        by_id = {load["load_id"]: load for load in status["loads"]}
        self.assertFalse(by_id["workshop-power"]["on"])
        with self.assertRaises(ValidationError):
            self.app.power.shed_load("ops", load_id="casting-crane", reason="  ")
        with self.assertRaises(StateTransitionError):
            self.app.power.shed_load("ops", load_id="workshop-power", reason="重复")
        status = self.app.power.restore_load("ops", load_id="workshop-power")
        by_id = {load["load_id"]: load for load in status["loads"]}
        self.assertTrue(by_id["workshop-power"]["on"])
        events = self.app.power.events(limit=10)
        manual = [event for event in events if event.get("detail", {}).get("manual")]
        self.assertEqual(["shed", "restore"], [event["kind"] for event in manual])
        self.assertEqual("检修停电", manual[0]["detail"]["reason"])


class GeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_generator_requires_start_delay_then_takes_security_bus(self) -> None:
        self.app.power.grid_loss("ops")
        with self.assertRaises(GuardViolation) as early:
            self.app.power.generator_running("ops")
        self.assertIn("remaining_seconds", early.exception.details)
        self.app.clock.advance(self.app.settings.power_generator_start_seconds + 1)
        status = self.app.power.generator_running("ops")
        self.assertEqual("emergency", status["state"])
        self.assertEqual("running", status["generator"]["state"])
        self.assertEqual(1, status["generator"]["run_count"])
        self.assertAlmostEqual(255.0, status["security_bus"]["load_kw"], places=3)
        self.assertEqual(5, status["on_count"])

    def test_emergency_feeds_security_tier_only(self) -> None:
        blackout(self.app)
        with self.assertRaises(GuardViolation):
            self.app.power.restore_load("ops", load_id="conc-feeder")
        with self.assertRaises(GuardViolation):
            self.app.power.restore_load("ops", load_id="oxygen-fan")

    def test_generator_standby_after_cooldown(self) -> None:
        blackout(self.app)
        self.app.power.grid_restored("ops")
        status = restore_all(self.app)
        self.assertEqual("cooldown", status["generator"]["state"])
        with self.assertRaises(GuardViolation) as early:
            self.app.power.generator_standby("ops")
        self.assertIn("remaining_seconds", early.exception.details)
        self.app.clock.advance(self.app.settings.power_generator_cooldown_seconds + 1)
        status = self.app.power.generator_standby("ops")
        self.assertEqual("standby", status["generator"]["state"])
        self.assertEqual(1, status["generator"]["run_count"])

    def test_fluctuation_aborts_generator_start(self) -> None:
        self.app.power.grid_loss("ops", cause="晃电")
        status = self.app.power.grid_restored("ops")
        self.assertEqual("restoring", status["state"])
        self.assertEqual("standby", status["generator"]["state"])
        status = restore_all(self.app)
        self.assertEqual("grid", status["state"])
        self.assertEqual(0, status["generator"]["run_count"])
        kinds = [event["kind"] for event in self.app.power.events(limit=50)]
        self.assertIn("gen_abort", kinds)
        self.assertNotIn("gen_running", kinds)


class RestoreSequenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_restore_follows_order_with_confirm_window_and_step(self) -> None:
        blackout(self.app)
        self.app.power.grid_restored("ops")
        with self.assertRaises(GuardViolation) as too_early:
            self.app.power.restore_next("ops")
        self.assertIn("remaining_seconds", too_early.exception.details)

        self.app.clock.advance(self.app.settings.power_grid_confirm_seconds + 1)
        status = self.app.power.restore_next("ops")
        self.assertEqual("restoring", status["state"])
        on_ids = {load["load_id"] for load in status["loads"] if load["on"]}
        self.assertIn("oxygen-fan", on_ids)

        with self.assertRaises(GuardViolation) as step_blocked:
            self.app.power.restore_next("ops")
        self.assertIn("remaining_seconds", step_blocked.exception.details)

        self.app.clock.advance(self.app.settings.power_restore_step_seconds + 1)
        self.app.power.restore_next("ops")
        on_ids = {load["load_id"] for load in self.app.power.status()["loads"] if load["on"]}
        self.assertIn("boiler-feed-pump", on_ids)

        status = restore_all(self.app)
        self.assertEqual("grid", status["state"])
        self.assertEqual(12, status["on_count"])
        self.assertEqual([], status["pending_restore"])
        self.assertEqual("cooldown", status["generator"]["state"])

        episode = status["episodes"][-1]
        self.assertEqual("PE-0001", episode["episode_id"])
        self.assertEqual("restored", episode["outcome"])
        self.assertIsNotNone(episode["restored_at"])

        restores = [event for event in self.app.power.events(limit=100) if event["kind"] == "restore"]
        self.assertEqual(7, len(restores))
        ordered = [event["load_id"] for event in restores]
        self.assertEqual(
            [
                "oxygen-fan",
                "boiler-feed-pump",
                "burner-fan",
                "conc-feeder",
                "casting-crane",
                "converter-drive",
                "workshop-power",
            ],
            ordered,
        )

    def test_every_switch_has_timestamp(self) -> None:
        blackout(self.app)
        self.app.power.grid_restored("ops")
        restore_all(self.app)
        for load in self.app.power.status()["loads"]:
            if load["tier"] == 1:
                # 保安负荷全程保电，没有任何停送动作——这本身就是保电的证据。
                self.assertIsNone(load["last_action"], load["load_id"])
                continue
            self.assertIsNotNone(load["last_changed_at"], load["load_id"])
            self.assertEqual("restore", load["last_action"])

    def test_reloss_during_restore_sheds_again_with_new_episode(self) -> None:
        blackout(self.app)
        self.app.power.grid_restored("ops")
        self.app.clock.advance(self.app.settings.power_grid_confirm_seconds + 1)
        self.app.power.restore_next("ops")  # oxygen-fan 已送上

        status = self.app.power.grid_loss("ops", cause="复电途中再次失电")
        self.assertEqual("emergency", status["state"])  # 柴发未停，直接回应急供电
        self.assertEqual("running", status["generator"]["state"])
        self.assertEqual("PE-0002", status["episode"]["episode_id"])
        by_id = {load["load_id"]: load for load in status["loads"]}
        self.assertFalse(by_id["oxygen-fan"]["on"])  # 刚送上的二级负荷再次被切
        self.assertTrue(by_id["dcs-ups"]["on"])

        episodes = status["episodes"]
        self.assertEqual("PE-0001", episodes[-1]["episode_id"])
        self.assertEqual("interrupted", episodes[-1]["outcome"])


class LoadRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_security_bus_capacity_guard(self) -> None:
        with self.assertRaises(GuardViolation) as over:
            self.app.power.register_load(
                "ops", load_id="big-ups", label="大容量UPS", tier=1, rated_kw=600.0, restore_order=6
            )
        self.assertIn("capacity_kw", over.exception.details)
        status = self.app.power.register_load(
            "ops", load_id="small-ups", label="仪表UPS", tier=1, rated_kw=500.0, restore_order=6
        )
        self.assertAlmostEqual(755.0, status["security_bus"]["load_kw"], places=3)

    def test_register_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.power.register_load(
                "ops", load_id="bad id!", label="x", tier=3, rated_kw=10.0, restore_order=30
            )
        with self.assertRaises(ValidationError):
            self.app.power.register_load(
                "ops", load_id="ok-id", label="x", tier=4, rated_kw=10.0, restore_order=30
            )
        with self.assertRaises(ValidationError):
            self.app.power.register_load(
                "ops", load_id="ok-id", label="x", tier=3, rated_kw=0.0, restore_order=30
            )
        with self.assertRaises(ValidationError):
            self.app.power.register_load(
                "ops", load_id="ok-id", label="", tier=3, rated_kw=10.0, restore_order=30
            )

    def test_register_update_and_unregister(self) -> None:
        self.app.power.register_load(
            "ops", load_id="sump-pump", label="集水井泵", tier=3, rated_kw=45.0, restore_order=30
        )
        status = self.app.power.register_load(
            "ops", load_id="sump-pump", label="集水井排污泵", tier=3, rated_kw=55.0, restore_order=31
        )
        by_id = {load["load_id"]: load for load in status["loads"]}
        self.assertEqual("集水井排污泵", by_id["sump-pump"]["label"])
        self.assertAlmostEqual(55.0, by_id["sump-pump"]["rated_kw"], places=3)

        with self.assertRaises(GuardViolation):
            self.app.power.unregister_load("ops", load_id="sump-pump")  # 仍带电
        self.app.power.shed_load("ops", load_id="sump-pump", reason="拆除")
        status = self.app.power.unregister_load("ops", load_id="sump-pump")
        ids = {load["load_id"] for load in status["loads"]}
        self.assertNotIn("sump-pump", ids)
        with self.assertRaises(ValidationError):
            self.app.power.restore_load("ops", load_id="sump-pump")


class PowerPersistenceTest(unittest.TestCase):
    def test_state_and_event_log_survive_restart(self) -> None:
        root = make_root()
        app = make_app(root=root)
        blackout(app)
        app.power.grid_restored("ops")
        app.clock.advance(app.settings.power_grid_confirm_seconds + 1)
        app.power.restore_next("ops")

        restarted = Application(app.settings, clock=app.clock)
        status = restarted.power.status()
        self.assertEqual("restoring", status["state"])
        self.assertEqual("running", status["generator"]["state"])
        self.assertEqual(6, status["off_count"])
        self.assertEqual("PE-0001", status["episode"]["episode_id"])
        events = restarted.power.events(limit=100, episode_id="PE-0001")
        kinds = [event["kind"] for event in events]
        self.assertIn("grid_loss", kinds)
        self.assertIn("gen_running", kinds)
        self.assertIn("restore", kinds)
        self.assertTrue(restarted.store.verify().ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
