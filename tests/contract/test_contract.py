"""Contract tests against the vendored upstream Laravel OpenAPI (contract/upstream).

* every payload the collector emits validates against the upstream JSON Schemas;
* every upstream example (request and response) is accepted by the collector's models;
* the vendored copy matches the web app checkout when one is present (drift gate).
"""

from __future__ import annotations

import filecmp
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from conftest import START_UTC
from noise_collector.acquisition.durability import record_config_ack
from noise_collector.acquisition.replay import replay
from noise_collector.audio.file_source import Faults
from noise_collector.contract import models
from noise_collector.contract.configuration import ConfigurationResult
from noise_collector.contract.examples import configuration_result, example_configuration
from noise_collector.delivery.outbox import build_batches
from noise_collector.store.db import connect
from noise_collector.synth import Burst, Scenario, write_wav
from support.upstream import errors

ROOT = Path(__file__).resolve().parents[2]
UP = ROOT / "contract" / "upstream"
WEB_APP = Path(os.environ.get("NOISE_WEB_APP_DIR", ROOT.parent / "my-neighbor-sucks"))

PLACEHOLDERS = {"{{DEPLOYMENT_ID}}": "ab6de18e-f8aa-4444-aed3-71d3464f07ea", "{{PROFILE_ID}}": "2fc235ef-6f5b-48d1-8d35-083dfdd5a6e9",
                "{{CALIBRATION_ID}}": "0e745c21-fc8f-40da-9113-7a158c913fc6", "{{BOOT_ID}}": "c97dba94-d806-45bd-b14f-34d44a10f534",
                "{{ATTEMPT_ID}}": "019a0f3d-1b2d-7000-8000-000000000001", "{{CONFIGURATION_SHA256}}": "3b" * 32}


