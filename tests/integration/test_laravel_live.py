"""End-to-end contract test against a real running Laravel app (skipped unless configured).

Runs the real collector pipeline: fetch + verify + stage the server configuration, replay
SYNTHETIC audio through the engine with that configuration, then deliver measurements, event
revisions, the acknowledgment, a heartbeat and the event recording (declare -> presigned PUT ->
complete -> server verification), and checks idempotent replay.

    scripts/run_live_contract_test.sh   # provisions a demo device in the local Docker stack and runs this

Environment: NOISE_LARAVEL_URL, NOISE_LARAVEL_TOKEN, NOISE_LARAVEL_STORAGE_HOSTS (comma list),
optional NOISE_LARAVEL_CHANNEL (default mic-1). Use a throwaway/demo device: data is SYNTHETIC.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from noise_collector.acquisition.durability import record_config_ack
from noise_collector.acquisition.replay import replay
from noise_collector.config.local_inputs import local_inputs
from noise_collector.config.settings import Settings
from noise_collector.contract.configuration import translate
from noise_collector.delivery.service import DeliveryService
from noise_collector.store.db import connect, migrate
from noise_collector.synth import Burst, Scenario, write_wav
from noise_collector.timeutil import iso_utc

URL = os.environ.get("NOISE_LARAVEL_URL")
TOKEN = os.environ.get("NOISE_LARAVEL_TOKEN")
pytestmark = [pytest.mark.laravel, pytest.mark.skipif(not (URL and TOKEN), reason="NOISE_LARAVEL_URL/TOKEN not set")]


def counts(conn, table, col="delivery_state"):
    return {r[0]: r[1] for r in conn.execute(f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}")}


def test_end_to_end_against_real_laravel(tmp_path: Path):
    cred = tmp_path / "cred.toml"
    cred.write_text(f'device_token = "{TOKEN}"\n')
    os.chmod(cred, 0o600)
    cal_file = tmp_path / "calibrations.toml"
    s = Settings.model_validate({
        "paths": {"state_dir": str(tmp_path / "state"), "credentials_file": str(cred), "calibrations_file": str(cal_file)},
        "server": {"base_url": URL, "allow_insecure_http_for_tests": URL.startswith("http:"),
                   "trusted_storage_hosts": [h for h in os.environ.get("NOISE_LARAVEL_STORAGE_HOSTS", "").split(",") if h]},
        "channel": {"id": os.environ.get("NOISE_LARAVEL_CHANNEL", "mic-1")},
        "microphone": {"gain_reference_check": "live contract test: SYNTHETIC audio, no physical gain"},
    })
    s.state_dir.mkdir(parents=True)
    migrate(s.db_path)
    svc = DeliveryService(s, TOKEN)
    conn = connect(s.db_path)

    # 1. configuration: fetch, verify canonical hash, translate, stage
    svc._poll_config(conn)
    row = conn.execute("SELECT revision, document_json FROM configurations WHERE state='staged'").fetchone()
    assert row is not None, svc.lane_errors
    result = json.loads(row["document_json"])
    from noise_collector.contract.configuration import document_hash

    assert document_hash(result["configuration"]) == result["sha256"]  # served document reproduces its hash
    cals = [c for c in result["provenance"]["calibrations"] if c.get("sensitivity_dbfs_at_94db") is None]
    if cals:  # SYNTHETIC scale for calibrations without a server sensitivity, pinned to its content hash
        cal_file.write_text("".join(
            f'[[calibration]]\nid = "{c["id"]}"\ncontent_hash = "{c["content_hash"]}"\nmethod = "reference_measurement"\n'
            f"sensitivity_dbfs_at_94db = -18.0\n\n" for c in cals))
    cfg = translate(result, local_inputs(s))
    assert cfg.profile.mode == "uncalibrated" or cfg.profile.scale is not None
    if os.environ.get("NOISE_LARAVEL_EXPECT_CALIBRATION_FILE"):
        assert any(c.get("attachments") for c in result["provenance"]["calibrations"])
    if any(c.get("attachments") for c in result["provenance"]["calibrations"]):
        # calibration file downloaded from the server, hash-verified, serial-checked and applied
        assert cfg.profile.scale.method == "server_sensitivity"
        assert cfg.profile.response_correction.method == "fir_min_phase_v1"
        assert list((s.state_dir / "profiles").iterdir())

    # 2. capture: SYNTHETIC audio ending a minute ago, processed by the real engine
    wav = tmp_path / "synthetic.wav"
    write_wav(str(wav), Scenario(duration_s=240, background_dbfs=-60, bursts=[Burst(150, 20, "engine_like", -30)], seed=9).render())
    start = float(int(time.time()) - 300)
    replay(str(wav), s.state_dir, cfg, start_utc=start)
    record_config_ack(conn, cfg.revision, "applied", iso_utc(time.time()), None, "; ".join(cfg.notes) or None, cfg.sha256)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= 1

    # 3. delivery until everything is acknowledged and verified
    deadline = time.time() + 300
    while time.time() < deadline:
        svc.control_step(conn)
        svc.audio_step(conn)
        m, e, r = counts(conn, "measurements"), counts(conn, "event_revisions"), counts(conn, "recordings")
        if not m.get("pending") and not m.get("batched") and set(e) == {"acknowledged"} and set(r) <= {"verified"}:
            break
        time.sleep(0.3)
    m, e, r = counts(conn, "measurements"), counts(conn, "event_revisions"), counts(conn, "recordings")
    errors = [dict(x) for x in conn.execute("SELECT recording_id, delivery_state, last_error FROM recordings WHERE delivery_state != 'verified'")]
    errors += [dict(x) for x in conn.execute("SELECT batch_id, state, last_status, last_error FROM outbox_batches WHERE state != 'acknowledged'")]
    errors += [dict(x) for x in conn.execute("SELECT event_id, revision, delivery_state, last_error FROM event_revisions WHERE delivery_state != 'acknowledged'")]
    assert not m.get("quarantined") and not m.get("pending") and not m.get("batched"), (m, errors)
    assert set(e) == {"acknowledged"}, (e, errors)
    assert r and set(r) == {"verified"}, (r, errors)
    for rec in conn.execute("SELECT * FROM recordings"):
        assert rec["verified_sha256"] == rec["sha256"]
    assert conn.execute("SELECT delivery_state FROM config_acknowledgments").fetchone()[0] == "acknowledged"
    assert svc.last_heartbeat_success is not None

    # 4. idempotency: resending an acknowledged batch is a replay, never a duplicate
    payload = conn.execute("SELECT payload FROM outbox_batches WHERE state='acknowledged' LIMIT 1").fetchone()["payload"]
    from noise_collector.contract.models import BatchResult

    out = svc.api.request("POST", "/api/v1/device/measurements/batches", content=payload.encode(), model=BatchResult)
    assert out.ok and out.model.replayed is True and out.model.inserted_count + out.model.duplicate_count == out.model.record_count
