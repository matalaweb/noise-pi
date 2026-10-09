"""The device's own measurement chain (config/chain.py; contract/device-reported-provenance.md)."""

import base64
import hashlib

import pytest

from noise_collector.config.chain import ChainError, build_chain, installation_id, store_records
from noise_collector.contract.models import ProvenanceRegistration
from noise_collector.dsp.calibration import P0
from noise_collector.store.db import connect, migrate

UMIK_FILE = (b'"Sens Factor =-0.082dB, SERNO: 7213485"\n'
             b"10.054\t-3.17\n20.0\t-0.80\n100.0\t-0.10\n1000.0\t0.00\n5000.0\t0.60\n10000.0\t1.20\n20000.0\t-1.50\n")
INSTALL = "8c0b1f2e-3d4a-4b5c-9d6e-7f8091a2b3c4"


def umik_settings(make_settings, tmp_path, **cal):
    f = tmp_path / "7213485_90deg.txt"
    f.write_bytes(UMIK_FILE)
    return make_settings(
        microphone={"model": "umik-1", "usb_serial": None, "microphone_model": None, "gain_reference_check": None},
        calibration={"state": "estimated", "frequency_response_file": str(f), "sensitivity_dbfs_at_94db": None,
                     "reference_method": None, **cal},
    )


def test_umik_file_supplies_serial_sensitivity_and_correction(make_settings, tmp_path):
    chain = build_chain(umik_settings(make_settings, tmp_path), INSTALL)
    p = chain.profile
    assert p.microphone_model == "miniDSP UMIK-1" and p.microphone_serial == "7213485" and p.mode == "estimated"
    # REW convention: 94 dB SPL reads Sens Factor - 30 dBFS
    sens = 20 * __import__("math").log10(10 ** (94 / 20) * P0 / p.scale.pa_per_fs)
    assert sens == pytest.approx(-30.082, abs=1e-6) and p.scale.method == "comparison_estimate"
    assert p.response_correction.method == "fir_min_phase_v1" and p.response_correction.curve[0] == (10.054, -3.17)
    prof = chain.record("measurement_profile")["record"]
    cal = chain.record("calibration")["record"]
    assert prof["id"] == p.profile_id and cal["id"] == p.calibration_id
    assert prof["microphone_serial"] == "7213485" and prof["calibration_state"] == "estimated"
    assert prof["supported_metrics"] == ["laeq_db", "lafmax_db", "lceq_db", "low_frequency_leq_db", "rms_dbfs"]
    assert prof["gain_description"] == "UMIK-1 analog gain not reported; ALSA Mic 0.00 dB"
    assert len(prof["weighting_implementation_version"]) <= 64 and len(prof["filter_implementation_version"]) <= 64
    assert cal["sensitivity_dbfs_at_94db"] == pytest.approx(-30.082) and "Sens Factor -0.082" in cal["reference_method"]
    att = cal["attachments"][0]
    assert att["filename"] == "7213485_90deg.txt" and att["sha256"] == hashlib.sha256(UMIK_FILE).hexdigest()
    assert base64.b64decode(att["content_base64"]) == UMIK_FILE
    ProvenanceRegistration(sent_at="2026-10-09T15:00:00.000Z", measurement_profiles=[prof], calibrations=[cal])


def test_ids_are_stable_and_change_with_content(make_settings, tmp_path):
    s = umik_settings(make_settings, tmp_path)
    a, b = build_chain(s, INSTALL), build_chain(s, INSTALL)
    assert a.profile.profile_id == b.profile.profile_id and a.profile.calibration_id == b.profile.calibration_id
    other_install = build_chain(s, "00000000-0000-4000-8000-000000000000")
    assert other_install.profile.profile_id != a.profile.profile_id
    measured = build_chain(umik_settings(make_settings, tmp_path, sensitivity_dbfs_at_94db=-29.5), INSTALL)
    assert measured.profile.calibration_id != a.profile.calibration_id
    assert measured.profile.profile_id == a.profile.profile_id  # the profile itself did not change


def test_file_for_another_microphone_is_refused(make_settings, tmp_path):
    s = umik_settings(make_settings, tmp_path)
    s.microphone.microphone_serial = "7213486"
    with pytest.raises(ChainError, match="is for serial 7213485"):
        build_chain(s, INSTALL)


def test_calibrated_chain_needs_a_reference_and_a_scale(make_settings, tmp_path):
    with pytest.raises(ChainError, match="reference_method"):
        build_chain(umik_settings(make_settings, tmp_path, state="calibrated", sensitivity_dbfs_at_94db=-29.4), INSTALL)
    s = make_settings(calibration={"state": "estimated", "sensitivity_dbfs_at_94db": None})
    with pytest.raises(ChainError, match="needs sensitivity_dbfs_at_94db"):
        build_chain(s, INSTALL)


def test_uncalibrated_chain_has_no_calibration_record(make_settings):
    chain = build_chain(make_settings(calibration={"state": "uncalibrated", "sensitivity_dbfs_at_94db": None}), INSTALL)
    assert chain.profile.calibration_id is None and chain.profile.scale is None
    assert chain.profile.supported_metrics == ("rms_dbfs",) and [r["kind"] for r in chain.records] == ["measurement_profile"]


def test_records_queue_once_and_installation_id_is_stable(make_settings, tmp_path):
    s = umik_settings(make_settings, tmp_path)
    migrate(s.db_path)
    conn = connect(s.db_path)
    install = installation_id(conn)
    assert installation_id(conn) == install
    chain = build_chain(s, install)
    store_records(conn, chain)
    store_records(conn, chain)
    rows = conn.execute("SELECT kind, state FROM provenance_records ORDER BY kind").fetchall()
    assert [(r["kind"], r["state"]) for r in rows] == [("calibration", "pending"), ("measurement_profile", "pending")]
