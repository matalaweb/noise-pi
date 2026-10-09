"""Server configuration -> effective configuration (contract/upstream GET /configuration)."""

import json
from pathlib import Path

import pytest

from noise_collector.contract.configuration import ConfigRejected, LocalCalibration, canonical_json, document_hash, translate
from noise_collector.contract.examples import CALIBRATION_IDS, configuration_result, local_inputs, reseal

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
        translate(res, local_inputs())
    assert e.value.code == "hash_mismatch"


def test_detection_and_recording_mapping():
    res = reseal(configuration_result(detection={"min_event_duration_ms": 2500, "merge_gap_ms": 4000,
                                                 "absolute": {"enabled": True, "metric": "lafmax_db", "level_db": 85.0},
                                                 "baseline_relative": {"baseline_window_seconds": 300}},
                                      recording={"format": "audio/flac", "post_roll_seconds": 2, "pre_roll_seconds": 60}))
    cfg = translate(res, local_inputs())
    rules = {r.id: r for r in cfg.detection.rules}
    assert rules["absolute"].threshold_db == 85.0 and rules["absolute"].consecutive_seconds == 3
    assert rules["baseline_relative"].delta_db == 12.0
    assert cfg.detection.quiet_seconds == 4 and cfg.detection.baseline.window_seconds == 300
    assert cfg.detection.baseline.min_eligible_seconds == 120
    assert cfg.recording.container == "flac" and cfg.recording.pre_roll_seconds == 60
    assert cfg.recording.post_roll_seconds == 4 and any("post-roll raised" in n for n in cfg.notes)


def test_calibrated_without_local_scale_applies_with_null_spl():
    cfg = translate(configuration_result(), local_inputs(with_scale=False))
    assert cfg.profile.mode == "calibrated" and cfg.profile.scale is None
    assert any("no absolute scale" in n for n in cfg.notes)


def test_local_scale_pinned_to_calibration_revision():
    local = local_inputs()
    local.calibrations[CALIBRATION_IDS["calibrated"]].content_hash = "f" * 64  # entry for an older revision
    cfg = translate(configuration_result(), local)
    assert cfg.profile.scale is None and any("different calibration revision" in n for n in cfg.notes)


def test_server_sensitivity_used_when_present():
    res = configuration_result(calibration_extra={"sensitivity_dbfs_at_94db": -26.0})
    cfg = translate(res, local_inputs(with_scale=False))
    assert cfg.profile.scale.method == "server_sensitivity"
    assert abs(cfg.profile.scale.pa_per_fs - 20e-6 * 10 ** (94 / 20) / 10 ** (-26 / 20)) < 1e-9


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r["configuration"]["channels"][0].update(channel="mic-9"), "channel_not_configured"),
    (lambda r: r["configuration"]["channels"][0].update(enabled=False), "channel_disabled"),
    (lambda r: r["configuration"]["channels"][0].update(bands_enabled=True), "unsupported_capability"),
    (lambda r: r["configuration"]["detection"]["baseline_relative"].update(metric="lcpeak_db"), "unsupported_trigger_metric"),
    (lambda r: r["provenance"].update(deployments=[]), "provenance_missing"),
])
def test_rejections(mutate, code):
    res = configuration_result()
    mutate(res)
    with pytest.raises(ConfigRejected) as e:
        translate(reseal(res), local_inputs())
    assert e.value.code == code


def test_uncalibrated_profile_with_calibration_rejected():
    res = configuration_result(mode="uncalibrated")
    res["configuration"]["channels"][0]["calibration_id"] = CALIBRATION_IDS["calibrated"]
    with pytest.raises(ConfigRejected):
        translate(reseal(res), local_inputs("uncalibrated"))


