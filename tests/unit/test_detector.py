from noise_collector.contract.configuration import DetectionSettings, Rule
from noise_collector.detect.detector import (
    ACTIVE,
    CANDIDATE,
    IDLE,
    POST_ROLL,
    WARMING,
    Detector,
    DetectorInterval,
    EventAbort,
    EventEnd,
    EventQuiet,
    EventStart,
    effective_rules,
)

REL = Rule(id="a", kind="relative", metric="laeq_db", delta_db=12, consecutive_seconds=2)


def make(rules=None, post_roll=30, **baseline):
    b = {"window_seconds": 600, "min_eligible_seconds": 120, "recovery_seconds": 30, **baseline}
    settings = DetectionSettings(baseline=b, rules=rules or [REL])
    return Detector(settings=settings, rules=rules or [REL], post_roll_seconds=post_roll)


def iv(k, laeq=40.0, complete=True, clipped=False, lf=None):
    return DetectorInterval(k, complete, {"laeq_db": laeq, "low_frequency_leq_db": lf, "lafmax_db": laeq + 1 if laeq else None,
                                          "rms_dbfs": None}, baseline_eligible=complete and not clipped, clipped=clipped)


def feed(det, values, start=0):
    actions = []
    for i, v in enumerate(values):
        for a in det.on_interval(v if isinstance(v, DetectorInterval) else iv(start + i, v)):
            actions.append((start + i, a))
    return actions


def warm(det, n=120, level=40.0):
    assert feed(det, [level] * n) == []
    return n


def test_warmup_requires_120_eligible_seconds():
    det = make()
    feed(det, [40.0] * 119)
    assert det.state == WARMING
    feed(det, [40.0], start=119)
    assert det.state == IDLE
    # loud during warmup cannot trigger a relative rule
    det2 = make()
    assert feed(det2, [40.0] * 10 + [90.0] * 5) == []


def test_two_second_confirmation_starts_at_first_qualifying_second():
    det = make()
    k = warm(det)
    acts = feed(det, [55.0, 55.0], start=k)
    assert det.state == ACTIVE
    (sec, start), = acts
    assert isinstance(start, EventStart)
    assert start.start_second == k and start.confirm_second == k + 1
    assert start.trigger["threshold_db"] == 52.0
    assert start.baseline["laeq_db"] == 40.0


def test_single_qualifying_second_returns_to_idle_and_is_not_baseline():
    det = make()
    k = warm(det)
    assert feed(det, [55.0], start=k) == []
    assert det.state == CANDIDATE
    feed(det, [40.0], start=k + 1)
    assert det.state == IDLE
    assert all(v != 55.0 for _, v in det.baselines["laeq_db"].values)


def test_quiet_run_and_post_roll_accounting():
    det = make()
    k = warm(det)
    feed(det, [60.0] * 10, start=k)  # event k .. k+9
    acts = feed(det, [40.0] * 30, start=k + 10)
    quiet = [a for _, a in acts if isinstance(a, EventQuiet)]
    end = [(s, a) for s, a in acts if isinstance(a, EventEnd)]
    assert quiet[0].provisional_end_second == k + 10
    assert quiet[0].post_roll_end_second == k + 40  # 30 s post-roll *including* the 5 quiet seconds
    assert len(end) == 1 and end[0][0] == k + 39  # finalized when the last post-roll second completes
    assert det.state in (IDLE, WARMING)


def test_hysteresis_value_between_threshold_minus_3_and_threshold_is_not_quiet():
    det = make()
    k = warm(det)
    feed(det, [60.0] * 3, start=k)
    acts = feed(det, [50.0] * 10, start=k + 3)  # threshold 52, quiet needs < 49
    assert acts == [] and det.state == ACTIVE


def test_retrigger_during_post_roll_continues_event():
    det = make()
    k = warm(det)
    feed(det, [60.0] * 5, start=k)
    acts = feed(det, [40.0] * 10 + [60.0, 60.0] + [40.0] * 40, start=k + 5)
    kinds = [type(a).__name__ for _, a in acts]
    assert kinds.count("EventStart") == 0
    assert "EventResumed" in kinds
    ends = [a for _, a in acts if isinstance(a, EventEnd)]
    assert len(ends) == 1 and ends[0].provisional_end_second == k + 17


def test_later_trigger_after_finalization_creates_new_event():
    det = make(recovery_seconds=0)
    k = warm(det)
    feed(det, [60.0] * 5 + [40.0] * 30, start=k)
    acts = feed(det, [60.0, 60.0], start=k + 35)
    assert any(isinstance(a, EventStart) for _, a in acts)


