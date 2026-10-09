"""Deterministic replay through the real engine + durability layer (synthetic signals only)."""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from conftest import START_UTC, engine_scenario
from noise_collector.acquisition.durability import DirectSink, DurabilityApplier
from noise_collector.acquisition.engine import AcquisitionEngine, EngineLocalSettings, GainState
from noise_collector.acquisition.recovery import recover_state
from noise_collector.acquisition.replay import replay
from noise_collector.audio.file_source import Faults, FileSource, read_samples
from noise_collector.contract.configuration import DeviceConfiguration
from noise_collector.contract.examples import example_configuration
from noise_collector.store.db import connect, migrate
from noise_collector.synth import Burst, Scenario
from noise_collector.timing.clock import SimulatedClock
from noise_collector.timeutil import iso_second

FS = 48000
K0 = int(START_UTC)


def ids():
    c = itertools.count(1)
    return lambda: f"00000000-0000-4000-8000-{next(c):012d}"


STATE: list = [None]


def db(state: Path) -> sqlite3.Connection:
    STATE[0] = state
    c = sqlite3.connect(state / "collector.db")
    c.row_factory = sqlite3.Row
    return c


def events(conn):
    return [json.loads(r["payload_json"]) for r in conn.execute("SELECT payload_json FROM event_revisions ORDER BY created_at, revision")]