def upstream(name: str):
    text = (UP / "fixtures" / name).read_text().replace('"{{CONFIGURATION_REVISION}}"', "4")
    for k, v in PLACEHOLDERS.items():
        text = text.replace(k, v)
    return json.loads(text)


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    """Payloads from real replays: calibrated WAV, uncalibrated FLAC, and an event cut by data loss."""
    d = tmp_path_factory.mktemp("produced")
    wav = d / "s.wav"
    write_wav(str(wav), Scenario(duration_s=240, background_dbfs=-60, bursts=[Burst(150, 20, "engine_like", -33)], seed=2).render())
    out: dict[str, list] = {"batches": [], "events": [], "declarations": []}
    runs = [
        ("cal", example_configuration(), None),
        ("uncal_flac", example_configuration(mode="uncalibrated", recording={"format": "audio/flac"}), None),
        ("cut", example_configuration(), Faults(drop={160 * 48000: 9600})),
    ]
    for name, cfg, faults in runs:
        state = d / name
        replay(str(wav), state, cfg, start_utc=START_UTC, faults=faults)
        conn = connect(state / "collector.db")
        build_batches(conn, START_UTC + 400, force=True)
        out["batches"] += [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox_batches")]
        out["events"] += [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM event_revisions")]
        for r in conn.execute("SELECT * FROM recordings WHERE state='finalized'"):
            fmt = json.loads(r["format_json"])
            out["declarations"].append(models.RecordingDeclaration(
                recording_id=r["recording_id"], segment_number=r["segment_number"], capture_started_at=r["capture_started_at"],
                duration_ms=r["duration_ms"], mime_type=fmt["mime_type"], codec=fmt["codec"], sample_rate_hz=fmt["sample_rate"],
                bit_depth=fmt["bit_depth"], byte_size=r["size_bytes"], sha256=r["sha256"]).model_dump())
        record_config_ack(conn, 1, "applied", "2026-09-21T14:13:20.000Z", None, "note", "ab" * 32)
        record_config_ack(conn, 2, "rejected", None, "hash_mismatch", "detail", None)
        out.setdefault("acks", []).extend(json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM config_acknowledgments"))
    return out


def test_measurement_batches_validate_upstream(produced):
    assert produced["batches"]
    for b in produced["batches"]:
        assert errors("MeasurementBatchRequest", b) == []


def test_event_revisions_validate_upstream(produced):
    states = {e["detection_state"] for e in produced["events"]}
    assert states == {"open", "finalized"}
    assert any("incomplete_interval" in e["quality_flags"] for e in produced["events"])
    for e in produced["events"]:
        assert errors("EventRevisionRequest", e) == []
        if e["detection_state"] == "finalized":
            assert e["ended_at"] is not None


def test_recording_declarations_validate_upstream(produced):
    mimes = {d["mime_type"] for d in produced["declarations"]}
    assert mimes == {"audio/wav", "audio/flac"}
    for d in produced["declarations"]:
        assert errors("RecordingDeclaration", d) == []


def test_small_payloads_validate_upstream(produced):
    for a in produced["acks"]:
        assert errors("ConfigurationAcknowledgment", a) == []
    assert errors("RecordingCompletion", models.RecordingCompletion(attempt_id=str(uuid.uuid4())).model_dump()) == []
    hb = models.Heartbeat(
        sent_at="2026-10-08T12:16:00.000Z", agent_version="noise-collector 0.1.0", boot_id=str(uuid.uuid4()), uptime_seconds=1,
        capabilities=models.Capabilities(channels=["mic-1"], metrics=["laeq_db", "rms_dbfs"], third_octave_bands=False,
                                         recording_formats=["audio/wav"], max_sample_rate_hz=48000),
        microphone_state="ok", free_disk_bytes=1, total_disk_bytes=2, queued_measurement_count=0, pending_audio_bytes=0,
        pending_audio_count=0, oldest_pending_capture_at=None, desired_config_revision=1, applied_config_revision=1,
        clock=models.Clock(sync_state="synchronized", offset_ms=1, source="adjtimex"), recent_dropped_intervals=0,
        last_capture_error=None)
    assert errors("Heartbeat", json.loads(hb.model_dump_json())) == []


@pytest.mark.parametrize("name,model", [
    ("measurement-batch.json", models.MeasurementBatch),
    ("measurement-batch-flagged.json", models.MeasurementBatch),
    ("event-open.json", models.EventRevision),
    ("event-finalized.json", models.EventRevision),
    ("recording-declaration.json", models.RecordingDeclaration),
    ("recording-completion.json", models.RecordingCompletion),
    ("heartbeat.json", models.Heartbeat),
    ("configuration-acknowledgment.json", models.ConfigAck),
])
def test_upstream_request_examples_accepted_by_collector_models(name, model):
    model.model_validate(upstream(name))


@pytest.mark.parametrize("name,model", [
    ("responses/measurement-batch-201.json", models.BatchResult),
    ("responses/event-201.json", models.EventResult),
    ("responses/recording-declaration-201.json", models.RecordingUploadResult),
    ("responses/recording-complete-202.json", models.RecordingStatus),
    ("responses/recording-status-verified-200.json", models.RecordingStatus),
    ("responses/heartbeat-200.json", models.HeartbeatResult),
    ("responses/validation-422.json", models.ErrorEnvelope),
    ("responses/measurement-conflict-409.json", models.ErrorEnvelope),
    ("responses/configuration-200.json", ConfigurationResult),
])
def test_upstream_responses_parse(name, model):
    model.model_validate(upstream(name))


def test_example_configuration_result_matches_upstream_shape():
    res = configuration_result()
    assert errors("ConfigurationResult", res) == []


@pytest.mark.skipif(not (WEB_APP / "docs" / "openapi").exists(), reason="web app checkout not present")
def test_vendored_contract_matches_web_app():
    src = WEB_APP / "docs" / "openapi"
    cmp = filecmp.dircmp(src, UP)
    diffs = cmp.diff_files + [f"fixtures/{f}" for f in filecmp.dircmp(src / "fixtures", UP / "fixtures").diff_files]
    assert diffs == [], f"upstream contract changed: {diffs}; re-vendor (scripts/sync_contract.sh) and review"


def test_coefficients_reproducible_script():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "design_filters.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