def test_invalid_intervals_do_not_end_event_but_data_loss_terminates():
    det = make()
    k = warm(det)
    feed(det, [60.0] * 3, start=k)
    nulls = [DetectorInterval(k + 3 + i, True, {"laeq_db": None, "low_frequency_leq_db": None, "lafmax_db": None, "rms_dbfs": None})
             for i in range(10)]
    assert feed(det, nulls) == [] and det.state == ACTIVE
    clipped = [iv(k + 13 + i, 30.0, clipped=True) for i in range(10)]
    assert feed(det, clipped) == [] and det.state == ACTIVE
    acts = det.on_interval(iv(k + 23, complete=False))
    assert isinstance(acts[0], EventAbort) and acts[0].last_observed_second == k + 23


def test_data_loss_in_post_roll_keeps_detection_end():
    det = make()
    k = warm(det)
    feed(det, [60.0] * 3 + [40.0] * 6, start=k)
    assert det.state == POST_ROLL
    (a,) = det.on_data_loss(k + 9, "capture_buffer_overflow")
    assert isinstance(a, EventEnd) and a.post_roll_truncated and a.provisional_end_second == k + 3


def test_baseline_frozen_during_event_and_excludes_recovery():
    det = make(recovery_seconds=30)
    k = warm(det, level=40.0)
    feed(det, [70.0] * 600, start=k)  # long loud event: baseline must not adapt
    assert det.frozen.baseline["laeq_db"] == 40.0
    assert det.state == ACTIVE
    assert all(v == 40.0 for _, v in det.baselines["laeq_db"].values)  # nothing loud was added
    assert det.baselines["laeq_db"].count(k + 600) == 0  # pre-event values have aged out


def test_stale_baseline_becomes_unavailable_not_adapted():
    det = make()
    k = warm(det)
    # 600 s of clipped (ineligible) intervals: baseline ages out, relative rule cannot trigger
    feed(det, [iv(k + i, 40.0, clipped=True) for i in range(600)])
    assert det.baselines["laeq_db"].value(k + 600) is None
    assert det.state == WARMING


def test_absolute_rule_operates_during_warmup_and_one_second_impulse():
    rules = [REL, Rule(id="abs_fmax", kind="absolute", metric="lafmax_db", threshold_db=80, consecutive_seconds=1)]
    det = make(rules=rules)
    acts = feed(det, [40.0] * 10 + [85.0])
    starts = [a for _, a in acts if isinstance(a, EventStart)]
    assert starts and starts[0].trigger["rule_id"] == "abs_fmax" and starts[0].start_second == 10


def test_rule_change_deferred_until_event_ends():
    det = make()
    k = warm(det)
    feed(det, [60.0] * 2, start=k)
    stricter = Rule(id="a", kind="relative", metric="laeq_db", delta_db=30, consecutive_seconds=2)
    assert det.update(DetectionSettings(rules=[stricter]), [stricter], 30) is False
    assert det.frozen.thresholds["a"] == 52.0
    feed(det, [40.0] * 30, start=k + 2)
    assert det.rules == [stricter]


def test_effective_rules_by_mode():
    rules = [REL, Rule(id="lf", kind="relative", metric="low_frequency_leq_db", delta_db=10),
             Rule(id="d", kind="relative", metric="rms_dbfs", delta_db=12)]
    act, dis = effective_rules(rules, "uncalibrated", False)
    assert [r.id for r in act] == ["d"] and dis["a"] == "spl_rule_with_uncalibrated_profile"
    act, dis = effective_rules(rules, "calibrated", False)
    assert [r.id for r in act] == ["a", "d"] and dis["lf"] == "low_frequency_not_validated"
    act, _ = effective_rules(rules, "calibrated", True)
    assert [r.id for r in act] == ["a", "lf", "d"]
    act, dis = effective_rules(rules, "calibrated", True, scale_available=False)
    assert [r.id for r in act] == ["d"] and dis["a"] == "no_absolute_scale"


def test_relative_rule_on_lafmax_uses_its_own_baseline():
    rule = Rule(id="f", kind="relative", metric="lafmax_db", delta_db=15, consecutive_seconds=1)
    det = make(rules=[rule])
    warm(det)
    (a,) = [x for _, x in feed(det, [57.0], start=120)]  # lafmax = laeq + 1 in iv(); baseline 41
    assert isinstance(a, EventStart) and a.trigger["metric"] == "lafmax_db" and a.baseline["lafmax_db"] == 41.0


def test_replay_determinism_same_inputs_same_actions():
    import random

    r = random.Random(3)
    seq = [40 + r.random() * 3 + (25 if 300 < i < 320 or 500 < i < 503 else 0) for i in range(800)]
    a1 = [(s, repr(a)) for s, a in feed(make(), seq)]
    a2 = [(s, repr(a)) for s, a in feed(make(), seq)]
    assert a1 == a2 and len(a1) >= 4