def test_upstream_configuration_fixture_translates():
    res = json.loads((UPSTREAM / "fixtures/responses/configuration-200.json").read_text())
    cal = res["provenance"]["calibrations"][0]
    cal["attachments"] = []  # the fixture's file checksum is illustrative; file handling is tested below
    local = local_inputs()
    local.calibrations = {cal["id"]: LocalCalibration(cal["id"], cal["content_hash"], sensitivity_dbfs_at_94db=-20.0)}
    cfg = translate(res, local, verify_hash=False)  # the fixture's sha256 is illustrative
    assert cfg.channel == "mic-1" and cfg.recording.container == "flac"
    assert cfg.detection.rules[0].metric == "lafmax_db" and cfg.detection.rules[0].delta_db == 15.0
    assert cfg.detection.quiet_seconds == 5 and cfg.profile.lf_band_hz == (20.0, 125.0)
    assert document_hash(res["configuration"]) != res["sha256"]


UMIK_FILE = (b'"Sens Factor =-0.7dB, AGain =18dB, SERNO: 7103946"\n'
             b"10.054\t-1.70\n20.0\t-0.80\n100.0\t-0.10\n1000.0\t0.00\n5000.0\t0.60\n10000.0\t1.20\n20000.0\t-1.50\n")


def with_file(tmp_path, files, serial="7103946", sensitivity=-25.3):
    import hashlib

    adir = tmp_path / "assets"
    adir.mkdir(exist_ok=True)
    atts = []
    for name, data in files:
        sha = hashlib.sha256(data).hexdigest()
        (adir / sha).write_bytes(data)
        atts.append({"id": "019a0f3d-7e7e-7000-8000-00000000c41f", "purpose": "frequency_response", "filename": name,
                     "byte_size": len(data), "sha256": sha, "download_path": "/api/v1/device/calibrations/x/attachments/y"})
    res = configuration_result(calibration_extra={"sensitivity_dbfs_at_94db": sensitivity, "attachments": atts})
    res["provenance"]["measurement_profiles"][0]["microphone_serial"] = serial
    local = local_inputs(with_scale=False)
    local.asset_dir = adir
    return res, local


def test_umik_calibration_file_drives_response_correction(tmp_path):
    res, local = with_file(tmp_path, [("7103946_90deg.txt", UMIK_FILE)])
    cfg = translate(res, local)
    rc = cfg.profile.response_correction
    assert rc.method == "fir_min_phase_v1" and rc.curve_is == "microphone_response" and rc.curve[0] == (10.054, -1.7)
    assert cfg.profile.scale.method == "server_sensitivity"
    assert any("7103946_90deg.txt" in n for n in cfg.notes)


def test_calibration_file_serial_must_match_profile(tmp_path):
    res, local = with_file(tmp_path, [("7103946.txt", UMIK_FILE)], serial="7000001")
    with pytest.raises(ConfigRejected) as e:
        translate(res, local)
    assert e.value.code == "calibration_serial_mismatch"


def test_orientation_selects_between_0_and_90_degree_files(tmp_path):
    other = UMIK_FILE.replace(b"1.20", b"2.40")
    res, local = with_file(tmp_path, [("7103946.txt", UMIK_FILE), ("7103946_90deg.txt", other)])
    cfg = translate(res, local)
    assert cfg.profile.response_correction.method == "none" and any("calibration_orientation" in n for n in cfg.notes)
    local.calibration_orientation = "90deg"
    cfg = translate(res, local)
    assert dict(cfg.profile.response_correction.curve)[10000.0] == 2.4
    local.calibration_orientation = "0deg"
    assert dict(translate(res, local).profile.response_correction.curve)[10000.0] == 1.2


def test_missing_or_tampered_calibration_file_rejected(tmp_path):
    res, local = with_file(tmp_path, [("7103946.txt", UMIK_FILE)])
    sha = res["provenance"]["calibrations"][0]["attachments"][0]["sha256"]
    (local.asset_dir / sha).write_bytes(UMIK_FILE + b"x")
    with pytest.raises(ConfigRejected) as e:
        translate(res, local)
    assert e.value.code == "asset_hash_mismatch"
    (local.asset_dir / sha).unlink()
    with pytest.raises(ConfigRejected) as e:
        translate(res, local)
    assert e.value.code == "asset_unavailable"
