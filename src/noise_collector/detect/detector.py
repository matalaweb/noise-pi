"""Deterministic candidate-disturbance detector over one-second intervals.

States: ``warming`` (an enabled relative rule has no baseline yet), ``idle``, ``candidate``
(a rule has started but not completed its consecutive-seconds requirement), ``active`` and
``post_roll``.

* A rule qualifies an interval when the interval is complete, its metric is not null, and the
  value is >= threshold (relative: frozen baseline + delta; absolute: threshold). Rules
  combine with OR. An event starts at the first qualifying interval of the confirming run.
* Exit: every evaluable rule's value must be below its threshold minus ``hysteresis_db`` for
  ``quiet_seconds`` consecutive complete intervals. Null/invalid metrics are never quiet. The
  provisional detection end is the first second of that quiet run; post-roll is measured from
  it, so the quiet seconds are part of the post-roll.
* A retrigger (a rule completing its run against the frozen thresholds) before post-roll ends
  continues the same event. A data loss while active terminates the event as incomplete.
* Baselines only take complete, unclipped intervals that qualify no rule while idle/warming
  and outside the post-event recovery period.

All inputs are interval values; no wall-clock or randomness is consulted, so replay is exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contract.configuration import DetectionSettings, Rule
from .baseline import RollingPercentile

WARMING, IDLE, CANDIDATE, ACTIVE, POST_ROLL = "warming", "idle", "candidate", "active", "post_roll"
BASELINE_METRICS = ("laeq_db", "lafmax_db", "lceq_db", "low_frequency_leq_db", "rms_dbfs")


@dataclass(frozen=True)
class DetectorInterval:
    second: int
    complete: bool
    values: dict[str, float | None]
    baseline_eligible: bool = True
    clipped: bool = False


@dataclass(frozen=True)
class EventStart:
    start_second: int
    confirm_second: int
    trigger: dict
    baseline: dict
    rules: list[dict]
    rule_version: str


@dataclass(frozen=True)
class EventQuiet:
    provisional_end_second: int
    post_roll_end_second: int


@dataclass(frozen=True)
class EventResumed:
    second: int


@dataclass(frozen=True)
class EventEnd:
    provisional_end_second: int
    post_roll_end_second: int
    post_roll_truncated: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class EventAbort:
    reason: str
    last_observed_second: int  # exclusive: first second not observed as part of the event


Action = EventStart | EventQuiet | EventResumed | EventEnd | EventAbort


def effective_rules(rules: list[Rule], mode: str, low_frequency_validated: bool, scale_available: bool = True) -> tuple[list[Rule], dict[str, str]]:
    """Rules the detector can honour for this profile.

    Uncalibrated profiles may only trigger on ``rms_dbfs`` (server rule). Absolute-SPL rules need
    a usable scale. The low-frequency metric triggers only after field validation (local setting).
    """
    active: list[Rule] = []
    disabled: dict[str, str] = {}
    for r in rules:
        if not r.enabled:
            disabled[r.id] = "disabled_by_configuration"
        elif mode == "uncalibrated" and r.metric != "rms_dbfs":
            disabled[r.id] = "spl_rule_with_uncalibrated_profile"
        elif r.metric != "rms_dbfs" and not scale_available:
            disabled[r.id] = "no_absolute_scale"
        elif r.metric == "low_frequency_leq_db" and not low_frequency_validated:
            disabled[r.id] = "low_frequency_not_validated"
        else:
            active.append(r)
    return active, disabled


def _rule_dict(r: Rule) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "metric": r.metric,
        "delta_db": r.delta_db,
        "threshold_db": r.threshold_db,
        "consecutive_seconds": r.consecutive_seconds,
    }


@dataclass
class _Frozen:
    rules: list[Rule]
    thresholds: dict[str, float]
    baseline: dict
    settings: DetectionSettings


@dataclass
class Detector:
    settings: DetectionSettings
    rules: list[Rule]
    post_roll_seconds: int
    state: str = WARMING
    baselines: dict[str, RollingPercentile] = field(default_factory=dict)
    pending: tuple[DetectionSettings, list[Rule], int] | None = None
    runs: dict[str, tuple[int, int]] = field(default_factory=dict)
    onset_baseline: dict | None = None
    frozen: _Frozen | None = None
    quiet_start: int = 0
    quiet_len: int = 0
    provisional_end: int | None = None
    post_roll_end: int | None = None
    recovery_until: int = -(2**62)

    def __post_init__(self) -> None:
        b = self.settings.baseline
        for m in BASELINE_METRICS:
            self.baselines.setdefault(m, RollingPercentile(b.window_seconds, b.percentile, b.min_eligible_seconds))
        self._validate(self.settings, self.post_roll_seconds)

    @staticmethod
    def _validate(settings: DetectionSettings, post_roll_seconds: int) -> None:
        if post_roll_seconds < settings.quiet_seconds:
            raise ValueError("post_roll_seconds must be >= quiet_seconds (quiet run is part of post-roll)")

    # -- configuration ---------------------------------------------------------------------

    @property
    def in_event(self) -> bool:
        return self.state in (ACTIVE, POST_ROLL)

    def update(self, settings: DetectionSettings, rules: list[Rule], post_roll_seconds: int) -> bool:
        """Install new thresholds now if no event is open, else after it completes. Returns True if applied now."""
        self._validate(settings, post_roll_seconds)
        if self.in_event or self.state == CANDIDATE:
            self.pending = (settings, rules, post_roll_seconds)
            return False
        self._install(settings, rules, post_roll_seconds)
        return True

    def _install(self, settings: DetectionSettings, rules: list[Rule], post_roll_seconds: int) -> None:
        self.settings, self.rules, self.post_roll_seconds = settings, rules, post_roll_seconds
        b = settings.baseline
        for rp in self.baselines.values():
            rp.configure(b.window_seconds, b.percentile, b.min_eligible_seconds)
        self.runs.clear()
        self.pending = None

    def reset_baselines(self) -> None:
        for rp in self.baselines.values():
            rp.clear()

    # -- helpers ---------------------------------------------------------------------------

    def _baseline_now(self, k: int) -> dict:
        snap = {m: self.baselines[m].value(k) for m in BASELINE_METRICS}
        snap["eligible_seconds"] = self.baselines["laeq_db"].count(k)
        snap["eligible_seconds_dbfs"] = self.baselines["rms_dbfs"].count(k)
        return snap

    def _threshold(self, r: Rule, baseline: dict) -> float | None:
        if r.kind == "absolute":
            return r.threshold_db
        b = baseline.get(r.metric)
        return None if b is None else b + (r.delta_db or 0.0)

    def _resting_state(self, k: int) -> str:
        for r in self.rules:
            if r.kind == "relative" and self.baselines[r.metric].value(k + 1) is None:
                return WARMING
        return IDLE

    def _settle(self, k: int) -> None:
        if self.pending is not None:
            self._install(*self.pending)
        self.state = self._resting_state(k)
        self.onset_baseline = None
        self.frozen = None
        self.runs.clear()
        self.quiet_len = 0
        self.provisional_end = self.post_roll_end = None

    def _advance_runs(self, iv: DetectorInterval, rules: list[Rule], thresholds: dict[str, float | None]) -> tuple[bool, tuple[int, int, Rule] | None]:
        qualified = False
        best: tuple[int, int, Rule] | None = None
        for order, r in enumerate(rules):
            thr = thresholds.get(r.id)
            v = iv.values.get(r.metric)
            if thr is not None and v is not None and v >= thr:
                start, length = self.runs.get(r.id, (iv.second, 0))
                if length == 0:
                    start = iv.second
                self.runs[r.id] = (start, length + 1)
                qualified = True
                if length + 1 >= r.consecutive_seconds and (best is None or (start, order) < best[:2]):
                    best = (start, order, r)
            else:
                self.runs[r.id] = (iv.second + 1, 0)
        return qualified, best

    # -- main entry ------------------------------------------------------------------------

    def on_interval(self, iv: DetectorInterval) -> list[Action]:
        k = iv.second
        if not iv.complete:
            return self.on_data_loss(k, "data_loss")
        if self.state in (WARMING, IDLE, CANDIDATE):
            return self._evaluate_rest(iv)
        if self.state == ACTIVE:
            return self._evaluate_active(iv)
        return self._evaluate_post_roll(iv)

    def _evaluate_rest(self, iv: DetectorInterval) -> list[Action]:
        k = iv.second
        was_candidate = self.state == CANDIDATE
        snapshot = self.onset_baseline if was_candidate and self.onset_baseline else self._baseline_now(k)
        thresholds = {r.id: self._threshold(r, snapshot) for r in self.rules}
        qualified, best = self._advance_runs(iv, self.rules, thresholds)
        actions: list[Action] = []
        if best is not None:
            start, _, rule = best
            thr = thresholds[rule.id]
            assert thr is not None
            evaluable = {r.id: t for r, t in ((r, thresholds[r.id]) for r in self.rules) if t is not None}
            self.frozen = _Frozen(rules=list(self.rules), thresholds=evaluable, baseline=snapshot, settings=self.settings)
            self.state = ACTIVE
            self.quiet_len = 0
            actions.append(
                EventStart(
                    start_second=start,
                    confirm_second=k,
                    trigger={
                        "rule_id": rule.id,
                        "kind": rule.kind,
                        "metric": rule.metric,
                        "threshold_db": thr,
                        "delta_db": rule.delta_db,
                        "consecutive_seconds": rule.consecutive_seconds,
                        "first_qualifying_second": start,
                    },
                    baseline=dict(snapshot),
                    rules=[dict(_rule_dict(r), evaluated_threshold_db=evaluable.get(r.id)) for r in self.rules],
                    rule_version=self.settings.rule_version,
                )
            )
            return actions
        if qualified:
            if not was_candidate:
                self.onset_baseline = snapshot
            self.state = CANDIDATE
            return actions
        self.onset_baseline = None
        if was_candidate and self.pending is not None:
            self._install(*self.pending)
        self.state = self._resting_state(k)
        if iv.baseline_eligible and not iv.clipped and k >= self.recovery_until:
            for m in BASELINE_METRICS:
                v = iv.values.get(m)
                if v is not None:
                    self.baselines[m].add(k, v)
            self.state = self._resting_state(k)
        return actions

    def _quiet(self, iv: DetectorInterval) -> bool:
        assert self.frozen is not None
        if iv.clipped:
            return False
        hyst = self.frozen.settings.hysteresis_db
        for r in self.frozen.rules:
            thr = self.frozen.thresholds.get(r.id)
            if thr is None:
                continue
            v = iv.values.get(r.metric)
            if v is None or v >= thr - hyst:
                return False
        return True

    def _evaluate_active(self, iv: DetectorInterval) -> list[Action]:
        k = iv.second
        if self._quiet(iv):
            if self.quiet_len == 0:
                self.quiet_start = k
            self.quiet_len += 1
        else:
            self.quiet_len = 0
        assert self.frozen is not None
        if self.quiet_len >= self.frozen.settings.quiet_seconds:
            self.provisional_end = self.quiet_start
            self.post_roll_end = self.quiet_start + self.post_roll_seconds
            self.state = POST_ROLL
            self.runs.clear()
            actions: list[Action] = [EventQuiet(self.provisional_end, self.post_roll_end)]
            if k + 1 >= self.post_roll_end:
                actions += self._finish(k)
            return actions
        return []

    def _evaluate_post_roll(self, iv: DetectorInterval) -> list[Action]:
        k = iv.second
        assert self.frozen is not None and self.post_roll_end is not None
        thresholds: dict[str, float | None] = dict(self.frozen.thresholds)
        _, best = self._advance_runs(iv, [r for r in self.frozen.rules if r.id in thresholds], thresholds)
        if best is not None:
            self.state = ACTIVE
            self.provisional_end = self.post_roll_end = None
            self.quiet_len = 0
            self.runs.clear()
            return [EventResumed(k)]
        if k + 1 >= self.post_roll_end:
            return self._finish(k)
        return []

    def _finish(self, k: int, truncated: bool = False, reason: str | None = None) -> list[Action]:
        assert self.provisional_end is not None and self.post_roll_end is not None
        end = EventEnd(self.provisional_end, self.post_roll_end, post_roll_truncated=truncated, reason=reason)
        self.recovery_until = self.post_roll_end + self.settings.baseline.recovery_seconds
        self._settle(k)
        return [end]

    def on_data_loss(self, k: int, reason: str) -> list[Action]:
        """Interval ``k`` (or a stream stop/split at ``k``) was not observed."""
        if self.state == ACTIVE:
            self.recovery_until = k + 1 + self.settings.baseline.recovery_seconds
            self._settle(k)
            return [EventAbort(reason=reason, last_observed_second=k)]
        if self.state == POST_ROLL:
            return self._finish(k, truncated=True, reason=reason)
        self.onset_baseline = None
        self.runs.clear()
        if self.pending is not None:
            self._install(*self.pending)
        if self.state == CANDIDATE:
            self.state = self._resting_state(k)
        return []