def recordings(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM recordings ORDER BY rowid")]


def wav_ints(path, state=None):
    if state is not None:
        path = Path(state) / path
    data, _ = sf.read(path, dtype="int32")
    return (data.astype(np.int64) >> 8).astype(np.int32)


@pytest.fixture(scope="module")
def engine_wav(tmp_path_factory):
    from noise_collector.synth import write_wav

    p = tmp_path_factory.mktemp("w") / "engine.wav"
    write_wav(str(p), engine_scenario().render())
    return p


def test_preroll_and_postroll_are_exact_and_audio_is_bit_exact(engine_wav, tmp_path):
    replay(str(engine_wav), tmp_path / "s", example_configuration(), start_utc=START_UTC)
    conn = db(tmp_path / "s")
    evs = events(conn)
    final = evs[-1]
    assert final["detection_state"] == "finalized" and final["revision"] == 2 and evs[0]["detection_state"] == "open"
    assert evs[0]["ended_at"] is None and final["quality_flags"] == []
    start = final["started_at"]
    recs = recordings(conn)
    assert len(recs) == 1 and final["recording"]["expected_segments"] == 1 and final["recording"]["expected"] is True
    assert final["detection"]["trigger_kind"] == "baseline_relative" and final["detection"]["threshold_db"] == 12.0
    assert final["detection"]["trigger_value_db"] > final["detection"]["baseline_db"] + 12
    r = recs[0]
    k_start = K0 + 151  # synthetic burst begins at 150 s with a 2 s ramp
    from noise_collector.timeutil import iso_second, parse_iso

    assert start == iso_second(k_start)
    assert r["start_sample"] == (151 - 10) * FS  # exactly ten seconds before the first qualifying second
    prov_end = int(parse_iso(final["ended_at"]).timestamp())
    assert r["end_sample"] == (prov_end - K0 + 30) * FS  # thirty seconds from the provisional end, quiet run included
    assert final["recording"]["started_at"] == iso_second(K0 + 141)
    assert final["recording"]["ended_at"] == iso_second(prov_end + 30)
    src = read_samples(str(engine_wav), FileSource(str(engine_wav), start_utc=START_UTC).fmt)
    assert np.array_equal(wav_ints(r["path"], STATE[0]), src[r["start_sample"] : r["end_sample"]])
    assert r["incomplete"] == 0 and r["state"] == "finalized" and r["delivery_state"] == "pending_declaration"
    # detection summary covers detection seconds only
    assert final["summary"]["duration_ms"] == (prov_end - k_start) * 1000
    assert final["summary"]["lcpeak_db"] is None and final["summary"]["laeq_db"] is not None


@pytest.mark.parametrize("pattern,seed", [([4800], None), ([1024], None), ([37, 4096, 333], None), (None, 11)])
def test_block_partition_invariance(engine_wav, tmp_path, pattern, seed):
    ref_dir = tmp_path / "ref"
    replay(str(engine_wav), ref_dir, example_configuration(), start_utc=START_UTC, block_pattern=[48000], id_factory=ids())
    got_dir = tmp_path / "got"
    replay(str(engine_wav), got_dir, example_configuration(), start_utc=START_UTC, block_pattern=pattern, seed=seed, id_factory=ids())
    a, b = db(ref_dir), db(got_dir)
    q = "SELECT utc_second, status, wire_json FROM measurements ORDER BY utc_second"
    ra, rb = a.execute(q).fetchall(), b.execute(q).fetchall()
    assert len(ra) == len(rb)
    for x, y in zip(ra, rb):
        assert x["utc_second"] == y["utc_second"] and x["status"] == y["status"]
        if x["wire_json"]:
            wx, wy = json.loads(x["wire_json"]), json.loads(y["wire_json"])
            for k in ("laeq_db", "lceq_db", "lafmax_db", "low_frequency_leq_db", "rms_dbfs"):
                assert wx[k] == pytest.approx(wy[k], abs=0.011), (x["utc_second"], k)
    qa = [(r["start_sample"], r["end_sample"], r["sha256"]) for r in recordings(a)]
    qb = [(r["start_sample"], r["end_sample"], r["sha256"]) for r in recordings(b)]
    assert qa == qb  # identical event sample ranges and identical evidence bytes


def test_long_event_segments_without_gaps_or_overlap(tmp_path):
    from noise_collector.synth import write_wav

    sc = Scenario(duration_s=420, background_dbfs=-60, bursts=[Burst(150, 200, "garage_door_like", -30)], seed=4)
    wavp = tmp_path / "long.wav"
    write_wav(str(wavp), sc.render())
    cfg = example_configuration(recording={"max_segment_duration_seconds": 60})
    replay(str(wavp), tmp_path / "s", cfg, start_utc=START_UTC)
    conn = db(tmp_path / "s")
    recs = recordings(conn)
    assert len(recs) >= 4
    for a, b in zip(recs, recs[1:]):
        assert a["end_sample"] == b["start_sample"]
        assert a["event_id"] == b["event_id"]
    assert [r["segment_number"] for r in recs] == list(range(1, len(recs) + 1))
    assert all(r["sample_count"] <= 60 * FS for r in recs)
    src = read_samples(str(wavp), FileSource(str(wavp), start_utc=START_UTC).fmt)
    joined = np.concatenate([wav_ints(r["path"], STATE[0]) for r in recs])
    assert np.array_equal(joined, src[recs[0]["start_sample"] : recs[-1]["end_sample"]])
    final = events(conn)[-1]
    assert final["recording"]["expected_segments"] == len(recs) and final["detection_state"] == "finalized"
    # one event identity throughout; DSP not reset by rollover (no omitted seconds during the event)
    assert conn.execute("SELECT COUNT(*) FROM measurements WHERE status='omitted' AND utc_second > ?", (K0 + 10,)).fetchone()[0] == 0


def test_level_shift_event_ends_at_max_duration_and_baseline_relearns(tmp_path):
    """A door opened and left open: a loud burst, then a background ~10.5 dB above the old one
    (above the exit level of baseline + 12 - 3 dB, below the +12 dB trigger)."""
    from noise_collector.synth import _scale_to, pink_noise, write_wav
    from support.upstream import errors as schema_errors

    sc = Scenario(duration_s=600, background_dbfs=-60, bursts=[Burst(300, 15, "garage_door_like", -30)], seed=11)
    x = sc.render()
    shift = int(315 * FS)
    x[shift:] += _scale_to(pink_noise(len(x) - shift, np.random.default_rng(12)), -49.9)
    wavp = tmp_path / "shift.wav"
    write_wav(str(wavp), x)
    cfg = example_configuration(detection={"max_event_duration_seconds": 120})
    assert cfg.detection.max_event_seconds == 120
    replay(str(wavp), tmp_path / "s", cfg, start_utc=START_UTC)
    conn = db(tmp_path / "s")
    revs = events(conn)
    finals = [e for e in revs if e["detection_state"] == "finalized"]
    assert len(finals) == 1  # no new event once the baseline re-learnt the raised level
    ev = finals[0]
    started = int(ev["started_at"][17:19]) + 60 * int(ev["started_at"][14:16])
    ended = int(ev["ended_at"][17:19]) + 60 * int(ev["ended_at"][14:16])
    assert (ended - started) % 3600 == 120
    assert "max_duration_reached" in ev["quality_flags"] and "incomplete_interval" not in ev["quality_flags"]
    assert ev["recording"]["expected"] and ev["recording"]["ended_at"].startswith(ev["ended_at"][:19])
    assert schema_errors("EventRevisionRequest", ev) == []
    recs = recordings(conn)
    assert recs and sum(r["sample_count"] for r in recs) <= (120 + 10) * FS  # pre-roll + event, no post-roll
    row = conn.execute("SELECT state FROM events").fetchone()
    assert row["state"] == "complete"


def test_buffer_overflow_mid_event_terminates_observed_segment(engine_wav, tmp_path):
    faults = Faults(drop={160 * FS + 1234: 9600})
    replay(str(engine_wav), tmp_path / "s", example_configuration(), start_utc=START_UTC, faults=faults)
    conn = db(tmp_path / "s")
    final = events(conn)[1]
    # Finalized where observation stopped, flagged as such; never presented as a normal end.
    assert final["detection_state"] == "finalized" and final["ended_at"] == iso_second(K0 + 160)
    assert {"incomplete_interval", "audio_dropout"} <= set(final["quality_flags"])
    r = recordings(conn)[0]
    assert r["incomplete"] == 1 and r["end_sample"] == 160 * FS + 1234
    gap = conn.execute("SELECT * FROM gaps WHERE cause='capture_buffer_overflow'").fetchone()
    assert gap["start_sample"] == 160 * FS + 1234 and gap["end_sample"] == 160 * FS + 1234 + 9600
    omitted: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM measurements WHERE status='omitted'"):
        omitted.setdefault(r["utc_second"] - K0, []).append(r["omit_reason"])
    # second 160 is split by the hole: both fragments are kept locally as omitted diagnostics
    assert sorted(omitted[160]) == ["capture_buffer_overflow", "partial_coverage"]
    # the lost second(s) are never uploaded as measurements, and no interval is fabricated
    assert conn.execute("SELECT COUNT(*) FROM measurements WHERE utc_second=? AND delivery_state='pending'", (K0 + 160,)).fetchone()[0] == 0
    assert 161 in omitted or 162 in omitted  # filter settling after the gap
    # the noise continues after the gap: a new event starts, with its pre-roll shortfall recorded, not fabricated
    second = events(conn)[3]
    assert second["detection_state"] == "finalized" and "incomplete_interval" not in second["quality_flags"]
    detail = json.loads(conn.execute("SELECT detail_json FROM events WHERE event_id=?", (second["event_id"],)).fetchone()[0])
    assert "preroll_shortfall" in detail["local_flags"] and detail["preroll_shortfall_samples"] > 0
    assert recordings(conn)[1]["start_sample"] == 160 * FS + 1234 + 9600
    # same session continues (known-size loss keeps sample mapping)
    assert conn.execute("SELECT COUNT(*) FROM acquisition_sessions").fetchone()[0] == 1


def test_driver_overflow_starts_new_session_with_gap(engine_wav, tmp_path):
    faults = Faults(driver_overflow={60 * FS: 4800})
    replay(str(engine_wav), tmp_path / "s", example_configuration(), start_utc=START_UTC, faults=faults)
    conn = db(tmp_path / "s")
    sessions = conn.execute("SELECT * FROM acquisition_sessions ORDER BY rowid").fetchall()
    assert len(sessions) == 2 and sessions[0]["end_reason"] in ("timestamp_jump", "driver_overflow_unknown_loss")
    gap = conn.execute("SELECT * FROM gaps").fetchone()
    assert gap["cause"] == sessions[0]["end_reason"]
    seqs = conn.execute("SELECT session_id, MIN(sequence) mn FROM measurements WHERE sequence IS NOT NULL GROUP BY session_id").fetchall()
    assert all(r["mn"] == 1 for r in seqs)
    dup = conn.execute("SELECT utc_second, COUNT(*) c FROM measurements WHERE delivery_state='pending' GROUP BY utc_second HAVING c > 1").fetchall()
    assert dup == []


def test_backward_clock_step_never_duplicates_uploaded_seconds(engine_wav, tmp_path):
    faults = Faults(clock_step={100 * FS: -20.0})
    replay(str(engine_wav), tmp_path / "s", example_configuration(), start_utc=START_UTC, faults=faults)
    conn = db(tmp_path / "s")
    assert conn.execute("SELECT COUNT(*) FROM acquisition_sessions").fetchone()[0] == 2
    dup = conn.execute("SELECT utc_second, COUNT(*) c FROM measurements WHERE delivery_state='pending' GROUP BY utc_second HAVING c > 1").fetchall()
    assert dup == []
    overlap = conn.execute("SELECT COUNT(*) FROM measurements WHERE timing_note='utc_overlap'").fetchone()[0]
    assert overlap >= 18  # re-covered seconds stay local and flagged
    assert conn.execute("SELECT cause FROM gaps").fetchone()["cause"] == "wall_clock_step"


def test_unsynchronized_clock_quarantines_utc_until_sync(engine_wav, tmp_path):
    faults = Faults(clock_sync={60 * FS: True})
    replay(str(engine_wav), tmp_path / "s", example_configuration(), start_utc=START_UTC, faults=faults, synchronized=False)
    conn = db(tmp_path / "s")
    early = conn.execute("SELECT DISTINCT delivery_state, timing_note FROM measurements WHERE utc_second < ? AND status='complete'", (K0 + 59,)).fetchall()
    assert {(r[0], r[1]) for r in early} == {("local_only", "clock_unsynchronized")}
    late = conn.execute("SELECT COUNT(*) FROM measurements WHERE utc_second > ? AND delivery_state='pending'", (K0 + 61,)).fetchone()[0]
    assert late > 100


def test_recording_locally_disabled(engine_wav, tmp_path):
    local = EngineLocalSettings(recording_locally_enabled=False)
    replay(str(engine_wav), tmp_path / "s", example_configuration(), start_utc=START_UTC, local=local)
    conn = db(tmp_path / "s")
    assert recordings(conn) == []
    final = events(conn)[-1]
    assert final["recording"] == {"expected": False, "started_at": None, "ended_at": None, "expected_segments": None}
    detail = json.loads(conn.execute("SELECT detail_json FROM events").fetchone()[0])
    assert "recording_disabled" in detail["local_flags"]


def test_audio_quota_refusal_keeps_measurements(engine_wav, tmp_path):
    replay(str(engine_wav), tmp_path / "s", example_configuration(), start_utc=START_UTC, audio_allowed=lambda n: False)
    conn = db(tmp_path / "s")
    assert recordings(conn) == []
    final = events(conn)[-1]
    assert final["recording"]["expected"] is False
    detail = json.loads(conn.execute("SELECT detail_json FROM events").fetchone()[0])
    assert "audio_coverage_loss" in detail["local_flags"]
    assert conn.execute("SELECT COUNT(*) FROM measurements WHERE status='complete'").fetchone()[0] >= 230


def test_uncalibrated_mode_publishes_dbfs_only_and_uses_dbfs_rule(engine_wav, tmp_path):
    replay(str(engine_wav), tmp_path / "s", example_configuration(mode="uncalibrated"), start_utc=START_UTC)
    conn = db(tmp_path / "s")
    rows = [json.loads(r[0]) for r in conn.execute("SELECT wire_json FROM measurements WHERE status='complete'")]
    assert all(r["laeq_db"] is None and r["lceq_db"] is None and r["calibration_id"] is None for r in rows)
    assert all(r["rms_dbfs"] is not None and r["null_reasons"] == {} for r in rows)
    final = events(conn)[-1]
    assert final["detection"]["trigger_metric"] == "rms_dbfs" and final["calibration_id"] is None
    assert final["summary"]["laeq_db"] is None and final["summary"]["rms_dbfs"] is not None


# ---------------------------------------------------------------------------- manual-drive harness


class Harness:
    def __init__(self, wav: Path, state: Path, cfg: DeviceConfiguration, **kw):
        state.mkdir(parents=True, exist_ok=True)
        migrate(state / "collector.db")
        self.conn = connect(state / "collector.db")
        self.applier = DurabilityApplier(self.conn, state)
        self.clock = SimulatedClock(START_UTC - 1000.0)
        self.src = FileSource(str(wav), start_utc=START_UTC, clock=self.clock, **kw)
        self.engine = AcquisitionEngine(config=cfg, local=EngineLocalSettings(), fmt=self.src.fmt, microphone={"serial": None},
                                        sink=DirectSink(self.applier), clock=self.clock)
        self.engine.start_stream("h")
        self.blocks = self.src.blocks()

    def run_until(self, sample: int):
        for blk in self.blocks:
            self.engine.on_block(blk)
            if blk.first_sample + len(blk.samples) >= sample:
                return

    def finish(self):
        for blk in self.blocks:
            self.engine.on_block(blk)
        self.engine.stop_stream("end")
        self.applier.close("end")


def test_owner_stop_finalizes_the_open_event_with_its_flag(engine_wav, tmp_path):
    from support.upstream import errors as schema_errors

    h = Harness(engine_wav, tmp_path / "s", example_configuration())
    h.run_until(158 * FS)
    assert h.engine.detector.state == "active"
    eid = h.engine.event.event_id
    assert not h.engine.force_end_event("00000000-0000-4000-8000-000000000000")  # not the open event
    assert h.engine.force_end_event(eid)
    assert h.engine.event is None and not h.engine.force_end_event(eid)
    h.finish()
    conn = db(tmp_path / "s")
    final = [e for e in events(conn) if e["event_id"] == eid and e["detection_state"] == "finalized"]
    assert len(final) == 1 and "ended_by_operator" in final[0]["quality_flags"]
    assert schema_errors("EventRevisionRequest", final[0]) == []
    assert conn.execute("SELECT state FROM events WHERE event_id=?", (eid,)).fetchone()[0] == "complete"


def test_threshold_change_mid_event_is_deferred_and_acknowledged(engine_wav, tmp_path):
    h = Harness(engine_wav, tmp_path / "s", example_configuration())
    h.run_until(158 * FS)
    assert h.engine.detector.state == "active"
    h.engine.request_config(example_configuration(2, detection={"baseline_relative": {"delta_db": 40.0}}))
    h.run_until(160 * FS)
    assert h.engine.config.revision == 2  # applied at the next boundary...
    assert h.engine.event is not None and h.engine.detector.frozen.thresholds["baseline_relative"] < 60  # ...but thresholds frozen
    h.finish()
    acks = [dict(r) for r in h.conn.execute("SELECT revision, status FROM config_acknowledgments")]
    assert acks == [{"revision": 2, "status": "applied"}]
    final = events(h.conn)[-1]
    assert final["detection_state"] == "finalized" and final["configuration_revision"] == 1
    assert h.engine.detector.rules[0].delta_db == 40


def test_profile_change_mid_event_forces_split(engine_wav, tmp_path):
    h = Harness(engine_wav, tmp_path / "s", example_configuration())
    h.run_until(158 * FS)
    new_profile = "9fc235ef-6f5b-48d1-8d35-083dfdd5a6e9"
    from noise_collector.contract.examples import example_profile

    h.engine.request_config(example_configuration(2, profile=example_profile().model_copy(update={"profile_id": new_profile})))
    h.run_until(161 * FS)
    h.finish()
    evs = events(h.conn)
    final = [e for e in evs if e["revision"] == 2][0]
    assert final["detection_state"] == "finalized" and "incomplete_interval" in final["quality_flags"]
    rec = recordings(h.conn)[0]
    assert rec["incomplete"] == 1 and "configuration_split" in rec["close_reason"]
    # new profile provenance after the split; settling seconds omitted, never relabelled
    after = [json.loads(r[0]) for r in h.conn.execute("SELECT wire_json FROM measurements WHERE status='complete' AND utc_second > ?", (K0 + 165,))]
    assert after and all(r["profile_id"] == new_profile and r["configuration_revision"] == 2 for r in after)


def test_gain_mismatch_withholds_spl_under_same_profile(engine_wav, tmp_path):
    h = Harness(engine_wav, tmp_path / "s", example_configuration())
    h.run_until(50 * FS)
    h.engine.set_gain_state(GainState(ok=False, inspectable=True, observed={"Mic,0": "capture=10"}, note="changed"))
    h.run_until(60 * FS)
    h.engine.set_gain_state(GainState(ok=True, inspectable=True))
    h.finish()
    rows = {r["utc_second"] - K0: json.loads(r["wire_json"]) for r in h.conn.execute("SELECT utc_second, wire_json FROM measurements WHERE status='complete'")}
    w = rows[55]
    assert w["laeq_db"] is None and w["null_reasons"]["laeq_db"] == "gain_mismatch" and w["quality_flags"] == ["invalid_calibration"]
    assert w["rms_dbfs"] is not None and w["profile_id"] == rows[40]["profile_id"] == rows[70]["profile_id"]
    assert rows[70]["laeq_db"] is not None and rows[70]["quality_flags"] == []


def test_calibrated_profile_without_scale_sends_flagged_nulls(engine_wav, tmp_path):
    cfg = example_configuration(with_scale=False)
    replay(str(engine_wav), tmp_path / "s", cfg, start_utc=START_UTC)
    conn = db(tmp_path / "s")
    w = json.loads(conn.execute("SELECT wire_json FROM measurements WHERE status='complete' LIMIT 1").fetchone()[0])
    assert w["laeq_db"] is None and w["null_reasons"]["laeq_db"] == "calibration_unavailable"
    assert w["quality_flags"] == ["invalid_calibration"] and w["rms_dbfs"] is not None and w["calibration_id"] is not None
    assert events(conn) == []  # the SPL rule cannot evaluate without a scale


def test_crash_mid_event_recovers_partial_audio_without_fabrication(engine_wav, tmp_path):
    state = tmp_path / "s"
    h = Harness(engine_wav, state, example_configuration())
    h.run_until(165 * FS + 777)
    h.applier.writer.sync()
    written = h.engine.event.written_through
    rec_start = h.engine.event.rec_start_sample
    # simulate a crash: drop everything without closing anything
    h.conn.close()
    conn = connect(state / "collector.db")
    cfg = example_configuration()
    report = recover_state(conn, state, cfg)
    assert len(report["recordings_recovered"]) == 1 and len(report["events_interrupted"]) == 1
    r = recordings(conn)[0]
    assert r["state"] == "finalized" and r["incomplete"] == 1 and r["close_reason"] == "process_interrupted"
    assert r["start_sample"] == rec_start and r["end_sample"] <= written
    src = read_samples(str(engine_wav), h.src.fmt)
    assert np.array_equal(wav_ints(r["path"], state), src[r["start_sample"] : r["end_sample"]])
    ev = conn.execute("SELECT * FROM events").fetchone()
    assert ev["state"] == "interrupted"
    revs = events(conn)
    last = revs[-1]
    assert last["revision"] == 2 and last["detection_state"] == "finalized"
    assert {"incomplete_interval", "processing_error"} <= set(last["quality_flags"])
    assert last["recording"]["expected_segments"] == 1 and last["ended_at"] >= last["started_at"]
    assert conn.execute("SELECT end_reason FROM acquisition_sessions").fetchone()[0] == "process_interrupted"
    # recovery is idempotent
    assert recover_state(conn, state, cfg)["recordings_recovered"] == []
