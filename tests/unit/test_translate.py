"""Server configuration documents (contract/upstream GET /configuration) and local defaults."""

import json
from pathlib import Path

import pytest

from noise_collector.contract.configuration import ConfigRejected, canonical_json, document_hash, effective, local_defaults, parse_document
from noise_collector.contract.examples import configuration_result, example_profile, local_inputs, reseal

UPSTREAM = Path(__file__).resolve().parents[2] / "contract" / "upstream"


def test_canonical_json_matches_php_json_encode():
    # Captured from the web app container: php -r 'echo json_encode([...], JSON_PRESERVE_ZERO_FRACTION|...)'
    values = [1e15, 1e14, 999999999999999.0, 1e16, 0.0001, 0.00001, 1.5e-7, 123456789012345678.0, 15.0, 0.1, -0.0,
              1e-10, 2.5e20, 1.0e100, 12.34e5, 100.0]
    php = ("[1000000000000000.0,100000000000000.0,999999999999999.0,10000000000000000.0,0.0001,1.0e-5,1.5e-7,"
           "1.2345678901234568e+17,15.0,0.1,-0.0,1.0e-10,2.5e+20,1.0e+100,1234000.0,100.0]")
    assert canonical_json(values).decode() == php
    assert canonical_json({"a/b": "é" + chr(0x2028) + "x", "e": [], "ctl": "\x01\t\"\\"}).decode() == \
        '{"a/b":"é\\u2028x","ctl":"\\u0001\\t\\"\\\\","e":[]}'
    assert canonical_json({"z": 1, "a": {}, "m": [True, None]}) == b'{"a":[],"m":[true,null],"z":1}'


def test_tampered_document_rejected():
    res = configuration_result()
    res["configuration"]["reporting_interval_seconds"] = 31
    with pytest.raises(ConfigRejected) as e:
        parse_document(res, local_inputs())
    assert e.value.code == "hash_mismatch"


def test_detection_and_recording_mapping():
    res = reseal(configuration_result(detection={"min_event_duration_ms": 2500, "merge_gap_ms": 4000,
                                                 "absolute": {"enabled": True, "metric": "lafmax_db", "level_db": 85.0},
                                                 "baseline_relative": {"baseline_window_seconds": 300}},
                                      recording={"format": "audio/flac", "post_roll_seconds": 2, "pre_roll_seconds": 60}))
    cfg = parse_document(res, local_inputs())
    rules = {r.id: r for r in cfg.detection.rules}
    assert rules["absolute"].threshold_db == 85.0 and rules["absolute"].consecutive_seconds == 3
    assert rules["baseline_relative"].delta_db == 12.0
    assert cfg.detection.quiet_seconds == 4 and cfg.detection.baseline.window_seconds == 300
    assert cfg.detection.baseline.min_eligible_seconds == 120
    assert cfg.recording.container == "flac" and cfg.recording.pre_roll_seconds == 60
    assert cfg.recording.post_roll_seconds == 4 and any("post-roll raised" in n for n in cfg.notes)


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r["configuration"]["channels"][0].update(channel="mic-9"), "channel_not_configured"),
    (lambda r: r["configuration"]["channels"][0].update(enabled=False), "channel_disabled"),
    (lambda r: r["configuration"]["channels"][0].update(bands_enabled=True), "unsupported_capability"),
    (lambda r: r["configuration"]["detection"]["baseline_relative"].update(metric="lcpeak_db"), "unsupported_trigger_metric"),
    (lambda r: r["configuration"]["detection"]["absolute"].update(enabled=True, level_db=None), "invalid_detection"),
])
def test_rejections(mutate, code):
    res = configuration_result()
    mutate(res)
    with pytest.raises(ConfigRejected) as e:
        parse_document(reseal(res), local_inputs())
    assert e.value.code == code


def test_legacy_documents_with_provenance_references_still_parse():
    # Revisions published before the device reported its own chain reference server profiles.
    res = configuration_result()
    res["configuration"]["channels"][0].update(measurement_profile_id="01a120fe-dc58-725f-8735-3136afa41523",
                                               deployment_id="01a12100-56f5-70bc-b2a0-32174e4f32df",
                                               calibration_id="01a1210c-71b6-7210-9177-e445c96b39e8", calibration_state="estimated")
    res["provenance"] = {"measurement_profiles": [{"id": "x"}], "deployments": [], "calibrations": []}
    op = parse_document(reseal(res), local_inputs())
    assert op.revision == 1 and op.channel == "mic-1"


def test_local_defaults_measure_without_rules():
    op = local_defaults(local_inputs())
    assert op.revision is None and op.sha256 is None and op.is_local_defaults
    assert op.detection.rules == [] and op.delivery.measurement_batch_seconds == 30 and op.recording.container == "flac"
    cfg = effective(op, example_profile("estimated"))
    assert cfg.revision is None and cfg.profile.mode == "estimated" and cfg.reports("laeq_db") and not cfg.reports("lcpeak_db")


def test_configured_metrics_beyond_the_profile_are_noted():
    op = parse_document(configuration_result(mode="calibrated"), local_inputs())
    cfg = effective(op, example_profile("uncalibrated"))
    assert not cfg.reports("laeq_db") and cfg.reports("rms_dbfs")
    assert any("not reported by this profile" in n for n in cfg.notes)


def test_upstream_configuration_fixture_parses():
    res = json.loads((UPSTREAM / "fixtures/responses/configuration-200.json").read_text())
    op = parse_document(res, local_inputs(), verify_hash=False)  # the fixture's sha256 is illustrative
    assert op.channel == "mic-1" and op.recording.container == "flac"
    assert op.detection.rules[0].metric == "lafmax_db" and op.detection.rules[0].delta_db == 15.0
    assert op.detection.quiet_seconds == 5
    assert document_hash(res["configuration"]) != res["sha256"]
