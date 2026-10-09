"""Delivery state machines against the in-process Laravel fake (real contract) with fault injection."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from conftest import START_UTC, engine_scenario
from noise_collector.acquisition.durability import record_config_ack
from noise_collector.acquisition.replay import replay
from noise_collector.contract.examples import CALIBRATION_IDS, PROFILE_IDS, configuration_result, example_configuration, reseal
from noise_collector.delivery import retention
from noise_collector.evidence.fsutil import resolve
from noise_collector.store.db import connect


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture(scope="module")
def replayed_state(tmp_path_factory):
    """One replayed SYNTHETIC event scenario, copied fresh into each test."""
    from noise_collector.synth import write_wav

    d = tmp_path_factory.mktemp("replayed")
    wav = d / "engine.wav"
    write_wav(str(wav), engine_scenario().render())
    replay(str(wav), d / "state", example_configuration(), start_utc=START_UTC)
    return d / "state"


@pytest.fixture
def env(replayed_state, state_dir, fake_server, delivery):
    shutil.rmtree(state_dir)
    shutil.copytree(replayed_state, state_dir)
    clock = Clock(START_UTC + 300)
    fake_server.clock = clock
    fake_server.config_result = configuration_result(1)
    svc = delivery(clock=clock)
    conn = connect(svc.s.db_path)
    return svc, conn, clock, fake_server


def drain(svc, conn, clock, steps=400, audio=True):
    for _ in range(steps):
        svc.control_step(conn)
        if audio:
            svc.audio_step(conn)
        clock.advance(2.0)


def states(conn, table, col="delivery_state"):
    return {r[0]: r[1] for r in conn.execute(f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}")}


def rec_path(svc, rec):
    return resolve(svc.s.state_dir, rec["path"])


def test_happy_path_everything_accepted_and_verified(env):
    svc, conn, clock, srv = env
    pending = conn.execute("SELECT COUNT(*) FROM measurements WHERE delivery_state='pending'").fetchone()[0]
    drain(svc, conn, clock)
    assert srv.schema_violations == []  # every body valid against the upstream OpenAPI
    assert states(conn, "measurements").get("acknowledged") == pending
    assert len(srv.records) == pending
    assert states(conn, "event_revisions") == {"acknowledged": 2}
    ev = next(iter(srv.events.values()))
    assert ev["detection_state"] == "finalized" and ev["current_revision"] == 2
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    assert rec["delivery_state"] == "verified" and rec["verified_sha256"] == rec["sha256"]
    put = [r for r in srv.storage_requests if r.method == "PUT"]
    assert len(put) == 1 and "authorization" not in put[0].headers
    assert list(srv.objects.values())[0] == rec_path(svc, rec).read_bytes()
    hb = srv.heartbeats[0]
    assert hb["capabilities"]["channels"] == ["mic-1"] and "lcpeak_db" not in hb["capabilities"]["metrics"]
    calls = [p for _, p in srv.log]
    first_decl = next(i for i, p in enumerate(calls) if p.endswith("/recordings") and "/events/" in p)
    assert calls.index("/api/v1/device/events") < first_decl


def test_lost_batch_response_replays_identical_bytes(env):
    svc, conn, clock, srv = env
    srv.inject("batches", "drop_after_commit")
    sent = []
    orig = svc.api.request

    def spy(method, path, **kw):
        if path.endswith("/batches"):
            sent.append(kw["content"])
        return orig(method, path, **kw)

    svc.api.request = spy
    drain(svc, conn, clock, steps=60, audio=False)
    assert sent[0] == sent[1]  # same batch_id, sent_at, records
    first = json.loads(sent[0])
    row = conn.execute("SELECT state, response_json FROM outbox_batches WHERE batch_id=?", (first["batch_id"],)).fetchone()
    assert row["state"] == "acknowledged" and json.loads(row["response_json"])["replayed"] is True
    assert len(srv.records) == conn.execute("SELECT COUNT(*) FROM measurements WHERE delivery_state='acknowledged'").fetchone()[0]


def test_401_stops_authenticated_traffic_until_credentials_change(env):
    svc, conn, clock, srv = env
    srv.token = "nmd_rotated-token-abcdefghijklmnopqrstuvwxyz0123456789"
    drain(svc, conn, clock, steps=5)
    assert svc.auth.blocked
    n = len(srv.log)
    drain(svc, conn, clock, steps=20)
    assert len(srv.log) == n  # no further authenticated requests
    assert conn.execute("SELECT COUNT(*) FROM measurements WHERE delivery_state IN ('pending','batched')").fetchone()[0] > 0
    cred = svc.s.paths.credentials_file
    cred.write_text(f'device_token = "{srv.token}"\n')
    os.chmod(cred, 0o600)
    drain(svc, conn, clock, steps=200)
    assert not svc.auth.blocked
    assert "acknowledged" in states(conn, "measurements")


def test_conflicts_quarantine_without_mutation(env):
    svc, conn, clock, srv = env
    srv.inject("batches", "code:measurement_conflict")
    srv.inject("events", "code:validation_failed")
    drain(svc, conn, clock, steps=120, audio=False)
    q = conn.execute("SELECT batch_id, last_status, last_error, payload FROM outbox_batches WHERE state='quarantined'").fetchall()
    assert [r["last_status"] for r in q] == [409] and q[0]["last_error"] == "measurement_conflict"
    ev = conn.execute("SELECT * FROM event_revisions WHERE revision=1").fetchone()
    assert ev["delivery_state"] == "quarantined" and ev["last_error"] == "validation_failed"
    # revision 2 is held back behind the quarantined revision 1 (revision order), never reworded
    assert conn.execute("SELECT delivery_state FROM event_revisions WHERE revision=2").fetchone()[0] == "pending"
    rows = conn.execute("SELECT delivery_state FROM measurements WHERE batch_id=?", (q[0]["batch_id"],)).fetchall()
    assert {x[0] for x in rows} == {"quarantined"}


def test_413_splits_into_smaller_batches_with_same_records(env):
    svc, conn, clock, srv = env
    srv.inject("batches", "http_413")
    drain(svc, conn, clock, steps=200, audio=False)
    big = conn.execute("SELECT * FROM outbox_batches WHERE state='quarantined'").fetchone()
    kids = conn.execute("SELECT * FROM outbox_batches WHERE replaces_batch_id=?", (big["batch_id"],)).fetchall()
    assert len(kids) == 2 and all(k["state"] == "acknowledged" for k in kids)
    orig = [json.dumps(r, sort_keys=True) for r in json.loads(big["payload"])["records"]]
    split = [json.dumps(r, sort_keys=True) for k in kids for r in json.loads(k["payload"])["records"]]
    assert sorted(orig) == sorted(split)


def test_retry_hints_rate_limit_and_malformed(env):
    svc, conn, clock, srv = env
    srv.inject("batches", "http_429:120")
    srv.inject("events", "malformed")
    svc.control_step(conn)
    row = conn.execute("SELECT * FROM outbox_batches ORDER BY first_second LIMIT 1").fetchone()
    assert row["state"] == "pending" and row["next_attempt_at"] >= clock.t + 119 and row["last_status"] == 429
    ev = conn.execute("SELECT * FROM event_revisions WHERE revision=1").fetchone()
    assert ev["delivery_state"] == "pending" and ev["last_error"].startswith("malformed_response")


def test_unknown_provenance_refreshes_configuration_then_retries(env):
    svc, conn, clock, srv = env
    srv.inject("batches", "code:unknown_provenance")
    svc.control_step(conn)
    row = conn.execute("SELECT * FROM outbox_batches ORDER BY first_second LIMIT 1").fetchone()
    assert row["state"] == "pending" and row["last_error"] == "unknown_provenance" and row["next_attempt_at"] >= clock.t + 29
    # the measurement chain is registered again (idempotent) before the batch is resubmitted
    assert conn.execute("SELECT COUNT(*) FROM provenance_records WHERE state='pending'").fetchone()[0] == 2
    clock.advance(31)
    svc.control_step(conn)
    calls = [p for _, p in srv.log]
    post = calls.index("/api/v1/device/measurements/batches")
    assert calls[post + 1:].count("/api/v1/device/provenance") == 1
    assert conn.execute("SELECT state FROM outbox_batches WHERE batch_id=?", (row["batch_id"],)).fetchone()[0] == "acknowledged"


def test_measurements_wait_for_chain_registration(env):
    svc, conn, clock, srv = env
    srv.inject("provenance", "http_503")
    svc.control_step(conn)
    calls = [p for _, p in srv.log]
    assert "/api/v1/device/provenance" in calls and "/api/v1/device/measurements/batches" not in calls
    assert "/api/v1/device/events" not in calls
    clock.advance(120)
    svc.control_step(conn)
    assert set(srv.profiles) == {PROFILE_IDS["calibrated"]} and set(srv.calibrations) == {CALIBRATION_IDS["calibrated"]}
    assert srv.batches  # sent once registered
    assert svc.lane_errors["provenance_last"] == "registered"


def test_conflicting_chain_registration_blocks_and_is_reported(env):
    svc, conn, clock, srv = env
    srv.profiles[PROFILE_IDS["calibrated"]] = ("other", {"channel": "mic-1"})
    svc.control_step(conn)
    st = svc.lane_errors["provenance_last"]
    assert st == "rejected:provenance_conflict" and not srv.batches
    summary = __import__("noise_collector.delivery.provenance", fromlist=["summary"]).summary(conn)
    assert summary["rejected"] == 2 and "provenance_conflict" in summary["last_rejection"]


def test_clock_future_rejection_holds_data(env):
    svc, conn, clock, srv = env
    srv.inject("batches", "code:clock_future_timestamp")
    svc.control_step(conn)
    row = conn.execute("SELECT * FROM outbox_batches ORDER BY first_second LIMIT 1").fetchone()
    assert row["state"] == "pending" and row["next_attempt_at"] >= clock.t + 599
    assert conn.execute("SELECT value FROM health_counters WHERE name='clock_rejections'").fetchone()[0] == 1


def test_tls_failure_and_redirect_are_retried_never_bypassed(env):
    svc, conn, clock, srv = env
    srv.inject("events", "tls", "redirect")
    svc._send_events(conn, clock.t)
    ev = conn.execute("SELECT * FROM event_revisions WHERE revision=1").fetchone()
    assert ev["delivery_state"] == "pending" and ev["last_error"] == "tls_error"
    clock.advance(400)
    svc._send_events(conn, clock.t)
    ev = conn.execute("SELECT * FROM event_revisions WHERE revision=1").fetchone()
    assert ev["last_error"] == "redirect_refused" and ev["delivery_state"] == "pending"


def test_expired_upload_url_requests_new_attempt_same_recording(env):
    svc, conn, clock, srv = env
    srv.inject("storage", "expire")
    drain(svc, conn, clock)
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    assert rec["delivery_state"] == "verified"
    atts = conn.execute("SELECT state FROM upload_attempts WHERE recording_id=? ORDER BY created_at", (rec["recording_id"],)).fetchall()
    assert [a[0] for a in atts] == ["expired", "completed"]
    assert list(srv.recordings) == [rec["recording_id"]]


def test_server_sha_mismatch_reuploads_when_local_bytes_intact(env):
    svc, conn, clock, srv = env
    srv.inject("storage", "corrupt_object")
    drain(svc, conn, clock)
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    assert rec["delivery_state"] == "verified" and rec["verified_sha256"] == rec["sha256"]
    assert len([r for r in srv.storage_requests if r.method == "PUT"]) == 2


def test_local_corruption_quarantines_and_never_rehashes(env):
    svc, conn, clock, srv = env
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    p = rec_path(svc, rec)
    data = bytearray(p.read_bytes())
    data[1000] ^= 0xFF
    p.write_bytes(bytes(data))
    drain(svc, conn, clock)
    rec2 = conn.execute("SELECT * FROM recordings").fetchone()
    assert rec2["delivery_state"] == "quarantined" and rec2["last_error"] == "local_file_hash_changed"
    assert rec2["sha256"] == rec["sha256"] and p.exists()


def test_lost_completion_response_queries_status_before_reupload(env):
    svc, conn, clock, srv = env
    srv.inject("complete", "drop_after_commit")
    drain(svc, conn, clock)
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    assert rec["delivery_state"] == "verified"
    assert len([r for r in srv.storage_requests if r.method == "PUT"]) == 1


def test_no_local_deletion_until_verified(env):
    svc, conn, clock, srv = env
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    retention.run(conn, svc.s.state_dir, svc.s.storage, "warning", clock.t + 10 * 86400)
    assert rec_path(svc, rec).exists()  # pending originals are never deleted, even under pressure
    drain(svc, conn, clock)
    retention.run(conn, svc.s.state_dir, svc.s.storage, "ok", clock.t + 3600, audio_hours=24)
    assert rec_path(svc, rec).exists()  # verified < 24 h ago
    retention.run(conn, svc.s.state_dir, svc.s.storage, "ok", clock.t + 25 * 3600, audio_hours=24)
    assert not rec_path(svc, rec).exists()
    receipt = conn.execute("SELECT * FROM deletion_receipts").fetchone()
    assert receipt["reference"] == rec["recording_id"] and receipt["sha256"] == rec["sha256"]


def test_event_unknown_on_server_requeues_same_identity(env):
    svc, conn, clock, srv = env
    drain(svc, conn, clock, steps=30, audio=False)
    srv.events.clear()  # server lost the event: declaration will 404
    srv.revisions.clear()
    drain(svc, conn, clock)
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    assert rec["delivery_state"] == "verified"
    assert set(srv.events) == {conn.execute("SELECT event_id FROM events").fetchone()[0]}


def test_recordings_disabled_by_account_quarantines_clip(env):
    svc, conn, clock, srv = env
    srv.recordings_enabled = False
    drain(svc, conn, clock)
    rec = conn.execute("SELECT * FROM recordings").fetchone()
    assert rec["delivery_state"] == "quarantined" and rec["last_error"] == "recordings_disabled"
    assert rec_path(svc, rec).exists()


def test_old_data_beyond_window_is_not_posted(env):
    svc, conn, clock, srv = env
    clock.t = START_UTC + 31 * 86400
    drain(svc, conn, clock, steps=20)
    assert "pending" not in states(conn, "measurements")
    assert states(conn, "measurements").get("expired_for_automatic_upload", 0) > 200
    assert not any(p.endswith("/batches") for _, p in srv.log)


def test_server_outside_window_rejection_expires_batch(env):
    svc, conn, clock, srv = env
    srv.inject("batches", "code:outside_backfill_window")
    drain(svc, conn, clock, steps=5, audio=False)
    assert "expired_for_automatic_upload" in states(conn, "outbox_batches", "state")


def test_batch_rate_limit_respected(env):
    svc, conn, clock, srv = env
    for _ in range(60):
        svc.control_step(conn)
        clock.advance(0.1)
    n = sum(1 for _, p in srv.log if p.endswith("/batches"))
    assert n <= 9


def test_configuration_staged_then_acknowledged_after_apply(env):
    svc, conn, clock, srv = env
    srv.config_result = configuration_result(2)
    svc._poll_config(conn)
    assert conn.execute("SELECT state FROM configurations WHERE revision=2").fetchone()[0] == "staged"
    svc._send_acks(conn, clock.t)
    assert srv.acks == []  # never acknowledged before the capture process applies it
    record_config_ack(conn, 2, "applied", "2026-10-08T00:00:00.000Z", None, None)
    svc._send_acks(conn, clock.t)
    assert srv.acks[-1] == {"schema_version": 1, "revision": 2, "status": "applied", "reason": None,
                            "applied_at": "2026-10-08T00:00:00.000Z", "content_hash": srv.config_result["sha256"]}
    record_config_ack(conn, 2, "applied", "2026-10-08T00:00:00.000Z", None, None)
    assert conn.execute("SELECT COUNT(*) FROM config_acknowledgments WHERE revision=2").fetchone()[0] == 1
    assert srv.schema_violations == []


def test_invalid_configurations_rejected_with_reason(env):
    svc, conn, clock, srv = env
    bad = configuration_result(3)
    bad["configuration"]["reporting_interval_seconds"] = 45  # content changed without a matching sha256
    srv.config_result = bad
    svc._poll_config(conn)
    svc._send_acks(conn, clock.t)
    assert srv.acks[-1]["status"] == "rejected" and srv.acks[-1]["reason"].startswith("hash_mismatch")
    lcpeak = configuration_result(4, detection={"baseline_relative": {"metric": "lcpeak_db"}})
    srv.config_result = reseal(lcpeak)
    svc._poll_config(conn)
    svc._send_acks(conn, clock.t)
    assert srv.acks[-1]["reason"].startswith("unsupported_trigger_metric")
    assert conn.execute("SELECT state FROM configurations WHERE revision=4").fetchone()[0] == "rejected"


def test_heartbeat_reports_backlog_and_honest_state(env):
    svc, conn, clock, srv = env
    hb = svc.build_heartbeat(conn, clock.t)
    assert hb.queued_measurement_count > 0 and hb.pending_audio_bytes > 0 and hb.pending_audio_count == 1
    assert "lcpeak_db" not in hb.capabilities.metrics and hb.capabilities.third_octave_bands is False
    assert hb.microphone_state == "error"  # no acquisition status file: never reported healthy
    assert hb.clock.sync_state == "unknown"
    assert hb.boot_id is None  # no live acquisition session: null, never a stale or invented id


def test_24h_outage_then_full_recovery_without_duplicates(env):
    svc, conn, clock, srv = env
    srv.down = True
    pending = conn.execute("SELECT COUNT(*) FROM measurements WHERE delivery_state='pending'").fetchone()[0]
    for _ in range(24 * 60):  # 24 h in 60 s steps
        svc.control_step(conn)
        svc.audio_step(conn)
        clock.advance(60)
    assert not srv.records and conn.execute("SELECT MAX(next_attempt_at) FROM outbox_batches").fetchone()[0] <= clock.t + 300
    srv.down = False
    drain(svc, conn, clock, steps=600)
    assert states(conn, "measurements").get("acknowledged") == pending
    assert len(srv.records) == pending
    assert conn.execute("SELECT delivery_state FROM recordings").fetchone()[0] == "verified"
