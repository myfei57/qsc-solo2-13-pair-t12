"""供电与保安电源组件。

把「失电怎么切、复电怎么送」从值班临场判断变成既定逻辑：

- 负荷分级：一级保安负荷（事故排烟、循环水、仪表电源等）失电不切，由柴油
  发电机经保安段保电；二级重要负荷、三级一般负荷按级别先后切除。
- 失电：按 三级 → 二级 顺序切负荷，柴发自动启动，并网后带上保安段。
- 复电：市电恢复先经过确认窗口（挡住晃电反复），再按 ``restore_order``
  一台台送电，每台之间留出送电间隔。
- 记录：每台设备什么时候停、什么时候送都写进 ``power/events`` 事件流，
  一次失电为一个 episode，事后可以完整还原。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, StateTransitionError, ValidationError
from ..machine import StateMachine
from ..runtime import RuntimeContext

STATES = ("grid", "shedding", "emergency", "restoring")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "grid": ("shedding",),
    "shedding": ("emergency", "restoring"),
    "emergency": ("restoring",),
    "restoring": ("grid", "shedding", "emergency"),
}

GEN_STATES = ("standby", "starting", "running", "cooldown")

GEN_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "standby": ("starting",),
    "starting": ("running", "standby"),
    "running": ("cooldown",),
    "cooldown": ("standby",),
}

POWER_STREAM = "power/events"

TIERS = (1, 2, 3)

_LOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

# 默认负荷表即一条正常运行的闪速炉产线：一级挂保安段，失电保持；二级、三级
# 按 restore_order 复电，越靠后的越先被切。
DEFAULT_LOADS: tuple[dict[str, Any], ...] = (
    {"load_id": "exhaust-fan", "label": "事故排烟风机", "tier": 1, "rated_kw": 90.0, "restore_order": 1},
    {"load_id": "cooling-water-pump", "label": "循环冷却水泵", "tier": 1, "rated_kw": 75.0, "restore_order": 2},
    {"load_id": "lube-oil-pump", "label": "润滑油站", "tier": 1, "rated_kw": 30.0, "restore_order": 3},
    {"load_id": "dcs-ups", "label": "仪表与DCS电源", "tier": 1, "rated_kw": 45.0, "restore_order": 4},
    {"load_id": "emergency-lighting", "label": "事故照明", "tier": 1, "rated_kw": 15.0, "restore_order": 5},
    {"load_id": "oxygen-fan", "label": "富氧风机", "tier": 2, "rated_kw": 250.0, "restore_order": 10},
    {"load_id": "boiler-feed-pump", "label": "余热锅炉给水泵", "tier": 2, "rated_kw": 110.0, "restore_order": 11},
    {"load_id": "burner-fan", "label": "燃烧器助燃风机", "tier": 2, "rated_kw": 132.0, "restore_order": 12},
    {"load_id": "conc-feeder", "label": "精矿给料系统", "tier": 3, "rated_kw": 200.0, "restore_order": 20},
    {"load_id": "casting-crane", "label": "放铜行车", "tier": 3, "rated_kw": 160.0, "restore_order": 21},
    {"load_id": "converter-drive", "label": "转炉倾动", "tier": 3, "rated_kw": 185.0, "restore_order": 22},
    {"load_id": "workshop-power", "label": "检修与照明电源", "tier": 3, "rated_kw": 120.0, "restore_order": 23},
)


class PowerSupply(Component):
    name = "power"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("power", "grid", TRANSITIONS, ctx.clock)
        self._gen = StateMachine("generator", "standby", GEN_TRANSITIONS, ctx.clock)
        self._loads: list[dict[str, Any]] = []
        self._episode_seq = 0
        self._episode: dict[str, Any] | None = None
        self._episodes: list[dict[str, Any]] = []
        self._gen_started_at: float | None = None
        self._gen_running_at: float | None = None
        self._gen_cooldown_at: float | None = None
        self._gen_run_count = 0
        self._grid_back_at: float | None = None
        self._last_restore_at: float | None = None
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            generator = restored.get("generator")
            if isinstance(generator, Mapping):
                self._gen.restore(generator)
            loads = restored.get("loads")
            if isinstance(loads, list):
                self._loads = [dict(entry) for entry in loads if isinstance(entry, dict)]
            self._episode_seq = int(restored.get("episode_seq", 0))
            episode = restored.get("episode")
            if isinstance(episode, dict):
                self._episode = dict(episode)
            episodes = restored.get("episodes")
            if isinstance(episodes, list):
                self._episodes = [dict(entry) for entry in episodes if isinstance(entry, dict)]
            self._gen_started_at = restored.get("gen_started_at")
            self._gen_running_at = restored.get("gen_running_at")
            self._gen_cooldown_at = restored.get("gen_cooldown_at")
            self._gen_run_count = int(restored.get("gen_run_count", 0))
            self._grid_back_at = restored.get("grid_back_at")
            self._last_restore_at = restored.get("last_restore_at")
        if not self._loads:
            self._seed_default_loads()
        self._refresh_gauges()

    # ------------------------------------------------------------------ 负荷台账
    def register_load(
        self,
        actor: str,
        *,
        load_id: str,
        label: str,
        tier: int,
        rated_kw: float,
        restore_order: int,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "register_load",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            load_id = self._validate_load_id(load_id)
            label = self._validate_label(label)
            if tier not in TIERS:
                raise ValidationError("负荷级别必须是 1/2/3", details={"tier": tier})
            if rated_kw <= 0:
                raise ValidationError("负荷功率必须为正", details={"rated_kw": rated_kw})
            if restore_order < 0:
                raise ValidationError("复电顺序不能为负", details={"restore_order": restore_order})
            existing = self._find(load_id)
            if existing is not None:
                prospective = [
                    load for load in self._loads if load["load_id"] != load_id
                ]
            else:
                prospective = list(self._loads)
            prospective.append(
                {
                    "load_id": load_id,
                    "label": label,
                    "tier": tier,
                    "rated_kw": float(rated_kw),
                    "restore_order": int(restore_order),
                    "on": existing["on"] if existing is not None else self._machine.state == "grid",
                    "last_action": existing["last_action"] if existing is not None else None,
                    "last_changed_at": existing["last_changed_at"] if existing is not None else None,
                }
            )
            self._require_security_capacity(prospective)
            self._loads = prospective
            self._log_event(
                "register",
                actor,
                load_id=load_id,
                label=label,
                tier=tier,
                detail={"rated_kw": float(rated_kw), "restore_order": int(restore_order)},
            )
            record = self._persist(reason="register_load")
            trace.attach(record).note("load_id", load_id).note("tier", tier)
            return self.status()

    def unregister_load(
        self,
        actor: str,
        *,
        load_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "unregister_load",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            load = self._require(load_id)
            if load["on"]:
                raise GuardViolation("负荷仍带电，禁止注销", details={"load_id": load_id})
            if self._machine.state != "grid":
                raise GuardViolation(
                    "非市电正常状态，禁止注销负荷",
                    details={"load_id": load_id, "state": self._machine.state},
                )
            self._loads = [entry for entry in self._loads if entry["load_id"] != load_id]
            self._log_event(
                "unregister", actor, load_id=load["load_id"], label=load["label"], tier=load["tier"]
            )
            record = self._persist(reason="unregister_load")
            trace.attach(record).note("load_id", load_id)
            return self.status()

    # ------------------------------------------------------------------ 失电
    def grid_loss(
        self,
        actor: str,
        *,
        cause: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "grid_loss",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require_one_of(("grid", "restoring"), "失电切负荷")
            cause = (cause or "市电失电").strip() or "市电失电"
            intent = self.write_intent(
                "grid_loss",
                {
                    "action": "grid_loss",
                    "cause": cause,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._close_episode(outcome="interrupted")
            self._episode_seq += 1
            episode_id = f"PE-{self._episode_seq:04d}"
            self._episode = {
                "episode_id": episode_id,
                "cause": cause,
                "lost_at": self.clock.timestamp_iso(),
                "grid_back_at": None,
                "restored_at": None,
                "shed_loads": [],
            }
            self._log_event("grid_loss", actor, detail={"cause": cause})
            # 按级切负荷：三级先切、二级后切；同级越晚复电的越先切。一级保电。
            for tier in (3, 2):
                staged = sorted(
                    (load for load in self._loads if load["tier"] == tier and load["on"]),
                    key=lambda load: (-load["restore_order"], load["load_id"]),
                )
                for load in staged:
                    self._switch_load(load, on=False, action="shed")
                    self._episode["shed_loads"].append(load["load_id"])
                    self._log_event(
                        "shed", actor, load_id=load["load_id"], label=load["label"], tier=load["tier"]
                    )
            if self._gen.state == "running":
                # 复电途中再次失电：柴发已在保安段上，直接回到应急供电。
                self._machine.to("emergency", actor, "复电途中再次失电，柴发继续带保安段")
            else:
                if self._gen.state == "standby":
                    self._gen.to("starting", actor, "失电自启动")
                    self._gen_started_at = self.clock.timestamp()
                    self._log_event("gen_start", actor)
                self._machine.to("shedding", actor, "市电失电，按级切负荷")
            self._grid_back_at = None
            self._last_restore_at = None
            record = self._persist(reason="grid_loss")
            trace.attach(record).note("episode_id", episode_id).note("intent_version", intent.version)
            return self.status()

    def generator_running(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """柴发并网、保安段带电（现场由自投装置给出，这里是一个确认动作）。"""

        actor = ensure_actor(actor)
        with self.action(
            "generator_running",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("shedding", "柴发并网")
            self._gen.require("starting", "柴发并网")
            elapsed = self.clock.timestamp() - float(self._gen_started_at or 0.0)
            remaining = self.settings.power_generator_start_seconds - elapsed
            if remaining > 0:
                raise GuardViolation(
                    "柴发启动时限未到，禁止并网",
                    details={"remaining_seconds": round(remaining, 3)},
                )
            self._gen.to("running", actor, "柴发并网，保安段带电")
            self._gen_running_at = self.clock.timestamp()
            self._gen_run_count += 1
            self._machine.to("emergency", actor, "柴发带上保安段")
            self._log_event("gen_running", actor, detail={"security_bus_kw": self._security_bus_kw()})
            record = self._persist(reason="generator_running")
            trace.attach(record)
            return self.status()

    # ------------------------------------------------------------------ 复电
    def grid_restored(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "grid_restored",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require_one_of(("shedding", "emergency"), "市电复电")
            if self._gen.state == "starting":
                # 晃电：柴发还没并网店就回来了，取消启动回备用。
                self._gen.to("standby", actor, "市电快速恢复，取消启动")
                self._gen_started_at = None
                self._log_event("gen_abort", actor)
            self._machine.to("restoring", actor, "市电恢复，进入确认窗口")
            self._grid_back_at = self.clock.timestamp()
            self._last_restore_at = None
            if self._episode is not None:
                self._episode["grid_back_at"] = self.clock.timestamp_iso()
            self._log_event("grid_back", actor)
            record = self._persist(reason="grid_restored")
            trace.attach(record)
            return self.status()

    def restore_next(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """按复电顺序送下一台；送完最后一台自动转市电、柴发转冷却。"""

        actor = ensure_actor(actor)
        with self.action(
            "restore_next",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("restoring", "逐台复电")
            self._require_restore_window()
            pending = self._pending_restore()
            if not pending:
                raise StateTransitionError("没有待复电负荷", details={"state": self._machine.state})
            load = pending[0]
            self._switch_load(load, on=True, action="restore")
            self._log_event(
                "restore", actor, load_id=load["load_id"], label=load["label"], tier=load["tier"]
            )
            self._finish_restore_if_done(actor)
            record = self._persist(reason="restore_next")
            trace.attach(record).note("load_id", load["load_id"])
            return self.status()

    def generator_standby(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "generator_standby",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._gen.require("cooldown", "柴发停机回备")
            elapsed = self.clock.timestamp() - float(self._gen_cooldown_at or 0.0)
            remaining = self.settings.power_generator_cooldown_seconds - elapsed
            if remaining > 0:
                raise GuardViolation(
                    "柴发冷却时长未到，禁止停机",
                    details={"remaining_seconds": round(remaining, 3)},
                )
            self._gen.to("standby", actor, "冷却完成，停机回备")
            self._gen_started_at = None
            self._gen_running_at = None
            self._gen_cooldown_at = None
            self._log_event("gen_standby", actor)
            record = self._persist(reason="generator_standby")
            trace.attach(record)
            return self.status()

    # ------------------------------------------------------------------ 单台操作
    def shed_load(
        self,
        actor: str,
        *,
        load_id: str,
        reason: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "shed_load",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            load = self._require(load_id)
            if load["tier"] == 1:
                raise GuardViolation("保安负荷禁止切除", details={"load_id": load_id})
            if not load["on"]:
                raise StateTransitionError("负荷已切除", details={"load_id": load_id})
            reason = (reason or "").strip()
            if not reason:
                raise ValidationError("手动切负荷必须给出原因", details={"load_id": load_id})
            self._switch_load(load, on=False, action="shed")
            self._log_event(
                "shed",
                actor,
                load_id=load["load_id"],
                label=load["label"],
                tier=load["tier"],
                detail={"manual": True, "reason": reason},
            )
            record = self._persist(reason="shed_load")
            trace.attach(record).note("load_id", load_id)
            return self.status()

    def restore_load(
        self,
        actor: str,
        *,
        load_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "restore_load",
            "power",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            load = self._require(load_id)
            if load["on"]:
                raise StateTransitionError("负荷已带电", details={"load_id": load_id})
            state = self._machine.state
            if state == "shedding":
                raise GuardViolation("失电切负荷中，禁止送电", details={"load_id": load_id})
            if state == "emergency":
                if load["tier"] != 1:
                    raise GuardViolation(
                        "柴发只带保安段，非保安负荷禁止送上",
                        details={"load_id": load_id, "tier": load["tier"]},
                    )
                prospective = self._security_bus_kw(extra=load)
                if prospective > self.settings.power_generator_capacity_kw:
                    raise GuardViolation(
                        "保安段负荷超过柴发容量",
                        details={
                            "load_id": load_id,
                            "security_bus_kw": prospective,
                            "capacity_kw": self.settings.power_generator_capacity_kw,
                        },
                    )
            if state == "restoring":
                self._require_restore_window()
            self._switch_load(load, on=True, action="restore")
            self._log_event(
                "restore",
                actor,
                load_id=load["load_id"],
                label=load["label"],
                tier=load["tier"],
                detail={"manual": True},
            )
            self._finish_restore_if_done(actor)
            record = self._persist(reason="restore_load")
            trace.attach(record).note("load_id", load_id)
            return self.status()

    # ------------------------------------------------------------------ 查询
    def status(self) -> Mapping[str, Any]:
        loads = sorted(self._loads, key=lambda load: (load["tier"], load["restore_order"], load["load_id"]))
        return {
            "state": self._machine.state,
            "generator": {
                "state": self._gen.state,
                "capacity_kw": self.settings.power_generator_capacity_kw,
                "started_at": None
                if self._gen_started_at is None
                else self._iso(self._gen_started_at),
                "running_at": None
                if self._gen_running_at is None
                else self._iso(self._gen_running_at),
                "run_count": self._gen_run_count,
            },
            "loads": [dict(load) for load in loads],
            "on_count": sum(1 for load in self._loads if load["on"]),
            "off_count": sum(1 for load in self._loads if not load["on"]),
            "security_bus": {
                "load_kw": self._security_bus_kw(),
                "capacity_kw": self.settings.power_generator_capacity_kw,
            },
            "pending_restore": [load["load_id"] for load in self._pending_restore()],
            "episode": None if self._episode is None else dict(self._episode),
            "episodes": [dict(entry) for entry in self._episodes],
            "recent_events": self.events(limit=20),
            "history": list(self._machine.history),
        }

    def events(self, *, limit: int = 50, episode_id: str | None = None) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        entries = self.store.read_stream(POWER_STREAM, limit=max(limit * 4, 100))
        events = []
        for entry in entries:
            payload = dict(entry.payload)
            if episode_id is not None and payload.get("episode_id") != episode_id:
                continue
            payload["seq"] = entry.seq
            events.append(payload)
        return events[-limit:]

    # ------------------------------------------------------------------ 内部
    def _seed_default_loads(self) -> None:
        self._loads = [
            {
                "load_id": item["load_id"],
                "label": item["label"],
                "tier": item["tier"],
                "rated_kw": float(item["rated_kw"]),
                "restore_order": int(item["restore_order"]),
                "on": True,
                "last_action": None,
                "last_changed_at": None,
            }
            for item in DEFAULT_LOADS
        ]

    def _validate_load_id(self, load_id: str) -> str:
        load_id = (load_id or "").strip()
        if not _LOAD_ID_PATTERN.match(load_id):
            raise ValidationError(
                "负荷编号不合法",
                details={"load_id": load_id, "expected": "字母数字开头，可含 - 与 _，最长 32 位"},
            )
        return load_id

    def _validate_label(self, label: str) -> str:
        label = (label or "").strip()
        if not label:
            raise ValidationError("负荷名称不能为空")
        if len(label) > 64:
            raise ValidationError("负荷名称超长", details={"length": len(label), "max": 64})
        return label

    def _find(self, load_id: str) -> dict[str, Any] | None:
        for load in self._loads:
            if load["load_id"] == load_id:
                return load
        return None

    def _require(self, load_id: str) -> dict[str, Any]:
        load = self._find(load_id)
        if load is None:
            raise ValidationError("未知负荷", details={"load_id": load_id})
        return load

    def _security_bus_kw(self, *, extra: dict[str, Any] | None = None) -> float:
        total = sum(load["rated_kw"] for load in self._loads if load["tier"] == 1 and load["on"])
        if extra is not None and extra["tier"] == 1:
            total += extra["rated_kw"]
        return round(total, 3)

    def _require_security_capacity(self, loads: list[dict[str, Any]]) -> None:
        total = sum(load["rated_kw"] for load in loads if load["tier"] == 1)
        if total > self.settings.power_generator_capacity_kw:
            raise GuardViolation(
                "一级负荷总和超过柴发容量",
                details={
                    "security_bus_kw": round(total, 3),
                    "capacity_kw": self.settings.power_generator_capacity_kw,
                },
            )

    def _pending_restore(self) -> list[dict[str, Any]]:
        return sorted(
            (load for load in self._loads if not load["on"]),
            key=lambda load: (load["restore_order"], load["load_id"]),
        )

    def _require_restore_window(self) -> None:
        now = self.clock.timestamp()
        confirm_remaining = self.settings.power_grid_confirm_seconds - (
            now - float(self._grid_back_at or 0.0)
        )
        if confirm_remaining > 0:
            raise GuardViolation(
                "市电确认窗口未到，暂不复电",
                details={"remaining_seconds": round(confirm_remaining, 3)},
            )
        if self._last_restore_at is not None:
            step_remaining = self.settings.power_restore_step_seconds - (
                now - float(self._last_restore_at)
            )
            if step_remaining > 0:
                raise GuardViolation(
                    "逐台送电间隔未到",
                    details={"remaining_seconds": round(step_remaining, 3)},
                )

    def _switch_load(self, load: dict[str, Any], *, on: bool, action: str) -> None:
        load["on"] = on
        load["last_action"] = action
        load["last_changed_at"] = self.clock.timestamp_iso()
        if action == "restore":
            self._last_restore_at = self.clock.timestamp()

    def _finish_restore_if_done(self, actor: str) -> None:
        if self._machine.state != "restoring" or self._pending_restore():
            return
        self._machine.to("grid", actor, "全部负荷复电完成")
        if self._gen.state == "running":
            self._gen.to("cooldown", actor, "负荷全部转回市电，柴发转冷却")
            self._gen_cooldown_at = self.clock.timestamp()
            self._log_event("gen_cooldown", actor)
        self._close_episode(outcome="restored")

    def _close_episode(self, *, outcome: str) -> None:
        if self._episode is None:
            return
        episode = dict(self._episode)
        episode["outcome"] = outcome
        if outcome == "restored":
            episode["restored_at"] = self.clock.timestamp_iso()
        self._episodes.append(episode)
        self._episodes = self._episodes[-self.settings.power_episode_history :]
        self._episode = None

    def _log_event(
        self,
        kind: str,
        actor: str,
        *,
        load_id: str | None = None,
        label: str | None = None,
        tier: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "episode_id": None if self._episode is None else self._episode["episode_id"],
            "kind": kind,
            "at": self.clock.timestamp_iso(),
            "actor": actor,
            "namespace": self.namespace.prefix,
        }
        if load_id is not None:
            payload["load_id"] = load_id
        if label is not None:
            payload["label"] = label
        if tier is not None:
            payload["tier"] = tier
        if detail:
            payload["detail"] = dict(detail)
        self.store.append(POWER_STREAM, payload)

    def _iso(self, epoch: float) -> str:
        from ..runtime import iso_from_epoch

        return iso_from_epoch(epoch)

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "history": list(self._machine.history),
            "generator": self._gen.to_dict(),
            "reason": reason,
            "written_at": self.clock.timestamp_iso(),
            "loads": [dict(load) for load in self._loads],
            "episode_seq": self._episode_seq,
            "episode": None if self._episode is None else dict(self._episode),
            "episodes": [dict(entry) for entry in self._episodes],
            "gen_started_at": self._gen_started_at,
            "gen_running_at": self._gen_running_at,
            "gen_cooldown_at": self._gen_cooldown_at,
            "gen_run_count": self._gen_run_count,
            "grid_back_at": self._grid_back_at,
            "last_restore_at": self._last_restore_at,
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("power.loads_off", float(sum(1 for load in self._loads if not load["on"])))
        self.metrics.observe("power.security_bus_kw", self._security_bus_kw())
        self.metrics.observe("power.outage_count", float(self._episode_seq))


__all__ = ["PowerSupply", "STATES", "TRANSITIONS", "GEN_STATES", "GEN_TRANSITIONS", "TIERS", "DEFAULT_LOADS"]
