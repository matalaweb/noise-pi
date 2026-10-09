"""noise-collector command line.

Read-only commands: devices, doctor, status, inspect-backlog, repair-plan, replay (writes only its
own scratch state dir), synth, calibration-check, export-diagnostics.
State-changing commands say exactly what they change: provision, backup, run/acquire/deliver.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from pathlib import Path

from . import __version__


def _settings(args):
    from .config.settings import load_settings

    return load_settings(Path(args.config) if args.config else None)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------- devices / doctor


def cmd_devices(args) -> int:
    from .audio.discovery import DiscoveryError, list_usb_audio, match, parse_stream_formats
    from .audio.gain import read_gain

    devs = list_usb_audio()
    out = {"usb_audio_devices": []}
    for d in devs:
        g = read_gain(d.card_index)
        out["usb_audio_devices"].append({
            **d.identity(),
            "alsa": d.alsa_hw,
            "native_capture_formats": parse_stream_formats(d.stream_info),
            "gain_inspectable": g.inspectable,
            "gain_controls (profile representation)": g.controls,
            "automatic_processing_controls": g.auto_controls,
            "gain_error": g.error,
        })
    try:
        import sounddevice as sd

        out["portaudio_input_devices"] = [
            {"index": i, "name": d["name"], "hostapi": sd.query_hostapis()[d["hostapi"]]["name"], "max_input_channels": d["max_input_channels"],
             "default_samplerate": d["default_samplerate"]}
            for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0
        ]
    except Exception as exc:  # PortAudio missing
        out["portaudio_error"] = str(exc)
    if args.config:
        try:
            s = _settings(args)
            out["selector"] = s.microphone.model_dump()
            out["selected"] = match(devs, s.microphone).identity()
        except (DiscoveryError, Exception) as exc:
            out["selection_error"] = str(exc)
    _print(out)
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run_checks

    results = run_checks(Path(args.config) if args.config else None)
    worst = 0
    for r in results:
        mark = {"PASS": "ok  ", "WARN": "WARN", "FAIL": "FAIL", "INFO": "info"}[r["status"]]
        print(f"[{mark}] {r['check']}: {r['detail']}")
        worst = max(worst, {"PASS": 0, "INFO": 0, "WARN": 1, "FAIL": 2}[r["status"]])
    return 2 if worst == 2 else 0


# ---------------------------------------------------------------------------- provisioning


def cmd_provision(args) -> int:
    """Write local settings, initialise state, fetch and stage the server configuration."""
    import tomllib

    from .config.local_inputs import local_inputs
    from .config.settings import Settings, load_token
    from .contract.configuration import translate
    from .delivery import config_manager
    from .store.db import connect, migrate
    from .transport.api import ApiClient

    with open(args.bootstrap, "rb") as fh:
        boot = tomllib.load(fh)
    expected = boot.pop("expected", {})
    settings = Settings.model_validate(boot)
    token = load_token(settings.paths.credentials_file)
    out_path = Path(args.output)
    print(f"[provision] will write local settings: {out_path}")
    print(f"[provision] will initialise state directory: {settings.state_dir}")
    if not args.yes:
        print("re-run with --yes to apply")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_to_toml(boot))
    os.chmod(out_path, 0o644)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings.db_path)
    conn = connect(settings.db_path)
    api = ApiClient(settings.server, token)
    result, rev = config_manager.poll(api, conn, local_inputs(settings))
    print(f"[provision] configuration poll: {result} (revision {rev})")
    row = conn.execute("SELECT document_json FROM configurations WHERE revision=?", (rev,)).fetchone() if rev else None
    if row is None:
        print("[provision] no valid configuration staged; the collector will run diagnostics only until one is available")
        return 2
    eff = translate(json.loads(row["document_json"]), local_inputs(settings))
    problems = []
    for key, val in (("deployment_id", eff.deployment_id), ("profile_id", eff.profile.profile_id),
                     ("calibration_id", eff.profile.calibration_id)):
        if key in expected and expected[key] != val:
            problems.append(f"{key}: expected {expected[key]!r}, server issued {val!r}")
    if problems:
        print("[provision] MISMATCH with expected identities:\n  " + "\n  ".join(problems))
        return 3
    print(f"[provision] staged revision {rev}: channel={eff.channel} deployment={eff.deployment_id} profile={eff.profile.profile_id} mode={eff.profile.mode}")
    for note in eff.notes:
        print(f"[provision] note: {note}")
    return 0


def _to_toml(d: dict, prefix: str = "") -> str:
    lines, tables = [], []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{k} = {json.dumps(v) if not isinstance(v, bool) else str(v).lower()}")
    for k, v in tables:
        name = f"{prefix}.{k}" if prefix else k
        lines.append(f"\n[{name}]")
        lines.append(_to_toml(v, name).strip())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- run


def cmd_run(args) -> int:
    from .health.logs import setup
    from .supervisor import Supervisor

    s = _settings(args)
    setup("supervisor", s.logging.level, s.state_dir / "logs", s.logging.max_bytes, s.logging.backups)
    return Supervisor(s, Path(args.config) if args.config else None).run()


def cmd_acquire(args) -> int:
    from .acquisition.runner import main
    from .health.logs import setup

    s = _settings(args)
    setup("acquisition", s.logging.level, s.state_dir / "logs", s.logging.max_bytes, s.logging.backups)
    return main(s)


def cmd_deliver(args) -> int:
    from .delivery.service import main
    from .health.logs import setup

    s = _settings(args)
    setup("delivery", s.logging.level, s.state_dir / "logs", s.logging.max_bytes, s.logging.backups)
    return main(s)


def cmd_umik(args) -> int:
    from .umik_report import report

    _print(report(args.cal_file))
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard.server import DashboardError, DashboardServer, new_token
    from .health.logs import setup

    if args.new_token:
        p = Path(args.new_token)
        print(f"[dashboard] writing a new access token to {p} (mode 0600); set dashboard.access_token_file to use it")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(new_token() + "\n")
        return 0
    s = _settings(args)
    setup("dashboard", s.logging.level, s.state_dir / "logs", s.logging.max_bytes, s.logging.backups)
    try:
        srv = DashboardServer(s, host=args.host, port=args.port)
    except (DashboardError, OSError) as exc:
        print(f"dashboard: {exc}", file=sys.stderr)
        return 2
    host, port = srv.address
    print(f"dashboard (read-only, no audio): http://{host}:{port}/")
    import signal
    import threading

    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


def cmd_shutdown(args) -> int:
    import signal

    from .health.status import read_status, run_dir

    s = _settings(args)
    st = read_status(run_dir(s.state_dir) / "supervisor-status.json")
    if not st or st["age_s"] > 30:
        print("no running supervisor found (use systemctl stop noise-collector for the service)")
        return 1
    print(f"sending SIGTERM to supervisor pid {st['pid']} (persists metadata, finalizes recordings as interrupted, releases the microphone)")
    os.kill(int(st["pid"]), signal.SIGTERM)
    return 0


# ---------------------------------------------------------------------------- inspection


def _ro(settings):
    from .store.db import connect

    return connect(settings.db_path, readonly=True)


def cmd_status(args) -> int:
    from .health.status import read_status, run_dir
    from .store.db import counters

    s = _settings(args)
    rd = run_dir(s.state_dir)
    out: dict = {k: read_status(rd / f"{k}-status.json") for k in ("supervisor", "acquisition", "delivery")}
    if s.db_path.exists():
        conn = _ro(s)
        out["local_times"] = {
            "last_captured_second": conn.execute("SELECT MAX(utc_second) FROM measurements").fetchone()[0],
            "last_durably_stored_second": conn.execute("SELECT MAX(utc_second) FROM measurements WHERE status='complete'").fetchone()[0],
            "last_acknowledged_second": conn.execute("SELECT MAX(utc_second) FROM measurements WHERE delivery_state='acknowledged'").fetchone()[0],
            "last_heartbeat_success": (out.get("delivery") or {}).get("last_heartbeat_success"),
        }
        out["counters"] = counters(conn)
        out["configuration"] = [dict(r) for r in conn.execute(
            "SELECT revision, state, applied_at, reason_code FROM configurations ORDER BY revision DESC LIMIT 5")]
    if args.json:
        _print(out)
    else:
        acq = out.get("acquisition") or {}
        dl = out.get("delivery") or {}
        print(f"microphone: {acq.get('microphone_state')}  durable capture: {acq.get('durable_capture')}  error: {acq.get('latest_capture_error')}")
        eng = acq.get("engine") or {}
        print(f"session: {eng.get('session_id')}  detector: {eng.get('detector_state')}  profile: {eng.get('profile_id')} ({eng.get('profile_mode')})")
        print(f"delivery: auth_blocked={dl.get('auth_blocked')} backlog={(dl.get('backlog') or {}).get('queued_measurements')}")
        print(f"times: {out.get('local_times')}")
    return 0


def backlog_report(conn) -> dict:
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a)]  # noqa: E731
    return {
        "measurements": q("SELECT delivery_state, COUNT(*) n, MIN(utc_second) oldest, MAX(utc_second) newest FROM measurements GROUP BY delivery_state"),
        "batches": q("SELECT state, COUNT(*) n, SUM(record_count) records, MAX(attempts) max_attempts FROM outbox_batches GROUP BY state"),
        "quarantined_batches": q("SELECT batch_id, last_status, last_error, record_count FROM outbox_batches WHERE state='quarantined' LIMIT 50"),
        "event_revisions": q("SELECT delivery_state, COUNT(*) n FROM event_revisions GROUP BY delivery_state"),
        "recordings": q("SELECT delivery_state, COUNT(*) n, SUM(size_bytes) bytes FROM recordings WHERE local_deleted_at IS NULL GROUP BY delivery_state"),
        "recording_errors": q("SELECT recording_id, delivery_state, last_error, attempts FROM recordings WHERE last_error IS NOT NULL ORDER BY created_at DESC LIMIT 20"),
        "config_acknowledgments": q("SELECT revision, status, delivery_state, attempts FROM config_acknowledgments ORDER BY revision DESC LIMIT 10"),
        "expired_for_automatic_upload": q("SELECT COUNT(*) n, MIN(utc_second) oldest FROM measurements WHERE delivery_state='expired_for_automatic_upload'"),
    }


def cmd_inspect_backlog(args) -> int:
    _print(backlog_report(_ro(_settings(args))))
    return 0


def cmd_repair_plan(args) -> int:
    """Read-only: list conditions needing an operator decision and what a repair would change."""
    conn = _ro(_settings(args))
    plan = []
    for r in conn.execute("SELECT batch_id, last_status, last_error, record_count FROM outbox_batches WHERE state='quarantined'"):
        plan.append({"item": f"batch {r['batch_id']}", "why": f"{r['last_status']} {r['last_error']}",
                     "option": "diagnose with the server's validation code; records are immutable and stay quarantined until an explicit correction/import exists"})
    n = conn.execute("SELECT COUNT(*) FROM measurements WHERE delivery_state='expired_for_automatic_upload'").fetchone()[0]
    if n:
        plan.append({"item": f"{n} measurements older than the 30-day window", "why": "server rejects normal backfill",
                     "option": "owner enables an import window in Laravel; data is preserved locally until then"})
    for r in conn.execute("SELECT recording_id, last_error FROM recordings WHERE delivery_state IN ('quarantined','failed') AND local_deleted_at IS NULL"):
        plan.append({"item": f"recording {r['recording_id']}", "why": r["last_error"], "option": "inspect file integrity; never re-hash or overwrite a declared file"})
    _print({"read_only": True, "plan": plan})
    return 0


# ---------------------------------------------------------------------------- replay / synth / calibration


def cmd_replay(args) -> int:
    from .acquisition.replay import replay
    from .config.local_inputs import load_calibrations
    from .contract.configuration import CaptureSpec, LocalInputs, translate
    from .contract.examples import example_configuration

    if args.server_config:
        result = json.loads(Path(args.server_config).read_text())
        channel = args.channel or result["configuration"]["channels"][0]["channel"]
        local = LocalInputs(channel=channel, capture=CaptureSpec(),
                            gain_reference_check="file replay: no physical gain",
                            calibrations=load_calibrations(Path(args.calibrations)) if args.calibrations else {})
        cfg = translate(result, local)
    else:
        cfg = example_configuration(mode=args.mode)
    state = Path(args.state_dir or f"./replay-state-{int(time.time())}")
    pattern = [int(x) for x in args.block_pattern.split(",")] if args.block_pattern else None
    res = replay(args.file, state, cfg, start_utc=args.start_utc, block_pattern=pattern, seed=args.seed)
    print(res.to_json())
    return 0


def cmd_synth(args) -> int:
    from .synth import Burst, Scenario, write_wav

    scenarios = {
        "quiet": Scenario(duration_s=args.duration, background_dbfs=-60),
        "engine": Scenario(duration_s=args.duration, background_dbfs=-60, bursts=[Burst(150, 25, "engine_like", -32)]),
        "garage": Scenario(duration_s=args.duration, background_dbfs=-60, bursts=[Burst(160, 12, "garage_door_like", -30)]),
        "mixed": Scenario(duration_s=args.duration, background_dbfs=-60, bursts=[
            Burst(150, 25, "engine_like", -32), Burst(260, 12, "garage_door_like", -30), Burst(330, 1, "impulse", -20)]),
    }
    sc = scenarios[args.scenario]
    write_wav(args.output, sc.render(), sc.fs, args.bits)
    print(f"wrote SYNTHETIC scenario {args.scenario!r} ({args.duration}s, {args.bits}-bit) to {args.output}")
    return 0


def cmd_calibration_check(args) -> int:
    from .calibration_check import run

    _print(run(args))
    return 0


# ---------------------------------------------------------------------------- diagnostics export


def cmd_export_diagnostics(args) -> int:
    from .doctor import run_checks
    from .health.status import read_status, run_dir
    from .store.db import counters
    from .transport.api import redact

    s = _settings(args)
    out = Path(args.output)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", "noise-collector diagnostics. Excludes credentials, signed URLs and audio"
                   + (" (audio INCLUDED at owner request)" if args.include_audio else "") + ".\n")
        settings_dump = s.model_dump(mode="json")
        z.writestr("settings.redacted.json", redact(json.dumps(settings_dump, indent=2)))
        z.writestr("doctor.json", redact(json.dumps(run_checks(Path(args.config) if args.config else None), indent=2, default=str)))
        for k in ("supervisor", "acquisition", "delivery"):
            st = read_status(run_dir(s.state_dir) / f"{k}-status.json")
            if st:
                z.writestr(f"status/{k}.json", redact(json.dumps(st, indent=2, default=str)))
        if s.db_path.exists():
            conn = _ro(s)
            report = backlog_report(conn)
            report["counters"] = counters(conn)
            report["gaps"] = [dict(r) for r in conn.execute("SELECT * FROM gaps ORDER BY id DESC LIMIT 500")]
            report["sessions"] = [dict(r) for r in conn.execute(
                "SELECT session_id, start_sample, end_sample, started_utc, ended_utc, end_reason, format_json, gain_json, timing_json FROM acquisition_sessions ORDER BY rowid DESC LIMIT 100")]
            report["configurations"] = [dict(r) for r in conn.execute("SELECT revision, sha256, state, applied_at, reason_code, detail FROM configurations")]
            report["upload_attempts"] = [dict(r) for r in conn.execute(
                "SELECT attempt_id, recording_id, storage_host, expires_at, state, put_status, detail FROM upload_attempts ORDER BY created_at DESC LIMIT 100")]
            z.writestr("database-summary.json", redact(json.dumps(report, indent=2, default=str)))
            if args.include_audio:
                for r in conn.execute("SELECT recording_id, path FROM recordings WHERE local_deleted_at IS NULL AND path IS NOT NULL ORDER BY created_at DESC LIMIT ?", (args.max_audio,)):
                    from .evidence.fsutil import resolve

                    p = resolve(s.state_dir, r["path"])
                    if p.exists():
                        z.write(p, f"audio/{r['recording_id']}.wav")
        logs = s.state_dir / "logs"
        if logs.exists():
            for p in sorted(logs.glob("*.log*")):
                z.writestr(f"logs/{p.name}", redact(p.read_text(errors="replace")))
    print(f"wrote {out}")
    return 0


def cmd_backup(args) -> int:
    from .store.db import backup, connect

    s = _settings(args)
    dest = Path(args.output)
    print(f"[backup] writing an online SQLite backup (includes committed WAL content) to {dest}")
    backup(connect(s.db_path, readonly=True), dest)
    print("[backup] done; audio files under recordings/ and spool/ must be copied separately")
    return 0


# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="noise-collector", description="Noise monitoring collector")
    p.add_argument("--config", help="local settings TOML (default /etc/noise-collector/collector.toml or $NOISE_COLLECTOR_CONFIG)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list USB audio devices, formats and gain controls").set_defaults(fn=cmd_devices)
    sub.add_parser("doctor", help="setup and health checks").set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("provision", help="write settings, init state, fetch+stage server configuration")
    sp.add_argument("--bootstrap", "--config-file", dest="bootstrap", required=True)
    sp.add_argument("--output", default="/etc/noise-collector/collector.toml")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_provision)
    sub.add_parser("run", help="run acquisition + delivery under supervision").set_defaults(fn=cmd_run)
    sub.add_parser("acquire", help=argparse.SUPPRESS).set_defaults(fn=cmd_acquire)
    sub.add_parser("deliver", help=argparse.SUPPRESS).set_defaults(fn=cmd_deliver)
    sub.add_parser("shutdown", help="controlled shutdown of a running supervisor").set_defaults(fn=cmd_shutdown)
    sp = sub.add_parser("umik", help="miniDSP UMIK-1 setup report: device, mixer, calibration file, values to enter (read-only)")
    sp.add_argument("--cal-file", help="the microphone's calibration file (e.g. 7103946_90deg.txt)")
    sp.set_defaults(fn=cmd_umik)
    sp = sub.add_parser("dashboard", help="local live dashboard (read-only levels/events; never audio)")
    sp.add_argument("--host", help="bind address (default from settings; non-loopback requires an access token)")
    sp.add_argument("--port", type=int)
    sp.add_argument("--new-token", metavar="PATH", help="write a new access token file and exit")
    sp.set_defaults(fn=cmd_dashboard)
    sp = sub.add_parser("status", help="local status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)
    sub.add_parser("inspect-backlog", help="read-only delivery backlog").set_defaults(fn=cmd_inspect_backlog)
    sub.add_parser("repair-plan", help="read-only list of items needing operator decisions").set_defaults(fn=cmd_repair_plan)
    sp = sub.add_parser("replay", help="deterministic file replay through the real engine")
    sp.add_argument("file")
    sp.add_argument("--state-dir")
    sp.add_argument("--server-config", help="a GET /configuration response body (default: built-in example)")
    sp.add_argument("--channel", help="channel name in --server-config (default: first)")
    sp.add_argument("--calibrations", help="calibrations.toml with scales for --server-config")
    sp.add_argument("--mode", default="calibrated", choices=["uncalibrated", "estimated", "calibrated"])
    sp.add_argument("--start-utc", type=float, default=1_790_000_000.0)
    sp.add_argument("--block-pattern")
    sp.add_argument("--seed", type=int)
    sp.set_defaults(fn=cmd_replay)
    sp = sub.add_parser("synth", help="write a SYNTHETIC test scenario WAV")
    sp.add_argument("scenario", choices=["quiet", "engine", "garage", "mixed"])
    sp.add_argument("output")
    sp.add_argument("--duration", type=float, default=400.0)
    sp.add_argument("--bits", type=int, default=24, choices=[16, 24, 32])
    sp.set_defaults(fn=cmd_synth)
    sp = sub.add_parser("calibration-check", help="compare a reference tone against the profile scale (read-only)")
    sp.add_argument("--level", type=float, required=True, help="reference level dB SPL (e.g. 94.0)")
    sp.add_argument("--frequency", type=float, default=1000.0)
    sp.add_argument("--seconds", type=float, default=10.0)
    sp.add_argument("--file", help="analyse a recorded reference WAV instead of live capture")
    sp.add_argument("--sensitivity-dbfs", type=float, help="manufacturer sensitivity: dBFS at 94 dB SPL, for comparison")
    sp.set_defaults(fn=cmd_calibration_check)
    sp = sub.add_parser("export-diagnostics", help="zip redacted diagnostics")
    sp.add_argument("output")
    sp.add_argument("--include-audio", action="store_true")
    sp.add_argument("--max-audio", type=int, default=5)
    sp.set_defaults(fn=cmd_export_diagnostics)
    sp = sub.add_parser("backup", help="online SQLite backup")
    sp.add_argument("output")
    sp.set_defaults(fn=cmd_backup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
