"""Setup and health checks (read-only)."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from pathlib import Path


def _r(check: str, status: str, detail: str) -> dict:
    return {"check": check, "status": status, "detail": detail}


def run_checks(config_path: Path | None) -> list[dict]:
    out: list[dict] = []
    v = sys.version_info
    out.append(_r("python", "PASS" if v >= (3, 11) else "FAIL", f"{platform.python_version()} on {platform.machine()} {platform.system()}"))
    for mod in ("numpy", "scipy", "soundfile", "httpx", "pydantic"):
        try:
            m = __import__(mod)
            out.append(_r(f"import {mod}", "PASS", getattr(m, "__version__", "?")))
        except Exception as exc:
            out.append(_r(f"import {mod}", "FAIL", str(exc)))
    try:
        import sounddevice as sd

        out.append(_r("portaudio", "PASS", sd.get_portaudio_version()[1]))
    except Exception as exc:
        out.append(_r("portaudio", "FAIL", f"{exc} (install libportaudio2)"))
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        out.append(_r("service account", "WARN", "running as root; the service must run as the dedicated noise-collector account"))
    try:
        from .dsp.filters import load_filter

        for fs in (48000,):
            for k in ("A", "C", "LF"):
                load_filter(k, fs)
        out.append(_r("filter coefficients", "PASS", "committed hashes verified"))
    except Exception as exc:
        out.append(_r("filter coefficients", "FAIL", str(exc)))

    try:
        from .config.settings import load_settings

        s = load_settings(config_path)
        out.append(_r("settings", "PASS", f"state_dir={s.state_dir} api={s.server.base_url}"))
    except Exception as exc:
        out.append(_r("settings", "FAIL", str(exc)[:300]))
        return out
    out.append(_r("api origin", "PASS" if s.server.base_url.startswith("https://") else "FAIL", s.server.base_url))
    if not s.server.trusted_storage_hosts:
        out.append(_r("storage hosts", "WARN", "trusted_storage_hosts is empty: audio uploads will be refused"))
    try:
        from .config.settings import load_token

        load_token(s.paths.credentials_file)
        out.append(_r("credentials", "PASS", f"{s.paths.credentials_file} readable, permissions restricted"))
    except Exception as exc:
        out.append(_r("credentials", "FAIL", str(exc)))

    sd_ = s.state_dir
    if sd_.exists() and os.access(sd_, os.W_OK):
        out.append(_r("state dir", "PASS", f"{sd_} writable"))
    else:
        out.append(_r("state dir", "FAIL", f"{sd_} missing or not writable"))
    if sd_.exists():
        from .storage import GIB, measure

        st = measure(sd_, s.storage)
        lvl = {"ok": "PASS", "warning": "WARN", "audio_stopped": "WARN", "critical": "FAIL"}[st.state]
        out.append(_r("storage", lvl, f"state={st.state} free={st.volume_free / GIB:.1f}GiB total={st.volume_total / GIB:.1f}GiB "
                                       f"reserve={st.reserve / GIB:.1f}GiB audio_quota={st.audio_quota / GIB:.1f}GiB used={st.audio_used / GIB:.2f}GiB "
                                       f"metadata_quota={st.measurement_quota / GIB:.1f}GiB db={st.db_bytes / 1e6:.0f}MB"))
        if st.volume_total < 16 * GIB:
            out.append(_r("volume size", "WARN", "volume < 16 GiB: 30 days of pending measurements plus 72 h of events may not fit"))
        from .store.lock import InstanceLock, LockHeld

        for role in ("acquisition", "delivery"):
            try:
                with InstanceLock(sd_, role):
                    out.append(_r(f"{role} lock", "INFO", "free (service not running)"))
            except LockHeld as exc:
                out.append(_r(f"{role} lock", "INFO", f"held: {exc}"))
        if s.db_path.exists():
            from .store.db import LATEST_SCHEMA, connect, schema_version

            conn = connect(s.db_path, readonly=True)
            ver = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            out.append(_r("database schema", "PASS" if ver == LATEST_SCHEMA else "WARN", f"version {ver}, agent expects {LATEST_SCHEMA}"))
            row = conn.execute("SELECT revision, document_json FROM configurations WHERE state='applied' ORDER BY revision DESC LIMIT 1").fetchone()
            staged = conn.execute("SELECT revision FROM configurations WHERE state='staged' ORDER BY revision DESC LIMIT 1").fetchone()
            from .config.chain import ChainError, build_chain
            from .config.local_inputs import local_inputs
            from .contract.configuration import ConfigRejected, parse_document
            from .store.db import get_meta

            try:
                chain = build_chain(s, get_meta(conn, "installation_id") or "unregistered")
                p = chain.profile
                note = {"uncalibrated": "dBFS only; SPL fields are null",
                        "estimated": "SPL values are labelled as estimates", "calibrated": "calibrated chain; confirm with calibration-check"}[p.mode]
                status = "PASS"
                if p.mode != "uncalibrated" and p.scale is None:
                    status, note = "WARN", "no absolute scale: SPL fields are null"
                out.append(_r("measurement chain", status, f"{p.microphone_model} serial {p.microphone_serial or '-'}, {p.mode}: {note}"))
                for n in chain.notes:
                    out.append(_r("measurement chain note", "INFO", n))
                try:
                    reg = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM provenance_records GROUP BY state")}
                except sqlite3.OperationalError:  # schema not migrated yet (service not restarted after an upgrade)
                    reg = {"not yet migrated": 1}
                if reg:
                    out.append(_r("chain registration", "WARN" if reg.get("rejected") else "INFO",
                                  ", ".join(f"{k} {v}" for k, v in sorted(reg.items()))))
            except ChainError as exc:
                out.append(_r("measurement chain", "FAIL", f"{exc} (fix [microphone]/[calibration] in collector.toml)"))
            if row:
                try:
                    op = parse_document(json.loads(row["document_json"]), local_inputs(s), verify_hash=False)
                    out.append(_r("configuration", "PASS", f"applied revision {row['revision']}, channel {op.channel}"))
                    for n in op.notes:
                        out.append(_r("configuration note", "INFO", n))
                except ConfigRejected as exc:
                    out.append(_r("configuration", "FAIL", f"applied revision {row['revision']} unusable with local settings: {exc}"))
            elif staged:
                out.append(_r("configuration", "INFO", f"revision {staged['revision']} staged, not yet applied"))
            else:
                out.append(_r("configuration", "INFO", "none published: running on local defaults (measurements only)"))
            del schema_version

    from .timing.clock import SystemClock

    cs = SystemClock().status()
    if cs.synchronized:
        out.append(_r("clock", "PASS", f"synchronized (est_error={cs.est_error_ms} ms, max_error={cs.max_error_ms} ms)"))
    elif cs.synchronized is None:
        out.append(_r("clock", "WARN", f"synchronization state unknown ({cs.source}); trusted UTC requires a synchronized clock"))
    else:
        out.append(_r("clock", "FAIL", "not synchronized: measurements stay local (untrusted UTC) until NTP sync"))

    try:
        from .audio.discovery import list_usb_audio, match, parse_stream_formats, portaudio_index
        from .audio.gain import read_gain

        from .audio import umik1

        devs = list_usb_audio()
        dev = match(devs, s.microphone)
        out.append(_r("microphone", "PASS", f"{dev.product} serial={dev.serial} path={dev.usb_path} {dev.alsa_hw}"))
        formats = parse_stream_formats(dev.stream_info)
        out.append(_r("native formats", "INFO", json.dumps(formats)))
        if formats and not any(f.get("channels") == s.capture.channels and 48000 in f.get("rates", []) for f in formats):
            out.append(_r("capture format", "FAIL", f"[capture] channels = {s.capture.channels} is not native; device offers {formats}"))
        g = read_gain(dev.card_index)
        out.append(_r("gain readback", "PASS" if g.inspectable else "WARN", json.dumps(g.controls) if g.inspectable else str(g.error)))
        if umik1.is_umik1(dev.vendor_id, dev.product_id, dev.product):
            ok, note = umik1.mixer_check(g)
            out.append(_r("UMIK-1 mixer", "PASS" if ok else "FAIL", note or "Mic capture at 0.00 dB, on"))
            again = umik1.analog_gain_db(dev.product)
            out.append(_r("UMIK-1 analog gain", "INFO",
                          f"{again} dB (from '{dev.product}'); the calibration file's AGain must match" if again is not None else
                          f"not reported by this unit ('{dev.product}'); use the calibration files miniDSP issued for this serial"))
            if umik1.real_serial(dev.serial) is None:
                out.append(_r("UMIK-1 serial", "INFO", "USB serial is a placeholder; the real serial comes from the calibration file "
                                                       "and the web-app profile" + ("" if s.microphone.usb_path else
                                                       "; set [microphone] usb_path to pin the physical port")))
            if s.microphone.model != "umik-1":
                out.append(_r("UMIK-1 preset", "WARN", "set [microphone] model = \"umik-1\" to enable the UMIK-1 gain checks"))
        if g.auto_controls:
            out.append(_r("automatic processing", "WARN", f"controls present, must be off: {g.auto_controls}"))
        try:
            idx = portaudio_index(dev)
            out.append(_r("portaudio device", "PASS", f"index {idx}"))
        except Exception as exc:
            out.append(_r("portaudio device", "FAIL", str(exc)))
        if not os.access("/dev/snd", os.R_OK | os.X_OK):
            out.append(_r("audio permissions", "FAIL", "/dev/snd not accessible: add the service account to the 'audio' group"))
    except Exception as exc:
        out.append(_r("microphone", "FAIL" if sys.platform.startswith("linux") else "INFO", str(exc)[:300]))

    import shutil
    import subprocess

    if shutil.which("pgrep"):
        busy = [p for p in ("pipewire", "wireplumber", "pulseaudio")
                if subprocess.run(["pgrep", "-x", p], capture_output=True).returncode == 0]
        if busy:
            out.append(_r("audio servers", "WARN", f"{', '.join(busy)} running: they can hold the USB microphone (EBUSY on hw:). "
                                                   "Use Raspberry Pi OS Lite or disable the device in WirePlumber (docs/umik1.md)"))
    from .health.status import host_health

    h = host_health()
    t = h.get("cpu_temp_c")
    out.append(_r("temperature", "INFO" if t is None else ("WARN" if t > 75 else "PASS"), f"{t} C"))
    if h.get("throttled_flags") not in (None, "0", "0x0"):
        out.append(_r("throttling/undervoltage", "WARN", f"flags={h['throttled_flags']} (check power supply and cooling)"))
    return out
