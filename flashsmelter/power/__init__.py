"""供电与保安电源系统。

把「晃电/停电时保谁、先切谁，复电时先送谁」从值班临场判断变成确定性的分级
逻辑，驱动方式是周期扫描（现场由 SCADA 按固定节拍调用 ``scan`` 上报电网电压）：

* 晃电（电压暂降）：进入 ``sag``，非保负荷按 可拉闸→一般→重要 顺序跳开，保安
  负荷由 UPS/直流与机械惯性能维持；电压在判据时长内恢复则按自启动批次分批送回，
  超过判据时长升级为停电。
* 停电：进入 ``blackout``，柴油发电机自启动，启动完成后 ATS 投合保安母线，按
  容量带上保安负荷；发电机启动失败进入 ``genset_failed``，故障消除或人工复位后
  重试。
* 复电：电网电压恢复并持续稳定确认后切回市电（``restoring``），按 ``restore_order``
  一台一台送电，手动送电类负荷不自动恢复，必须人工确认。
* 全过程写入追加型电源台账（``power/ledger``），每台设备每次停、送都有事件。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, NotFoundError, StateTransitionError, ValidationError
from ..machine import StateMachine
from ..runtime import RuntimeContext
from ..store import JournalEntry
from .loads import (
    GRADES,
    GRADE_LABELS,
    SAG_POLICY_LABELS,
    SHED_ORDER,
    default_catalog,
    validate_spec,
)

STATES = ("idle", "sag", "blackout", "genset", "genset_failed", "restoring", "testing")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("sag", "blackout", "testing"),
    "sag": ("idle", "blackout"),
    "blackout": ("genset", "genset_failed"),
    "genset": ("restoring",),
    "genset_failed": ("blackout", "restoring"),
    "restoring": ("idle", "blackout"),
    "testing": ("idle", "blackout"),
}

LEDGER_STREAM = "power/ledger"

# 电源台账事件类型。
EV_SAG_ENTER = "sag-enter"
EV_SAG_EXIT = "sag-exit"
EV_BLACKOUT_ENTER = "blackout-enter"
EV_LOAD_SHED = "load-shed"
EV_LOAD_ENERGIZED = "load-energized"
EV_GENSET_START = "genset-start"
EV_GENSET_FAILED = "genset-failed"
EV_ATS_CLOSE = "ats-close"
EV_ATS_OPEN = "ats-open"
EV_GENSET_STOP = "genset-stop"
EV_GRID_RETURN = "grid-return"
EV_RESTORE_BEGIN = "restore-begin"
EV_RESTORE_COMPLETE = "restore-complete"
EV_MANUAL_CUT = "manual-cut"
EV_MANUAL_FEED = "manual-feed"
EV_GENSET_TEST_START = "genset-test-start"
EV_GENSET_TEST_END = "genset-test-end"
EV_GENSET_OVERLOAD = "genset-overload-block"

# 停电期间各事件的来源去向。
SOURCE_GRID = "grid"
SOURCE_UPS = "ups"
SOURCE_GENSET = "genset"
SOURCE_NONE = "none"


class PowerSystem(Component):
    """供电、保安电源与负荷分级调度组件。"""

    name = "power"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("power", "idle", TRANSITIONS, ctx.clock)
        self._catalog: dict[str, dict[str, Any]] = {}
        self._energized: dict[str, bool] = {}
        self._blocked: set[str] = set()
        self._grid_voltage = 1.0
        self._incident: dict[str, Any] | None = None
        self._genset_state = "stopped"
        self._ats_closed = False
        self._genset_ready_at: float | None = None
        self._genset_failure_count = 0
        self._genset_last_start_at: str | None = None
        self._genset_last_test_at: str | None = None
        self._last_failure_at: float | None = None
        self._pending: list[list[Any]] = []  # [ready_epoch, device_id]，晃电分批自启动
        self._restore_queue: list[str] = []
        self._restore_next_at: float | None = None
        self._grid_stable_since: float | None = None
        self._sag_entered_epoch: float | None = None
        self._blackout_seq = 0
        self._sag_seq = 0
        restored = self.restore()
        if restored is not None:
            self._load_from_payload(restored)
        else:
            for spec in default_catalog():
                self._catalog[spec["device_id"]] = spec
                self._energized[spec["device_id"]] = True
            self._validate_capacity()
            record = self._persist(reason="catalog-seeded")
            self._refresh_gauges()
            self._last_seed_record = record

    # ------------------------------------------------------------------ 扫描
    def scan(
        self,
        actor: str,
        *,
        grid_voltage: float,
        genset_fault: bool = False,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """周期上报电网电压并推进失电/复电状态机。"""

        actor = ensure_actor(actor)
        try:
            voltage = float(grid_voltage)
        except (TypeError, ValueError) as exc:
            raise ValidationError("电网电压必须是数值", details={"grid_voltage": repr(grid_voltage)}) from exc
        if voltage != voltage or not 0.0 <= voltage <= 1.5:
            raise ValidationError("电网电压标幺值必须在 [0,1.5]", details={"grid_voltage": voltage})
        with self.action("scan", "power", actor, correlation_id=correlation_id) as trace:
            self._grid_voltage = voltage
            state = self._machine.state
            if state == "testing":
                if voltage <= self.settings.power_voltage_lost_pu:
                    self._enter_blackout("grid-lost-during-test", new_incident=True, genset_already_running=True)
                trace.note("state", self._machine.state)
                self._persist(reason="scan")
                return self.status()
            if state == "idle":
                self._process_pending(now=self.clock.timestamp())
                if voltage <= self.settings.power_voltage_lost_pu:
                    self._enter_blackout("grid-lost", new_incident=True)
                elif voltage < self.settings.power_voltage_sag_pu:
                    self._enter_sag("grid-undervoltage")
                trace.note("state", self._machine.state)
                self._persist(reason="scan")
                return self.status()
            if state == "sag":
                if voltage <= self.settings.power_voltage_lost_pu:
                    self._enter_blackout("grid-lost-escalation", new_incident=True)
                elif voltage < self.settings.power_voltage_sag_pu:
                    elapsed = self.clock.timestamp() - float(self._sag_entered_epoch)
                    if elapsed >= self.settings.power_sag_to_blackout_seconds:
                        self._enter_blackout("sag-timeout", new_incident=True)
                elif voltage >= self.settings.power_voltage_return_pu:
                    self._exit_sag_recovery()
                trace.note("state", self._machine.state)
                self._persist(reason="scan")
                return self.status()
            if state == "blackout":
                now = self.clock.timestamp()
                if genset_fault:
                    self._genset_failure("genset-start-fault")
                elif voltage >= self.settings.power_voltage_return_pu and self._grid_confirmed(now):
                    self._begin_restoring("grid-recovered-during-start")
                elif self._genset_ready_at is not None and now >= float(self._genset_ready_at):
                    self._genset_carry_load()
                trace.note("state", self._machine.state)
                self._persist(reason="scan")
                return self.status()
            if state == "genset_failed":
                now = self.clock.timestamp()
                if voltage >= self.settings.power_voltage_return_pu and self._grid_confirmed(now):
                    self._begin_restoring("grid-recovered-after-failure")
                elif not genset_fault and self._retry_cooldown_done(now):
                    self._machine.to("blackout", actor, "发电机故障消除，重新启动")
                    self._genset_state = "starting"
                    self._genset_ready_at = now + self.settings.power_genset_start_seconds
                    self._emit(None, EV_GENSET_START, reason="retry-after-failure", actor=actor)
                trace.note("state", self._machine.state)
                self._persist(reason="scan")
                return self.status()
            if state == "genset":
                now = self.clock.timestamp()
                if voltage >= self.settings.power_voltage_return_pu:
                    if self._grid_confirmed(now):
                        self._begin_restoring("grid-stable-return")
                else:
                    self._grid_stable_since = None
                trace.note("state", self._machine.state)
                self._persist(reason="scan")
                return self.status()
            if state == "restoring":
                if voltage < self.settings.power_voltage_sag_pu:
                    self._enter_blackout("restore-interrupted", new_incident=True)
                else:
                    self._advance_restore(actor=actor, now=self.clock.timestamp())
                    if not self._restore_queue and not self._pending:
                        self._finish_restore()
                trace.note("state", self._machine.state)
                self._persist(reason="scan")
                return self.status()
            raise StateTransitionError("供电系统处于未处理状态", details={"state": state})

    # ------------------------------------------------------------- 台账维护
    def register_load(
        self,
        actor: str,
        *,
        device_id: str,
        name: str,
        grade: str,
        kw: float,
        sag_policy: str,
        source: str,
        sag_group: int = 0,
        restore_order: int,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "register_load", "power", actor,
            correlation_id=correlation_id, expected_generation=expected_generation,
        ) as trace:
            spec = validate_spec(
                {
                    "device_id": device_id,
                    "name": name,
                    "grade": grade,
                    "kw": kw,
                    "sag_policy": sag_policy,
                    "source": source,
                    "sag_group": sag_group,
                    "restore_order": restore_order,
                }
            )
            if spec["device_id"] in self._catalog:
                raise ValidationError("设备已登记", details={"device_id": device_id})
            self._catalog[spec["device_id"]] = spec
            # 新设备默认无电，必须人工确认送电，避免台账变更悄悄改变母线负荷。
            self._energized[spec["device_id"]] = False
            self._blocked.add(spec["device_id"])
            self._validate_capacity()
            record = self._persist(reason="register-load")
            trace.attach(record).note("device_id", spec["device_id"]).note("grade", spec["grade"])
            return self.status()

    def cut(
        self,
        actor: str,
        *,
        device_id: str,
        reason: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """人工拉闸：设备断电并标记为人工停用，自动复电不会带上它。"""

        actor = ensure_actor(actor)
        if not reason or not reason.strip():
            raise ValidationError("人工拉闸必须填写原因")
        with self.action(
            "cut", device_id, actor,
            correlation_id=correlation_id, expected_generation=expected_generation,
        ) as trace:
            spec = self._require_device(device_id)
            self._blocked.add(device_id)
            self._pending = [item for item in self._pending if item[1] != device_id]
            if device_id in self._restore_queue:
                self._restore_queue.remove(device_id)
            if self._energized.get(device_id):
                self._shed(spec, reason=f"manual-cut:{reason.strip()}", actor=actor, event=EV_MANUAL_CUT)
            else:
                self._emit(device_id, EV_MANUAL_CUT, reason=f"manual-cut:{reason.strip()}", actor=actor)
            record = self._persist(reason="manual-cut")
            trace.attach(record).note("name", spec["name"])
            return self.status()

    def feed(
        self,
        actor: str,
        *,
        device_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """人工送电：解除人工停用并在当前可用电源下送电。"""

        actor = ensure_actor(actor)
        with self.action(
            "feed", device_id, actor,
            correlation_id=correlation_id, expected_generation=expected_generation,
        ) as trace:
            spec = self._require_device(device_id)
            state = self._machine.state
            if state in ("blackout", "genset_failed"):
                raise GuardViolation(
                    "停电且保安电源尚未带上，禁止人工送电", details={"state": state, "device_id": device_id}
                )
            if state == "sag" and spec["grade"] != "security":
                raise GuardViolation("晃电未解除，非保安负荷禁止送电", details={"device_id": device_id})
            self._blocked.discard(device_id)
            if state == "genset":
                if spec["source"] not in ("genset", "ups"):
                    raise GuardViolation(
                        "市电未恢复，市电负荷无法送电",
                        details={"device_id": device_id, "source": spec["source"]},
                    )
                self._feed_secure(spec, reason="manual-feed", actor=actor, event=EV_MANUAL_FEED)
            else:
                self._feed(spec, SOURCE_GRID, reason="manual-feed", actor=actor, event=EV_MANUAL_FEED)
            self._pending = [item for item in self._pending if item[1] != device_id]
            record = self._persist(reason="manual-feed")
            trace.attach(record).note("name", spec["name"])
            return self.status()

    def restore_now(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """跳过等待节拍，立即推进所有待送批次/队列（仍按既定顺序逐台送）。"""

        actor = ensure_actor(actor)
        with self.action("restore_now", "power", actor, correlation_id=correlation_id) as trace:
            state = self._machine.state
            if state == "idle":
                self._process_pending(now=float("inf"))
                if not self._pending and self._incident and self._incident.get("phase") == "sag-recovering":
                    self._incident = None
            elif state == "restoring":
                while self._restore_queue:
                    self._advance_restore(actor=actor, now=float("inf"))
                self._finish_restore()
            else:
                raise GuardViolation("当前状态没有待推进的复电队列", details={"state": state})
            record = self._persist(reason="restore-now")
            trace.attach(record).note("state", self._machine.state)
            return self.status()

    def genset_test(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """发电机空载试机：只验证自启动，不切换 ATS；试机期间真停电立即转保安。"""

        actor = ensure_actor(actor)
        with self.action(
            "genset_test", "power", actor,
            correlation_id=correlation_id, expected_generation=expected_generation,
        ) as trace:
            if self._machine.state != "idle":
                raise GuardViolation("只有市电正常运行时允许试机", details={"state": self._machine.state})
            self._machine.to("testing", actor, "柴油发电机空载试机")
            self._genset_state = "test-running"
            self._genset_last_test_at = self.clock.timestamp_iso()
            self._emit(None, EV_GENSET_TEST_START, reason="scheduled-test", actor=actor)
            record = self._persist(reason="genset-test-start")
            trace.attach(record)
            return self.status()

    def genset_test_end(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "genset_test_end", "power", actor,
            correlation_id=correlation_id, expected_generation=expected_generation,
        ) as trace:
            self._machine.require("testing", "结束试机")
            self._machine.to("idle", actor, "试机结束")
            self._genset_state = "stopped"
            self._emit(None, EV_GENSET_TEST_END, reason="test-finished", actor=actor)
            record = self._persist(reason="genset-test-end")
            trace.attach(record)
            return self.status()

    def reset(
        self,
        actor: str,
        *,
        note: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """发电机启动失败后的人工复位重试，必须填写处理说明。"""

        actor = ensure_actor(actor)
        if not note or not note.strip():
            raise ValidationError("复位必须填写处理说明")
        with self.action(
            "reset", "power", actor,
            correlation_id=correlation_id, expected_generation=expected_generation,
        ) as trace:
            self._machine.require("genset_failed", "发电机故障复位")
            now = self.clock.timestamp()
            self._machine.to("blackout", actor, f"故障复位重试：{note.strip()}")
            self._genset_state = "starting"
            self._genset_ready_at = now + self.settings.power_genset_start_seconds
            self._emit(None, EV_GENSET_START, reason=f"manual-reset:{note.strip()}", actor=actor)
            record = self._persist(reason="reset")
            trace.attach(record).note("note", note)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def loads(self) -> list[Mapping[str, Any]]:
        items = []
        for device_id in sorted(self._catalog, key=lambda d: self._catalog[d]["restore_order"]):
            spec = self._catalog[device_id]
            items.append(
                {
                    **spec,
                    "grade_label": GRADE_LABELS[spec["grade"]],
                    "sag_policy_label": SAG_POLICY_LABELS[spec["sag_policy"]],
                    "energized": bool(self._energized.get(device_id)),
                    "blocked": device_id in self._blocked,
                }
            )
        return items

    def grade_summary(self) -> Mapping[str, Any]:
        summary: dict[str, dict[str, Any]] = {
            grade: {"grade": grade, "label": GRADE_LABELS[grade], "total_kw": 0.0, "devices": 0,
                    "energized": 0, "de_energized": 0}
            for grade in GRADES
        }
        for device_id, spec in self._catalog.items():
            row = summary[spec["grade"]]
            row["total_kw"] += spec["kw"]
            row["devices"] += 1
            if self._energized.get(device_id):
                row["energized"] += 1
            else:
                row["de_energized"] += 1
        for row in summary.values():
            row["total_kw"] = round(row["total_kw"], 3)
        return {grade: summary[grade] for grade in GRADES}

    def genset_status(self) -> Mapping[str, Any]:
        load_kw = self._genset_load_kw() if self._ats_closed else 0.0
        return {
            "state": self._genset_state,
            "ats_closed": self._ats_closed,
            "capacity_kw": self.settings.power_genset_capacity_kw,
            "load_kw": round(load_kw, 3),
            "headroom_kw": round(self.settings.power_genset_capacity_kw - load_kw, 3),
            "ready_at_epoch": self._genset_ready_at,
            "failure_count": self._genset_failure_count,
            "last_start_at": self._genset_last_start_at,
            "last_test_at": self._genset_last_test_at,
        }

    def ledger(
        self,
        *,
        limit: int = 100,
        incident_id: str | None = None,
        device_id: str | None = None,
        event: str | None = None,
    ) -> list[Mapping[str, Any]]:
        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        entries = self.store.read_stream(LEDGER_STREAM, limit=max(limit * 8, 200))
        events = [self._ledger_to_dict(entry) for entry in entries]
        filtered = [
            item
            for item in events
            if (incident_id is None or item.get("incident_id") == incident_id)
            and (device_id is None or item.get("device_id") == device_id)
            and (event is None or item.get("event") == event)
        ]
        return filtered[-limit:]

    def status(self) -> Mapping[str, Any]:
        energized_count = sum(1 for value in self._energized.values() if value)
        return {
            "state": self._machine.state,
            "grid": {
                "voltage": round(self._grid_voltage, 4),
                "sag_pu": self.settings.power_voltage_sag_pu,
                "return_pu": self.settings.power_voltage_return_pu,
                "lost_pu": self.settings.power_voltage_lost_pu,
            },
            "incident": dict(self._incident) if self._incident else None,
            "genset": self.genset_status(),
            "loads": self.loads(),
            "grades": self.grade_summary(),
            "counts": {
                "devices": len(self._catalog),
                "energized": energized_count,
                "de_energized": len(self._catalog) - energized_count,
                "blocked": len(self._blocked),
                "sag_events": self._sag_seq,
                "blackout_events": self._blackout_seq,
            },
            "pending_restarts": [[item[0], item[1]] for item in self._pending],
            "restore_queue": list(self._restore_queue),
            "restore_next_at_epoch": self._restore_next_at,
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 晃电
    def _enter_sag(self, reason: str) -> None:
        self._machine.to("sag", "protection", reason)
        self._sag_seq += 1
        self._sag_entered_epoch = self.clock.timestamp()
        self._incident = {
            "id": f"SAG-{self._sag_seq:04d}",
            "kind": "sag",
            "phase": "sag",
            "reason": reason,
            "started_at": self.clock.timestamp_iso(),
            "started_epoch": self._sag_entered_epoch,
        }
        self._emit(None, EV_SAG_ENTER, reason=reason, actor="protection")
        # 非保安负荷一律跳开，顺序：可拉闸 → 一般 → 重要；同级别内按复电序倒序，
        # 保证最不重要的负荷最先断开。
        for grade in SHED_ORDER:
            for spec in sorted(
                (s for s in self._catalog.values() if s["grade"] == grade),
                key=lambda s: -s["restore_order"],
            ):
                if self._energized.get(spec["device_id"]):
                    self._shed(spec, reason="undervoltage-sag", actor="protection")

    def _exit_sag_recovery(self) -> None:
        now = self.clock.timestamp()
        self._machine.to("idle", "protection", "电压恢复，晃电解除")
        assert self._incident is not None
        self._incident["phase"] = "sag-recovering"
        self._emit(None, EV_SAG_EXIT, reason="voltage-recovered", actor="protection")
        # restart 策略按 sag_group 分批自启动；manual 策略必须人工送电。
        restartable = [
            spec
            for spec in self._sorted_loads()
            if spec["sag_policy"] == "restart"
            and spec["device_id"] not in self._blocked
            and not self._energized.get(spec["device_id"])
        ]
        restartable.sort(key=lambda s: (s["sag_group"], s["restore_order"]))
        for spec in restartable:
            ready = now + (spec["sag_group"] - 1) * self.settings.power_sag_group_delay_seconds
            self._pending.append([ready, spec["device_id"]])
        self._process_pending(now=now)

    def _process_pending(self, *, now: float) -> None:
        due = [item for item in self._pending if item[0] <= now]
        if not due:
            return
        self._pending = [item for item in self._pending if item[0] > now]
        for _, device_id in sorted(due, key=lambda item: item[0]):
            spec = self._catalog.get(device_id)
            if spec is None or device_id in self._blocked:
                continue
            if self._machine.state != "idle":
                # 状态中途变化（理论上不会发生），重新挂回等待人工处理。
                continue
            self._feed(spec, SOURCE_GRID, reason="sag-auto-restart", actor="protection")
        if not self._pending and self._incident and self._incident.get("phase") == "sag-recovering":
            self._incident = None

    # ------------------------------------------------------------------ 停电
    def _enter_blackout(self, reason: str, *, new_incident: bool, genset_already_running: bool = False) -> None:
        previous = self._machine.state
        if previous != "blackout":
            self._machine.to("blackout", "protection", reason)
        now = self.clock.timestamp()
        if new_incident:
            self._blackout_seq += 1
            self._incident = {
                "id": f"OUT-{self._blackout_seq:04d}",
                "kind": "blackout",
                "phase": "blackout",
                "reason": reason,
                "started_at": self.clock.timestamp_iso(),
                "started_epoch": now,
            }
            self._emit(None, EV_BLACKOUT_ENTER, reason=reason, actor="protection")
        # 中断任何尚未完成的复电安排。
        self._pending = []
        self._restore_queue = []
        self._restore_next_at = None
        self._grid_stable_since = None
        # 非保安负荷确保全部断开，按级别顺序逐条记账。
        for grade in SHED_ORDER:
            for spec in sorted(
                (s for s in self._catalog.values() if s["grade"] == grade),
                key=lambda s: -s["restore_order"],
            ):
                if self._energized.get(spec["device_id"]):
                    self._shed(spec, reason=reason, actor="protection")
        # 发电机保安母线上的负荷先断开，等 ATS 投合后带上；UPS/直流负荷不间断。
        for spec in self._sorted_loads():
            if spec["grade"] == "security" and spec["source"] == "genset":
                if self._energized.get(spec["device_id"]):
                    self._shed(spec, reason="awaiting-genset", actor="protection")
        self._ats_closed = False
        if genset_already_running and self._genset_state in ("running", "test-running"):
            self._genset_state = "running"
            self._genset_ready_at = now
            self._genset_last_start_at = self.clock.timestamp_iso()
            self._emit(None, EV_GENSET_START, reason="already-running-from-test", actor="protection")
        elif previous == "testing":  # pragma: no cover - 防御
            self._genset_state = "running"
            self._genset_ready_at = now
        else:
            self._genset_state = "starting"
            self._genset_ready_at = now + self.settings.power_genset_start_seconds
            self._genset_last_start_at = self.clock.timestamp_iso()
            self._emit(None, EV_GENSET_START, reason=reason, actor="protection")

    def _genset_carry_load(self) -> None:
        self._machine.to("genset", "protection", "发电机启动完成，ATS 投合保安母线")
        self._genset_state = "running"
        self._ats_closed = True
        assert self._incident is not None
        self._incident["phase"] = "genset"
        self._emit(None, EV_ATS_CLOSE, reason="genset-ready", actor="protection")
        # 按复电顺序带保安负荷，容量不足的留在断开状态并记账。
        capacity = self.settings.power_genset_capacity_kw
        loaded = 0.0
        for spec in self._sorted_loads():
            if spec["grade"] != "security" or spec["source"] != "genset":
                continue
            if spec["device_id"] in self._blocked:
                continue
            if self._energized.get(spec["device_id"]):
                loaded += spec["kw"]
                continue
            if loaded + spec["kw"] > capacity + 1e-6:
                self._emit(
                    spec["device_id"], EV_GENSET_OVERLOAD,
                    reason="genset-capacity-exceeded", actor="protection",
                    extra={"kw": spec["kw"], "capacity_kw": capacity, "loaded_kw": round(loaded, 3)},
                )
                continue
            self._feed(spec, SOURCE_GENSET, reason="genset-security", actor="protection")
            loaded += spec["kw"]

    def _genset_failure(self, reason: str) -> None:
        if self._machine.state == "genset_failed":
            return
        self._machine.to("genset_failed", "protection", "柴油发电机启动失败")
        self._genset_state = "failed"
        self._genset_ready_at = None
        self._last_failure_at = self.clock.timestamp()
        self._genset_failure_count += 1
        self._emit(None, EV_GENSET_FAILED, reason=reason, actor="protection")

    def _retry_cooldown_done(self, now: float) -> bool:
        if self._last_failure_at is None:
            return True
        return now - self._last_failure_at >= self.settings.power_genset_retry_cooldown_seconds

    # ------------------------------------------------------------------ 复电
    def _grid_confirmed(self, now: float) -> bool:
        """电网电压恢复必须持续稳定一段时间才允许切回。"""

        if self._grid_stable_since is None:
            self._grid_stable_since = now
            return False
        return now - self._grid_stable_since >= self.settings.power_grid_return_confirm_seconds

    def _begin_restoring(self, reason: str) -> None:
        if self._machine.state != "restoring":
            self._machine.to("restoring", "protection", "市电恢复，按顺序复电")
        self._emit(None, EV_GRID_RETURN, reason=reason, actor="protection")
        if self._ats_closed:
            self._emit(None, EV_ATS_OPEN, reason="return-to-grid", actor="protection")
            for spec in self._sorted_loads():
                if spec["source"] == "genset" and self._energized.get(spec["device_id"]):
                    self._shed(spec, reason="ats-return-to-grid", actor="protection")
        self._emit(None, EV_GENSET_STOP, reason="return-to-grid", actor="protection")
        self._genset_state = "stopped"
        self._ats_closed = False
        self._genset_ready_at = None
        assert self._incident is not None
        self._incident["phase"] = "restoring"
        # 复电队列：当前无电、未被人工停用、且允许自动送电的设备，按 restore_order。
        queue = [
            spec["device_id"]
            for spec in self._sorted_loads()
            if not self._energized.get(spec["device_id"])
            and spec["device_id"] not in self._blocked
            and spec["sag_policy"] != "manual"
        ]
        self._restore_queue = queue
        self._restore_next_at = None
        self._emit(None, EV_RESTORE_BEGIN, reason=reason, actor="protection",
                   extra={"queued": list(queue)})
        self._advance_restore(actor="protection", now=self.clock.timestamp())

    def _advance_restore(self, *, actor: str, now: float) -> None:
        if not self._restore_queue:
            return
        if self._restore_next_at is not None and now < float(self._restore_next_at):
            return
        device_id = self._restore_queue.pop(0)
        spec = self._catalog.get(device_id)
        if spec is None or device_id in self._blocked:
            self._advance_restore(actor=actor, now=now)
            return
        self._feed(spec, SOURCE_GRID, reason="ordered-restore", actor=actor)
        self._restore_next_at = self.clock.timestamp() + self.settings.power_restore_step_seconds

    def _finish_restore(self) -> None:
        if self._machine.state == "restoring":
            self._machine.to("idle", "protection", "复电顺序执行完毕")
        self._emit(None, EV_RESTORE_COMPLETE, reason="all-feeds-restored", actor="protection")
        self._restore_queue = []
        self._restore_next_at = None
        self._grid_stable_since = None
        self._genset_state = "stopped"
        self._incident = None

    # ------------------------------------------------------------------ 内部
    def _feed(
        self,
        spec: Mapping[str, Any],
        source: str,
        *,
        reason: str,
        actor: str,
        event: str = EV_LOAD_ENERGIZED,
    ) -> None:
        device_id = spec["device_id"]
        if device_id in self._blocked:
            raise GuardViolation("设备处于人工停用状态，禁止自动送电", details={"device_id": device_id})
        self._energized[device_id] = True
        self._emit(device_id, event, source=source, reason=reason, actor=actor,
                   extra={"grade": spec["grade"], "kw": spec["kw"]})

    def _feed_secure(self, spec: Mapping[str, Any], *, reason: str, actor: str, event: str) -> None:
        """发电机运行时送保安负荷，受容量约束。"""

        device_id = spec["device_id"]
        if spec["source"] == "ups":
            self._feed(spec, SOURCE_UPS, reason=reason, actor=actor, event=event)
            return
        capacity = self.settings.power_genset_capacity_kw
        if self._genset_load_kw() + spec["kw"] > capacity + 1e-6:
            self._emit(
                device_id, EV_GENSET_OVERLOAD,
                reason=reason, actor=actor,
                extra={"kw": spec["kw"], "capacity_kw": capacity, "loaded_kw": round(self._genset_load_kw(), 3)},
            )
            raise GuardViolation(
                "保安母线容量不足，禁止再送该负荷",
                details={"device_id": device_id, "kw": spec["kw"], "capacity_kw": capacity,
                         "loaded_kw": round(self._genset_load_kw(), 3)},
            )
        self._feed(spec, SOURCE_GENSET, reason=reason, actor=actor, event=event)

    def _shed(self, spec: Mapping[str, Any], *, reason: str, actor: str, event: str = EV_LOAD_SHED) -> None:
        device_id = spec["device_id"]
        self._energized[device_id] = False
        self._emit(device_id, event, source=SOURCE_NONE, reason=reason, actor=actor,
                   extra={"grade": spec["grade"], "kw": spec["kw"]})

    def _genset_load_kw(self) -> float:
        return round(
            sum(
                spec["kw"]
                for spec in self._catalog.values()
                if spec["source"] == "genset" and self._energized.get(spec["device_id"])
            ),
            3,
        )

    def _emit(
        self,
        device_id: str | None,
        event: str,
        *,
        source: str | None = None,
        reason: str,
        actor: str,
        extra: Mapping[str, Any] | None = None,
    ) -> JournalEntry:
        payload: dict[str, Any] = {
            "at": self.clock.timestamp_iso(),
            "epoch": self.clock.timestamp(),
            "incident_id": None if self._incident is None else self._incident["id"],
            "incident_kind": None if self._incident is None else self._incident["kind"],
            "device_id": device_id,
            "event": event,
            "source": source,
            "reason": reason,
            "actor": actor,
        }
        if extra:
            payload.update(extra)
        entry = self.store.append(LEDGER_STREAM, payload)
        self.metrics.inc(f"power.event.{event}")
        if event == EV_LOAD_SHED:
            self.metrics.inc("power.loads_shed")
        elif event == EV_LOAD_ENERGIZED:
            self.metrics.inc("power.loads_energized")
        elif event == EV_GENSET_START:
            self.metrics.inc("power.genset_starts")
        elif event == EV_GENSET_FAILED:
            self.metrics.inc("power.genset_failures")
        self._refresh_gauges()
        return entry

    def _ledger_to_dict(self, entry: JournalEntry) -> dict[str, Any]:
        payload = dict(entry.payload)
        return {
            "seq": entry.seq,
            "at": payload.get("at", entry.written_at),
            "incident_id": payload.get("incident_id"),
            "incident_kind": payload.get("incident_kind"),
            "device_id": payload.get("device_id"),
            "event": payload.get("event"),
            "source": payload.get("source"),
            "reason": payload.get("reason"),
            "actor": payload.get("actor"),
            "details": {k: v for k, v in payload.items()
                        if k not in {"at", "epoch", "incident_id", "incident_kind", "device_id",
                                     "event", "source", "reason", "actor"}},
        }

    def _sorted_loads(self) -> list[dict[str, Any]]:
        return [self._catalog[d] for d in sorted(self._catalog, key=lambda d: self._catalog[d]["restore_order"])]

    def _require_device(self, device_id: str) -> dict[str, Any]:
        spec = self._catalog.get(device_id)
        if spec is None:
            raise NotFoundError("设备未登记", details={"device_id": device_id})
        return spec

    def _validate_capacity(self) -> None:
        total = sum(spec["kw"] for spec in self._catalog.values() if spec["source"] == "genset")
        capacity = self.settings.power_genset_capacity_kw
        if total > capacity + 1e-6:
            raise ValidationError(
                "保安负荷总容量超过柴油发电机容量",
                details={"genset_load_kw": round(total, 3), "capacity_kw": capacity},
            )

    # ------------------------------------------------------------------ 落盘
    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "grid_voltage": round(self._grid_voltage, 4),
            "incident": self._incident,
            "genset": {
                "state": self._genset_state,
                "ats_closed": self._ats_closed,
                "ready_at_epoch": self._genset_ready_at,
                "failure_count": self._genset_failure_count,
                "last_start_at": self._genset_last_start_at,
                "last_test_at": self._genset_last_test_at,
                "last_failure_epoch": self._last_failure_at,
            },
            "catalog": [dict(spec) for spec in self._catalog.values()],
            "energized": dict(self._energized),
            "blocked": sorted(self._blocked),
            "pending": [list(item) for item in self._pending],
            "restore_queue": list(self._restore_queue),
            "restore_next_at_epoch": self._restore_next_at,
            "grid_stable_since_epoch": self._grid_stable_since,
            "sag_entered_epoch": self._sag_entered_epoch,
            "blackout_seq": self._blackout_seq,
            "sag_seq": self._sag_seq,
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _load_from_payload(self, payload: Mapping[str, Any]) -> None:
        self._machine.restore(payload)
        self._grid_voltage = float(payload.get("grid_voltage", 1.0))
        self._incident = payload.get("incident") if isinstance(payload.get("incident"), dict) else None
        genset = payload.get("genset", {})
        if isinstance(genset, dict):
            self._genset_state = str(genset.get("state", "stopped"))
            self._ats_closed = bool(genset.get("ats_closed", False))
            self._genset_ready_at = genset.get("ready_at_epoch")
            self._genset_failure_count = int(genset.get("failure_count", 0))
            self._genset_last_start_at = genset.get("last_start_at")
            self._genset_last_test_at = genset.get("last_test_at")
            self._last_failure_at = genset.get("last_failure_epoch")
        catalog = payload.get("catalog", [])
        if isinstance(catalog, list):
            for raw in catalog:
                if isinstance(raw, Mapping) and "device_id" in raw:
                    spec = validate_spec(raw)
                    self._catalog[spec["device_id"]] = spec
        self._validate_capacity()
        energized = payload.get("energized", {})
        if isinstance(energized, Mapping):
            for device_id in self._catalog:
                self._energized[device_id] = bool(energized.get(device_id, False))
        blocked = payload.get("blocked", [])
        if isinstance(blocked, list):
            self._blocked = {d for d in blocked if d in self._catalog}
        pending = payload.get("pending", [])
        if isinstance(pending, list):
            self._pending = [list(item) for item in pending if isinstance(item, list) and len(item) == 2
                             and item[1] in self._catalog]
        queue = payload.get("restore_queue", [])
        if isinstance(queue, list):
            self._restore_queue = [d for d in queue if d in self._catalog]
        self._restore_next_at = payload.get("restore_next_at_epoch")
        self._grid_stable_since = payload.get("grid_stable_since_epoch")
        self._sag_entered_epoch = payload.get("sag_entered_epoch")
        self._blackout_seq = int(payload.get("blackout_seq", 0))
        self._sag_seq = int(payload.get("sag_seq", 0))
        self._refresh_gauges()

    def _refresh_gauges(self) -> None:
        self.metrics.observe("power.grid_voltage", round(self._grid_voltage, 4))
        self.metrics.observe("power.genset_load_kw", self._genset_load_kw() if self._ats_closed else 0.0)
        self.metrics.observe("power.energized_count", float(sum(1 for v in self._energized.values() if v)))
        self.metrics.observe("power.de_energized_count", float(sum(1 for v in self._energized.values() if not v)))


__all__ = ["PowerSystem", "STATES", "TRANSITIONS", "LEDGER_STREAM"]
