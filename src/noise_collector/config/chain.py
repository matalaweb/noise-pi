"""The device's own measurement chain: profile + calibration, built from local settings.

The collector is the source of truth for how it measures (contract/device-reported-provenance.md).
``build_chain`` turns ``[microphone]``, ``[measurement]``, ``[calibration]`` and the local
frequency-response file into

* the engine ``Profile`` (scale, response correction, metrics, capture format), and
* the wire records registered with ``POST /api/v1/device/provenance``.

Record ids are UUIDv5 values derived from this installation's id and the record content, so the
acquisition and delivery processes agree on them without coordination, an unchanged chain keeps its
ids across restarts, and any change (another calibration file, a new sensitivity) yields a new
immutable record. A UMIK-1 calibration file supplies the microphone serial (``SERNO``) and, for an
*estimated* chain, the sensitivity (REW convention, docs/umik1.md).
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..audio import umik1
from ..contract.configuration import (
    COMPUTED_METRICS,
    CaptureSpec,
    GainSpec,
    NoiseFloorSpec,
    Profile,
    ResponseCorrectionSpec,
    ScaleSpec,
    document_hash,
)
from ..dsp.calibration import parse_calibration_file, scale_from_sensitivity
from ..dsp.filters import lf_filter, load_filter
from ..store.db import get_meta, set_meta, transaction
from ..timeutil import iso_utc
from .settings import Settings

ID_NAMESPACE = uuid.UUID("6f1c2a3e-9d1b-5c7a-8e4f-2b6d0a9c1e75")
INSTALLATION_KEY = "installation_id"
MAX_FILE_BYTES = 1024 * 1024


class ChainError(ValueError):
    """The local measurement chain is unusable (shown in status and `doctor`)."""


@dataclass(frozen=True)
class LocalChain:
    profile: Profile
    records: tuple[dict, ...]  # wire records: {"kind", "id", "content_hash", "record"}
    notes: tuple[str, ...] = ()

    def record(self, kind: str) -> dict | None:
        return next((r for r in self.records if r["kind"] == kind), None)


def installation_id(conn: sqlite3.Connection) -> str:
    """A random id for this installation, created once (both processes race safely)."""
    current = get_meta(conn, INSTALLATION_KEY)
    if current:
        return current
    with transaction(conn):
        current = get_meta(conn, INSTALLATION_KEY)
        if not current:
            current = str(uuid.uuid4())
            set_meta(conn, INSTALLATION_KEY, current)
    return current


def _record_id(install_id: str, kind: str, content_hash: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"{install_id}|{kind}|{content_hash}"))


def _content_hash(record: dict) -> str:
    """Hash of everything but the id and attachment bytes (a file is represented by its sha256)."""
    body = {k: v for k, v in record.items() if k != "id"}
    if "attachments" in body:
        body["attachments"] = [{k: v for k, v in a.items() if k != "content_base64"} for a in body["attachments"]]
    return document_hash(body)


def _versions(sample_rate: int, band: tuple[float, float]) -> tuple[str, str]:
    a, c = load_filter("A", sample_rate), load_filter("C", sample_rate)
    lf = lf_filter(sample_rate, band)
    weighting = f"noise-collector A/C v1 A:{a.sha256[:8]} C:{c.sha256[:8]}"
    filters = f"noise-collector LF v1 {band[0]:g}-{band[1]:g} Hz:{lf.sha256[:8]}"
    return weighting, filters


def build_chain(settings: Settings, install_id: str) -> LocalChain:
    mic, meas, cal = settings.microphone, settings.measurement, settings.calibration
    is_umik = mic.model == "umik-1"
    notes: list[str] = []
    state = cal.state

    # Frequency-response file (optional; a UMIK-1 file also identifies the microphone).
    curve_file = None
    file_bytes = b""
    header: dict[str, str] = {}
    if cal.frequency_response_file is not None:
        path = Path(cal.frequency_response_file)
        try:
            file_bytes = path.read_bytes()
        except OSError as exc:
            raise ChainError(f"[calibration] frequency_response_file {path}: {exc.strerror or exc}") from exc
        if len(file_bytes) > MAX_FILE_BYTES:
            raise ChainError(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        try:
            curve_file = parse_calibration_file(file_bytes)
        except ValueError as exc:
            raise ChainError(f"{path.name}: {exc}") from exc
        header = curve_file.header
    hdr = umik1.cal_header(header)

    serial = mic.microphone_serial
    if hdr.serno:
        if serial and serial != hdr.serno:
            raise ChainError(f"{Path(cal.frequency_response_file or '').name} is for serial {hdr.serno} but "
                             f"[microphone] microphone_serial is {serial}: use the file for this microphone")
        serial = hdr.serno
    model = mic.microphone_model or ("miniDSP UMIK-1" if is_umik else None)
    if not model:
        raise ChainError("set [microphone] microphone_model (reported in the measurement profile)")

    # Absolute scale.
    scale = None
    sensitivity = cal.sensitivity_dbfs_at_94db
    reference_method = cal.reference_method
    if state != "uncalibrated":
        if sensitivity is None and cal.pa_per_fs is None and state == "estimated" and is_umik and hdr.sens_factor_db is not None:
            sensitivity = umik1.sensitivity_estimate(hdr.sens_factor_db)["sensitivity_dbfs_at_94db"]
            reference_method = reference_method or (f"UMIK-1 calibration file Sens Factor {hdr.sens_factor_db:g} dB "
                                                    "(REW convention: 94 dB SPL reads Sens Factor - 30 dBFS)")
            notes.append(f"estimated sensitivity {sensitivity:g} dBFS @ 94 dB from the calibration file's Sens Factor")
        if sensitivity is not None:
            scale = ScaleSpec(method="reference_measurement" if state == "calibrated" else "comparison_estimate",
                              pa_per_fs=scale_from_sensitivity(sensitivity, 94.0), source="local [calibration]")
        elif cal.pa_per_fs is not None:
            scale = ScaleSpec(method="reference_measurement" if state == "calibrated" else "comparison_estimate",
                              pa_per_fs=cal.pa_per_fs, source="local [calibration]")
        else:
            hint = " (or a UMIK-1 frequency_response_file with a Sens Factor)" if is_umik and state == "estimated" else ""
            raise ChainError(f"[calibration] state = \"{state}\" needs sensitivity_dbfs_at_94db or pa_per_fs{hint}; "
                             "or set state = \"uncalibrated\"")
        if not reference_method:
            if state == "calibrated":
                raise ChainError("a calibrated chain needs [calibration] reference_method (e.g. \"94 dB / 1 kHz acoustic calibrator\")")
            reference_method = "owner-entered sensitivity (estimated)"
        has_gain_check = bool(mic.expected_gain_controls) or bool(mic.gain_reference_check) or is_umik
        if state == "calibrated" and not has_gain_check:
            notes.append("calibrated chain without gain read-back or reference check: SPL withheld")
            scale = None

    # Response correction from the file (only meaningful with an absolute scale).
    correction = ResponseCorrectionSpec()
    if curve_file is not None and cal.apply_frequency_response and state != "uncalibrated":
        correction = ResponseCorrectionSpec(
            method="fir_min_phase_v1",
            curve=tuple(zip(map(float, curve_file.freqs_hz), map(float, curve_file.response_db))),
            curve_is=cal.curve_is,
            source_sha256=curve_file.sha256,
        )
        notes.append(f"response correction from {Path(cal.frequency_response_file or '').name} (sha256 {curve_file.sha256[:12]}...)")
    noise_floor = None
    if cal.noise_floor_laeq_db is not None and cal.noise_floor_method:
        noise_floor = NoiseFloorSpec(laeq_db=cal.noise_floor_laeq_db, method=cal.noise_floor_method)

    band = (float(meas.low_frequency_band_hz[0]), float(meas.low_frequency_band_hz[1]))
    if band[1] >= meas.sample_rate_hz / 2:
        raise ChainError(f"low-frequency band {band} is not below Nyquist")
    weighting_v, filter_v = _versions(meas.sample_rate_hz, band)
    metrics = list(COMPUTED_METRICS) if state != "uncalibrated" else ["rms_dbfs"]
    again = hdr.again_db
    gain_description = None
    if is_umik:
        gain_description = (f"UMIK-1 analog {again:g} dB" if again is not None else "UMIK-1 analog gain not reported") + "; ALSA Mic 0.00 dB"
    elif mic.expected_gain_controls:
        gain_description = "; ".join(f"{k}={v}" for k, v in sorted(mic.expected_gain_controls.items()))[:255]
    application = None
    if state != "uncalibrated":
        application = "sensitivity offset dBFS->SPL" + ("; min-phase FIR frequency-response correction (0 dB at 1 kHz)"
                                                         if correction.method != "none" else "")

    profile_rec = {
        "channel": settings.channel.id,
        "name": meas.name,
        "microphone_model": model,
        "microphone_serial": serial,
        "audio_interface": mic.audio_interface or ("USB (built-in)" if is_umik else None),
        "sample_rate_hz": meas.sample_rate_hz,
        "gain_db": None,
        "gain_description": gain_description,
        "weighting_implementation_version": weighting_v,
        "filter_implementation_version": filter_v,
        "agent_processing_version": f"noise-collector {__version__}",
        "calibration_state": state,
        "calibration_application_method": application,
        "supported_metrics": metrics,
        "low_frequency_lower_hz": band[0],
        "low_frequency_upper_hz": band[1],
        "band_centers_hz": [],
    }
    profile_hash = _content_hash(profile_rec)
    profile_id = _record_id(install_id, "measurement_profile", profile_hash)
    records = [{"kind": "measurement_profile", "id": profile_id, "content_hash": profile_hash, "record": {"id": profile_id, **profile_rec}}]

    calibration_id = None
    calibration_hash = None
    if state != "uncalibrated":
        attachments = []
        if curve_file is not None:
            attachments.append({
                "purpose": "frequency_response",
                "filename": Path(cal.frequency_response_file or "").name,
                "media_type": "text/plain",
                "sha256": hashlib.sha256(file_bytes).hexdigest(),
                "content_base64": base64.b64encode(file_bytes).decode("ascii"),
            })
        cal_rec = {
            "channel": settings.channel.id,
            "calibration_state": state,
            "reference_method": reference_method,
            "reference_device": cal.reference_device,
            "reference_level_db": cal.reference_level_db,
            "reference_frequency_hz": cal.reference_frequency_hz,
            "sensitivity_mv_per_pa": None,
            "sensitivity_dbfs_at_94db": sensitivity,
            "gain_configuration": gain_description,
            "application_method": application,
            "performed_at": cal.performed_at,
            "performed_by": cal.performed_by,
            "notes": cal.notes,
            "attachments": attachments,
        }
        calibration_hash = _content_hash(cal_rec)
        calibration_id = _record_id(install_id, "calibration", calibration_hash)
        records.append({"kind": "calibration", "id": calibration_id, "content_hash": calibration_hash,
                        "record": {"id": calibration_id, **cal_rec}})

    from ..contract.models import ProvenanceCalibration, ProvenanceProfile

    for r in records:  # fail here, with the field named, rather than as a server rejection
        wire_model = ProvenanceProfile if r["kind"] == "measurement_profile" else ProvenanceCalibration
        try:
            wire_model.model_validate(r["record"])
        except ValueError as exc:
            raise ChainError(f"{r['kind']} record is invalid: {exc}"[:800]) from exc

    c = settings.capture
    profile = Profile(
        profile_id=profile_id,
        calibration_id=calibration_id,
        mode=state,
        content_hash=profile_hash,
        calibration_content_hash=calibration_hash,
        capture=CaptureSpec(sample_rate=meas.sample_rate_hz, container=c.container, valid_bits=c.valid_bits,
                            channels=c.channels, analysis_channel=c.analysis_channel),
        gain=GainSpec(inspectable=bool(mic.expected_gain_controls) or is_umik, controls=dict(mic.expected_gain_controls),
                      reference_check=mic.gain_reference_check),
        gain_db=None,
        scale=scale,
        response_correction=correction,
        noise_floor=noise_floor,
        supported_metrics=tuple(metrics),
        lf_band_hz=band,
        microphone_model=model,
        microphone_serial=serial,
        calibration_file_again_db=again,
        calibration_file_sens_factor_db=hdr.sens_factor_db,
    )
    return LocalChain(profile=profile, records=tuple(records), notes=tuple(notes))


def store_records(conn: sqlite3.Connection, chain: LocalChain) -> None:
    """Queue the chain's records for registration with the server (idempotent)."""
    now = iso_utc(time.time())
    with transaction(conn):
        for r in chain.records:
            conn.execute(
                "INSERT OR IGNORE INTO provenance_records(record_id, kind, content_hash, payload_json, created_at) VALUES (?,?,?,?,?)",
                (r["id"], r["kind"], r["content_hash"], json.dumps(r["record"], sort_keys=True), now),
            )
