"""The delivery/control process: two lanes over the durable local state.

Control lane: measurement batches, event revisions, configuration polling/acknowledgments,
heartbeat, retention. Audio lane: one recording at a time through declaration, staged upload,
completion and verification. Each lane has its own SQLite connection and short transactions.
Network failures never touch acquisition; they only move ``next_attempt_at``.

Server outcomes follow the contract's ``error.retry`` policy (contract/upstream/device-api-v1.yaml):
``backoff`` -> retry the same identity with full-jitter backoff; ``after_clock_sync`` -> hold until
the clock is trusted; ``after_configuration_refresh`` -> refetch configuration, then retry;
``after_correction`` -> stop authenticated traffic; ``never`` -> quarantine locally (never drop).
"""

from __future__ import annotations

import json
import logging
import random
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .. import __version__
from ..config.local_inputs import local_inputs
from ..config.settings import Settings, load_token
from ..contract.configuration import COMPUTED_METRICS, DeliverySettings, Operational, local_defaults, parse_document
from ..contract.models import (
    BatchResult,
    Capabilities,
    Clock,
    ConfigAckResult,
    EventResult,
    Heartbeat,
    HeartbeatResult,
    RecordingCompletion,
    RecordingDeclaration,
    RecordingStatus,
    RecordingUploadResult,
    UploadAttempt,
)
from ..evidence.fsutil import resolve
from ..health.status import read_status, run_dir, write_status
from ..storage import measure
from ..store.db import bump_counter, connect, counters, get_meta, migrate, set_meta, transaction
from ..store.lock import InstanceLock
from ..timeutil import iso_utc, parse_iso
from ..transport.api import (
    AFTER_CLOCK_SYNC,
    AFTER_CONFIG_REFRESH,
    AUTH,
    CONFLICT,
    INVALID,
    NOT_FOUND,
    OK,
    OUTSIDE_WINDOW,
    TOO_LARGE,
    ApiClient,
    Outcome,
    StorageUploader,
    file_sha256,
    token_fingerprint,
)
from ..transport.backoff import RateLimiter, next_delay
from . import config_manager, outbox, provenance, retention

log = logging.getLogger(__name__)

P_BATCHES = "/api/v1/device/measurements/batches"
P_EVENTS = "/api/v1/device/events"
P_ACKS = "/api/v1/device/configuration/acknowledgments"
P_HEARTBEAT = "/api/v1/device/heartbeat"
MAX_CHECKSUM_REUPLOADS = 3
STATUS_STALE_S = 15.0
CLOCK_HOLD_S = 600.0
REFRESH_HOLD_S = 300.0
UNKNOWN_PROVENANCE_HOLD_S = 30.0
REUPLOADABLE_FAILURES = {"sha256_mismatch", "size_mismatch", "object_missing", "unreadable_media"}


@dataclass
class _Target:
    attempt_id: str
    url: str
    headers: dict[str, str]
    expires_at: float


@dataclass
class AuthGate:
    blocked: bool = False
    reason: str | None = None
    fingerprint: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def block(self, reason: str) -> None:
        with self.lock:
            if not self.blocked:
                log.error("authentication rejected (%s); stopping authenticated retries until credentials change", reason)
            self.blocked, self.reason = True, reason


class DeliveryService:
    def __init__(self, settings: Settings, token: str, *, transport: httpx.BaseTransport | None = None,
                 storage_transport: httpx.BaseTransport | None = None, clock=time.time, rng: random.Random | None = None) -> None:
        self.s = settings
        self.token = token
        self._transport = transport
        self.api = ApiClient(settings.server, token, transport=transport)
        self.uploader = StorageUploader(settings.server, transport=storage_transport)
        self.clock = clock
        self.rng = rng or random.Random()
        self.auth = AuthGate(fingerprint=token_fingerprint(token))
        # The limiter paces real elapsed time; an injected test clock drives it too.
        self.limiter = RateLimiter(settings.server.max_batches_per_minute, clock=time.monotonic if clock is time.time else clock)
        self.targets: dict[str, _Target] = {}
        self.stop_event = threading.Event()
        self.next_config = 0.0
        self.next_heartbeat = 0.0
        self.next_retention = 0.0
        self.next_expire = 0.0
        self.desired_revision: int | None = None
        self.last_heartbeat_success: float | None = None
        self.last_batch_ack: float | None = None
        self.lane_errors: dict[str, str] = {}
        self.status_path = run_dir(settings.state_dir) / "delivery-status.json"
        self.started = time.monotonic()
        self._eff_cache: tuple[int | None, Operational] | None = None
        self._dropped_at_last_hb: int | None = None

    # ------------------------------------------------------------------ helpers

    def operational(self, conn: sqlite3.Connection) -> Operational:
        """Operational settings in force: the applied revision, or the local defaults before one exists."""
        row = conn.execute(
            "SELECT revision, document_json FROM configurations WHERE state='applied' ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        rev = row["revision"] if row is not None else None
        if self._eff_cache and self._eff_cache[0] == rev:
            return self._eff_cache[1]
        op = local_defaults(local_inputs(self.s))
        if row is not None:
            try:
                op = parse_document(json.loads(row["document_json"]), local_inputs(self.s), verify_hash=False)
            except Exception as exc:  # the acquisition process applied it, so this should not happen
                log.error("applied configuration %s cannot be parsed: %s", row["revision"], exc)
        self._eff_cache = (rev, op)
        return op

    def _delivery_settings(self, conn: sqlite3.Connection) -> DeliverySettings:
        return self.operational(conn).delivery

    def _delay(self, attempts: int, out: Outcome) -> float:
        if out.retry_after is not None and out.status == 429:
            self.limiter.penalize(out.retry_after)
        return next_delay(attempts, out.retry_after, self.rng)

    def _auth(self, conn: sqlite3.Connection, out: Outcome) -> None:
        self.auth.block(out.error_code or str(out.status))
        set_meta_tx(conn, "auth_blocked_fingerprint", self.auth.fingerprint or "")
        bump_counter(conn, "auth_rejections", 1, out.error_code)

    def _register_provenance(self, conn: sqlite3.Connection, now: float) -> bool:
        """Register pending measurement-chain records; True when nothing referenced is unregistered."""
        result, out = provenance.register(self.api, conn, now, self._delay)
        if result != "idle":
            self.lane_errors["provenance_last"] = result
        if result == "auth" and out is not None:
            self._auth(conn, out)
        return provenance.pending_count(conn) == 0

    def _hold(self, conn: sqlite3.Connection, out: Outcome) -> float:
        """Delay for the server's non-backoff retry hints; schedules a config refresh when asked."""
        if out.kind == AFTER_CONFIG_REFRESH:
            self.next_config = 0.0
            bump_counter(conn, "unknown_provenance_rejections", 1, out.error_code)
            if out.error_code == "unknown_provenance":
                # Register the chain again (idempotent), then resubmit shortly.
                provenance.reregister_all(conn)
                return UNKNOWN_PROVENANCE_HOLD_S
            return REFRESH_HOLD_S
        bump_counter(conn, "clock_rejections", 1, out.error_code)
        return CLOCK_HOLD_S

    def _check_auth_recovered(self, conn: sqlite3.Connection) -> None:
        if not self.auth.blocked:
            blocked_fp = get_meta(conn, "auth_blocked_fingerprint")
            if blocked_fp and blocked_fp == self.auth.fingerprint:
                self.auth.block("blocked_before_restart")
            return
        try:
            token = load_token(self.s.paths.credentials_file)
        except Exception:
            return
        fp = token_fingerprint(token)
        if fp != self.auth.fingerprint:
            log.warning("credentials changed; resuming authenticated delivery")
            self.api.close()
            self.token = token
            self.api = ApiClient(self.s.server, token, transport=self._transport)
            self.auth = AuthGate(fingerprint=fp)
            set_meta_tx(conn, "auth_blocked_fingerprint", "")

    # ------------------------------------------------------------------ control lane

    def control_step(self, conn: sqlite3.Connection) -> None:
        now = self.clock()
        if now >= self.next_expire:
            outbox.expire_old(conn, now, self.s.storage.backfill_window_days)
            _expire_events(conn, now, self.s.storage.backfill_window_days)
            self.next_expire = now + 600
        ds = self._delivery_settings(conn)
        outbox.build_batches(conn, now, batch_seconds=ds.measurement_batch_seconds)
        if now >= self.next_retention:
            self._retention(conn, now)
            self.next_retention = now + 600
        self._check_auth_recovered(conn)
        if self.auth.blocked:
            return
        self._send_acks(conn, now)
        if self._register_provenance(conn, now):
            self._send_events(conn, now)
            self._send_batch(conn, now)
        if now >= self.next_config:
            self._poll_config(conn)
            self.next_config = now + ds.config_poll_seconds * self.rng.uniform(0.8, 1.2)
        if now >= self.next_heartbeat:
            self._heartbeat(conn, now)
            self.next_heartbeat = now + ds.heartbeat_seconds * self.rng.uniform(0.9, 1.1)

    def _retention(self, conn: sqlite3.Connection, now: float) -> None:
        op = self.operational(conn)
        try:
            st = measure(self.s.state_dir, self.s.storage)
            retention.run(
                conn, self.s.state_dir, self.s.storage, st.state, now,
                ack_days=op.retention.acknowledged_measurement_days,
                audio_hours=op.retention.verified_audio_days * 24,
            )
        except OSError as exc:
            self.lane_errors["retention"] = str(exc)

    def _send_batch(self, conn: sqlite3.Connection, now: float) -> None:
        if not self.limiter.try_acquire():
            return
        prefer_current = self.rng.random() < 0.5
        row = outbox.next_batch(conn, now, prefer_current)
        if row is None or not outbox.lease(conn, row["batch_id"], now):
            self.limiter.tokens += 1  # unused slot
            return
        out = self.api.request("POST", P_BATCHES, content=row["payload"].encode(), model=BatchResult)
        bid = row["batch_id"]
        if out.kind == OK:
            err = outbox.validate_ack(row, out.model)  # type: ignore[arg-type]
            if err is None:
                outbox.mark_acknowledged(conn, bid, out.body, self.clock())
                self.last_batch_ack = self.clock()
                return
            out = Outcome("malformed", out.status, error_code=err)
        if out.kind == AUTH:
            outbox.mark_retry(conn, bid, 0, now, out.status, out.error_code, count_attempt=False)
            self._auth(conn, out)
        elif out.kind == TOO_LARGE:
            outbox.split_oversized(conn, bid, now)
        elif out.kind in (CONFLICT, INVALID):
            outbox.quarantine(conn, bid, out.status, out.error_code)
        elif out.kind == OUTSIDE_WINDOW:
            outbox.expire_batch(conn, bid, out.error_code)
        elif out.kind in (AFTER_CLOCK_SYNC, AFTER_CONFIG_REFRESH):
            outbox.mark_retry(conn, bid, self._hold(conn, out), now, out.status, out.error_code)
        else:
            outbox.mark_retry(conn, bid, self._delay(row["attempts"], out), now, out.status, out.error_code)

    def _send_events(self, conn: sqlite3.Connection, now: float) -> None:
        row = conn.execute(
            """SELECT r.* FROM event_revisions r WHERE r.delivery_state='pending' AND r.next_attempt_at <= ?
               AND NOT EXISTS (SELECT 1 FROM event_revisions p WHERE p.event_id=r.event_id AND p.revision < r.revision
                               AND p.delivery_state != 'acknowledged')
               ORDER BY r.created_at, r.revision LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return
        with transaction(conn):
            conn.execute("UPDATE event_revisions SET attempts=attempts+1 WHERE event_id=? AND revision=?", (row["event_id"], row["revision"]))
        out = self.api.request("POST", P_EVENTS, content=row["payload_json"].encode(), model=EventResult)
        key = (row["event_id"], row["revision"])
        if out.kind == OK and out.model.event_id == row["event_id"] and out.model.revision == row["revision"]:  # type: ignore[union-attr]
            _set_rev(conn, key, "acknowledged", out.status, None, 0, ack=True)
        elif out.kind == OK:
            _set_rev(conn, key, "pending", out.status, "ack_mismatch", now + self._delay(row["attempts"], out))
        elif out.kind == AUTH:
            self._auth(conn, out)
        elif out.kind in (CONFLICT, INVALID, TOO_LARGE):
            _set_rev(conn, key, "quarantined", out.status, out.error_code, 0)
            bump_counter(conn, "event_revisions_quarantined", 1, out.error_code)
        elif out.kind == OUTSIDE_WINDOW:
            _set_rev(conn, key, "expired_for_automatic_upload", out.status, out.error_code, 0)
        elif out.kind in (AFTER_CLOCK_SYNC, AFTER_CONFIG_REFRESH):
            _set_rev(conn, key, "pending", out.status, out.error_code, now + self._hold(conn, out))
        else:
            _set_rev(conn, key, "pending", out.status, out.error_code, now + self._delay(row["attempts"], out))

    def _send_acks(self, conn: sqlite3.Connection, now: float) -> None:
        for row in conn.execute(
            "SELECT * FROM config_acknowledgments WHERE delivery_state='pending' AND next_attempt_at <= ? ORDER BY revision LIMIT 5", (now,)
        ).fetchall():
            out = self.api.request("POST", P_ACKS, content=row["payload_json"].encode(), model=ConfigAckResult)
            if out.kind == OK:
                state, nxt = "acknowledged", 0.0
            elif out.kind == AUTH:
                self._auth(conn, out)
                return
            elif out.kind in (CONFLICT, INVALID, NOT_FOUND):
                state, nxt = "quarantined", 0.0
                bump_counter(conn, "config_acks_quarantined", 1, out.error_code)
            else:
                state, nxt = "pending", now + self._delay(row["attempts"], out)
            with transaction(conn):
                conn.execute(
                    "UPDATE config_acknowledgments SET delivery_state=?, attempts=attempts+1, next_attempt_at=? WHERE ack_id=?",
                    (state, nxt, row["ack_id"]),
                )
            if state == "pending":
                return

    def _poll_config(self, conn: sqlite3.Connection) -> None:
        try:
            result, rev = config_manager.poll(self.api, conn, local_inputs(self.s))
        except Exception as exc:  # never let a bad document kill the lane
            log.exception("configuration poll failed")
            self.lane_errors["config"] = str(exc)[:200]
            return
        if rev is not None:
            self.desired_revision = rev
        if result.startswith("fetch_auth"):
            self._auth(conn, Outcome(AUTH, None, error_code="config_auth"))
        self.lane_errors["config_last"] = result

    # ------------------------------------------------------------------ heartbeat

    @staticmethod
    def _boot_id(acq: dict | None, stale: bool) -> str | None:
        """The current acquisition session, or null when there is none (e.g. microphone not connected)."""
        return None if stale else ((acq or {}).get("engine") or {}).get("session_id")

    def build_heartbeat(self, conn: sqlite3.Connection, now: float) -> Heartbeat:
        acq = read_status(run_dir(self.s.state_dir) / "acquisition-status.json")
        stale = acq is None or acq.get("age_s", 1e9) > STATUS_STALE_S
        local = "error" if stale else (acq or {}).get("microphone_state", "error")
        mic = {"ok": "ok", "disconnected": "disconnected", "not_configured": "unknown", "stopped": "unknown"}.get(local, "error")
        if not stale and (acq or {}).get("durable_capture") == "critical":
            mic = "error"
        clock = (acq or {}).get("clock") or {}
        synced = clock.get("synchronized")
        b = outbox.backlog(conn)
        pend = conn.execute(
            """SELECT COALESCE(SUM(size_bytes),0) s, COUNT(*) n FROM recordings WHERE local_deleted_at IS NULL AND state='finalized'
               AND delivery_state NOT IN ('verified','local_only','expired_for_automatic_upload','server_purged')"""
        ).fetchone()
        applied = conn.execute("SELECT MAX(revision) r FROM configurations WHERE state='applied'").fetchone()["r"]
        desired = max(filter(None, [self.desired_revision, applied]), default=None)
        st = measure(self.s.state_dir, self.s.storage)
        dropped = int(counters(conn).get("dropped_intervals", 0))
        recent = dropped - self._dropped_at_last_hb if self._dropped_at_last_hb is not None else dropped
        rates = [r for f in ((acq or {}).get("device") or {}).get("native_formats", []) for r in f.get("rates", [])]
        error = (acq or {}).get("latest_capture_error") if not stale else "acquisition status stale"
        return Heartbeat(
            sent_at=iso_utc(now),
            agent_version=f"noise-collector {__version__}",
            boot_id=self._boot_id(acq, stale),
            uptime_seconds=int((acq or {}).get("uptime_s") or (time.monotonic() - self.started)),
            capabilities=Capabilities(
                channels=[self.s.channel.id],
                metrics=list(COMPUTED_METRICS),  # lcpeak is not validated, so not offered
                third_octave_bands=False,
                recording_formats=["audio/wav", "audio/flac"] if self.s.recording.locally_enabled else [],
                max_sample_rate_hz=max(rates) if rates else 48000,
            ),
            microphone_state=mic,  # type: ignore[arg-type]
            free_disk_bytes=int(st.volume_free),
            total_disk_bytes=int(st.volume_total),
            queued_measurement_count=int(b["queued_measurements"]),
            pending_audio_bytes=int(pend["s"]),
            pending_audio_count=int(pend["n"]),
            oldest_pending_capture_at=iso_utc(b["oldest_pending_second"]) if b["oldest_pending_second"] else None,
            desired_config_revision=desired,
            applied_config_revision=applied,
            clock=Clock(sync_state="synchronized" if synced else ("unsynchronized" if synced is False else "unknown"),
                        offset_ms=int(round(clock["offset_ms"])) if clock.get("offset_ms") is not None else None,
                        source=clock.get("source")),
            recent_dropped_intervals=max(0, recent),
            last_capture_error=(error or None) and str(error)[:2000],
        )

    def _heartbeat(self, conn: sqlite3.Connection, now: float) -> None:
        hb = self.build_heartbeat(conn, now)
        out = self.api.request("POST", P_HEARTBEAT, content=hb.model_dump_json().encode(), model=HeartbeatResult)
        if out.kind == OK:
            self.last_heartbeat_success = now
            self._dropped_at_last_hb = int(counters(conn).get("dropped_intervals", 0))
            res: HeartbeatResult = out.model  # type: ignore[assignment]
            if res.desired_config_revision:
                self.desired_revision = res.desired_config_revision
            if res.configuration_pending:
                self.next_config = now
        elif out.kind == AUTH:
            self._auth(conn, out)

    # ------------------------------------------------------------------ audio lane

    def audio_step(self, conn: sqlite3.Connection) -> bool:
        """Advance one recording. Returns True if any work was attempted."""
        if self.auth.blocked:
            return False
        now = self.clock()
        row = conn.execute(
            """SELECT * FROM recordings WHERE state='finalized' AND next_attempt_at <= ?
               AND delivery_state IN ('uploading','awaiting_upload','awaiting_verification','pending_declaration')
               ORDER BY CASE delivery_state WHEN 'uploading' THEN 0 WHEN 'awaiting_upload' THEN 1
                        WHEN 'awaiting_verification' THEN 2 ELSE 3 END, created_at LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return False
        handler = getattr(self, f"_rec_{row['delivery_state']}")
        handler(conn, row, now)
        return True

    def _rec_set(self, conn: sqlite3.Connection, rid: str, state: str, *, delay: float = 0.0, error: str | None = None,
                 extra: dict | None = None, attempt_inc: bool = False) -> None:
        sets = ["delivery_state=?", "next_attempt_at=?", "last_error=?"]
        vals: list = [state, self.clock() + delay, error]
        for k, v in (extra or {}).items():
            sets.append(f"{k}=?")
            vals.append(v)
        if attempt_inc:
            sets.append("attempts=attempts+1")
        with transaction(conn):
            conn.execute(f"UPDATE recordings SET {', '.join(sets)} WHERE recording_id=?", (*vals, rid))

    def _event_dependency(self, conn: sqlite3.Connection, event_id: str) -> str:
        r = conn.execute("SELECT delivery_state FROM event_revisions WHERE event_id=? AND revision=1", (event_id,)).fetchone()
        return r["delivery_state"] if r else "missing"

    def _common_failure(self, conn: sqlite3.Connection, row: sqlite3.Row, out: Outcome, state: str) -> None:
        rid = row["recording_id"]
        if out.kind == AUTH:
            self._auth(conn, out)
        elif out.kind in (AFTER_CLOCK_SYNC, AFTER_CONFIG_REFRESH):
            self._rec_set(conn, rid, state, delay=self._hold(conn, out), error=out.error_code)
        else:
            self._rec_set(conn, rid, state, delay=self._delay(row["attempts"], out), error=out.error_code, attempt_inc=True)

    def _apply_upload_result(self, conn: sqlite3.Connection, rid: str, res: RecordingUploadResult) -> None:
        if res.status == "verified":
            self._rec_set(conn, rid, "awaiting_verification")
        elif res.status == "purged":
            self._rec_set(conn, rid, "server_purged", error="purged_by_server_retention")
        elif res.upload is not None:
            self._remember_target(conn, rid, res.upload)
            self._rec_set(conn, rid, "awaiting_upload", extra={"server_json": json.dumps({"declared": res.status})})
        elif res.status in ("uploaded", "verifying"):
            self._rec_set(conn, rid, "awaiting_verification", delay=2.0)
        else:
            self._rec_set(conn, rid, "awaiting_upload")

    def _rec_pending_declaration(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
        rid = row["recording_id"]
        dep = self._event_dependency(conn, row["event_id"])
        if dep in ("quarantined", "expired_for_automatic_upload", "local_only", "missing"):
            self._rec_set(conn, rid, "quarantined" if dep != "expired_for_automatic_upload" else "expired_for_automatic_upload",
                          error=f"event_dependency_{dep}")
            return
        if dep != "acknowledged":
            self._rec_set(conn, rid, "pending_declaration", delay=15.0, error="awaiting_event_acknowledgment")
            return
        path = resolve(self.s.state_dir, row["path"])
        if not path.exists() or path.stat().st_size != row["size_bytes"]:
            self._rec_set(conn, rid, "quarantined", error="local_file_missing_or_resized")
            bump_counter(conn, "recordings_local_corruption", 1, rid)
            return
        fmt = json.loads(row["format_json"])
        decl = RecordingDeclaration(
            recording_id=rid,
            segment_number=row["segment_number"],
            capture_started_at=row["capture_started_at"],
            duration_ms=row["duration_ms"],
            mime_type=fmt.get("mime_type", "audio/wav"),
            codec=fmt["codec"],
            sample_rate_hz=fmt["sample_rate"],
            channel_count=1,
            bit_depth=fmt["bit_depth"],
            byte_size=row["size_bytes"],
            sha256=row["sha256"],
        )
        out = self.api.request("POST", f"/api/v1/device/events/{row['event_id']}/recordings",
                               content=decl.model_dump_json().encode(), model=RecordingUploadResult)
        if out.kind == OK:
            res: RecordingUploadResult = out.model  # type: ignore[assignment]
            if res.recording_id.lower() != rid:
                self._rec_set(conn, rid, "pending_declaration", delay=self._delay(row["attempts"], out), error="declaration_id_mismatch",
                              attempt_inc=True)
                return
            self._apply_upload_result(conn, rid, res)
        elif out.kind == NOT_FOUND:
            # The server does not know the event: re-send its accepted identity (same payload), then retry.
            with transaction(conn):
                conn.execute("UPDATE event_revisions SET delivery_state='pending', next_attempt_at=0 WHERE event_id=? AND revision=1", (row["event_id"],))
            self._rec_set(conn, rid, "pending_declaration", delay=30.0, error="event_not_found_on_server", attempt_inc=True)
        elif out.kind in (CONFLICT, INVALID, TOO_LARGE):
            self._rec_set(conn, rid, "quarantined", error=out.error_code)
            bump_counter(conn, "recordings_quarantined", 1, out.error_code)
        else:
            self._common_failure(conn, row, out, "pending_declaration")

    def _remember_target(self, conn: sqlite3.Connection, rid: str, t: UploadAttempt) -> None:
        try:
            exp = parse_iso(t.expires_at).timestamp()
        except ValueError:
            exp = self.clock() + 300
        self.targets[rid] = _Target(t.attempt_id, t.url, dict(t.headers), exp)
        with transaction(conn):
            conn.execute(
                """INSERT OR IGNORE INTO upload_attempts(attempt_id, recording_id, storage_host, expires_at, state, created_at, updated_at)
                   VALUES (?,?,?,?, 'issued', ?, ?)""",
                (t.attempt_id, rid, urlparse(t.url).hostname or "", t.expires_at, iso_utc(self.clock()), iso_utc(self.clock())),
            )

    def _attempt_state(self, conn: sqlite3.Connection, attempt_id: str, state: str, put_status: int | None = None, detail: str | None = None) -> None:
        with transaction(conn):
            conn.execute(
                "UPDATE upload_attempts SET state=?, put_status=COALESCE(?, put_status), detail=COALESCE(?, detail), updated_at=? WHERE attempt_id=?",
                (state, put_status, detail, iso_utc(self.clock()), attempt_id),
            )

    def _new_attempt(self, conn: sqlite3.Connection, row: sqlite3.Row) -> _Target | None:
        rid = row["recording_id"]
        out = self.api.request("POST", f"/api/v1/device/recordings/{rid}/upload-attempts", model=RecordingUploadResult)
        if out.kind == OK:
            res: RecordingUploadResult = out.model  # type: ignore[assignment]
            if res.upload is not None:
                self._remember_target(conn, rid, res.upload)
                return self.targets[rid]
            self._apply_upload_result(conn, rid, res)
        elif out.kind == NOT_FOUND:
            self._rec_set(conn, rid, "pending_declaration", error="recording_not_found_on_server", attempt_inc=True)
        elif out.kind == CONFLICT:
            if out.error_code == "recording_already_verified":
                self._rec_set(conn, rid, "awaiting_verification", error=out.error_code)
            else:
                self._rec_set(conn, rid, "server_purged", error=out.error_code)
        elif out.kind == INVALID:
            self._rec_set(conn, rid, "quarantined", error=out.error_code)
        else:
            self._common_failure(conn, row, out, "awaiting_upload")
        return None

    def _rec_awaiting_upload(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
        rid = row["recording_id"]
        t = self.targets.get(rid)
        if t is None or t.expires_at - 30 <= now:
            if t is not None:
                self._attempt_state(conn, t.attempt_id, "expired")
                self.targets.pop(rid, None)
            t = self._new_attempt(conn, row)
            if t is None:
                return
        path = resolve(self.s.state_dir, row["path"])
        if file_sha256(path) != row["sha256"]:
            self._rec_set(conn, rid, "quarantined", error="local_file_hash_changed")
            bump_counter(conn, "recordings_local_corruption", 1, rid)
            return
        self._rec_set(conn, rid, "uploading")
        self._attempt_state(conn, t.attempt_id, "uploading")
        put = self.uploader.put(t.url, t.headers, path, row["size_bytes"])
        if put.kind == OK:
            self._attempt_state(conn, t.attempt_id, "uploaded", put.status)
            self._complete(conn, row, t.attempt_id)
            return
        self._attempt_state(conn, t.attempt_id, "failed" if put.kind != NOT_FOUND else "expired", put.status, put.error_code)
        if put.kind == NOT_FOUND:
            self.targets.pop(rid, None)
            self._rec_set(conn, rid, "awaiting_upload", delay=1.0, error=put.error_code, attempt_inc=True)
        elif put.kind == INVALID:
            self.targets.pop(rid, None)
            self._rec_set(conn, rid, "awaiting_upload", delay=3600.0, error=put.error_code, attempt_inc=True)
            bump_counter(conn, "storage_upload_rejected", 1, put.error_code)
        else:
            self._rec_set(conn, rid, "awaiting_upload", delay=self._delay(row["attempts"], put), error=put.error_code, attempt_inc=True)

    def _complete(self, conn: sqlite3.Connection, row: sqlite3.Row, attempt_id: str) -> None:
        rid = row["recording_id"]
        self._attempt_state(conn, attempt_id, "completion_sent")
        out = self.api.request("POST", f"/api/v1/device/recordings/{rid}/complete",
                               content=RecordingCompletion(attempt_id=attempt_id).model_dump_json().encode(), model=RecordingStatus)
        if out.kind == OK:
            self._attempt_state(conn, attempt_id, "completed")
            self.targets.pop(rid, None)
            status: RecordingStatus = out.model  # type: ignore[assignment]
            if status.status in ("failed", "verified", "purged"):
                self._verification_result(conn, row, status)
            else:
                self._rec_set(conn, rid, "awaiting_verification", delay=2.0)
        elif out.kind == AUTH:
            self._rec_set(conn, rid, "uploading")
            self._auth(conn, out)
        elif out.kind in (NOT_FOUND, CONFLICT):
            # upload_attempt_mismatch (superseded/too old) or unknown: a fresh attempt is needed.
            self._attempt_state(conn, attempt_id, "failed", detail=out.error_code)
            self.targets.pop(rid, None)
            self._rec_set(conn, rid, "awaiting_upload", delay=5.0, error=out.error_code, attempt_inc=True)
        elif out.kind == INVALID:
            self._rec_set(conn, rid, "awaiting_verification", error=out.error_code)
        else:
            # Outcome unknown: stay in 'uploading'; the next pass queries status before acting.
            self._common_failure(conn, row, out, "uploading")

    def _get_status(self, conn: sqlite3.Connection, row: sqlite3.Row, state_on_error: str) -> RecordingStatus | None:
        rid = row["recording_id"]
        st = self.api.request("GET", f"/api/v1/device/recordings/{rid}", model=RecordingStatus)
        if st.kind == OK:
            return st.model  # type: ignore[return-value]
        if st.kind == NOT_FOUND:
            self._rec_set(conn, rid, "pending_declaration", error="recording_not_found_on_server")
        else:
            self._common_failure(conn, row, st, state_on_error)
        return None

    def _rec_uploading(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
        """Previous outcome uncertain (crash, lost response): ask the server before acting."""
        rid = row["recording_id"]
        status = self._get_status(conn, row, "uploading")
        if status is None:
            return
        if status.status in ("uploaded", "verifying", "verified", "failed", "purged"):
            self._verification_result(conn, row, status)
            return
        last = conn.execute(
            "SELECT * FROM upload_attempts WHERE recording_id=? ORDER BY created_at DESC LIMIT 1", (rid,)
        ).fetchone()
        if last is not None and last["state"] in ("uploaded", "completion_sent") and parse_iso(last["expires_at"]).timestamp() > now:
            self._complete(conn, row, last["attempt_id"])
        else:
            self._rec_set(conn, rid, "awaiting_upload")

    def _rec_awaiting_verification(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
        status = self._get_status(conn, row, "awaiting_verification")
        if status is not None:
            self._verification_result(conn, row, status)

    def _verification_result(self, conn: sqlite3.Connection, row: sqlite3.Row, status: RecordingStatus) -> None:
        rid = row["recording_id"]
        if status.recording_id.lower() != rid:
            self._rec_set(conn, rid, "awaiting_verification", delay=60, error="status_id_mismatch")
            return
        if status.status == "verified":
            server = status.model_dump_json()
            if status.verified_sha256 is None:
                # Verified without naming a checksum: keep the local source (no automatic deletion).
                self._rec_set(conn, rid, "verified", error="verified_without_checksum",
                              extra={"verified_at": status.verified_at or iso_utc(self.clock()), "verified_sha256": None, "server_json": server})
                bump_counter(conn, "recordings_verified_without_checksum", 1)
            elif status.verified_sha256 == row["sha256"]:
                self._rec_set(conn, rid, "verified", extra={"verified_at": status.verified_at or iso_utc(self.clock()),
                                                            "verified_sha256": status.verified_sha256, "server_json": server})
            else:
                self._reupload_after_mismatch(conn, row, "verified_sha256_differs")
            return
        if status.status == "failed":
            reason = status.failure_reason or (status.latest_attempt.failure_reason if status.latest_attempt else None) or "unknown"
            if reason in REUPLOADABLE_FAILURES:
                self._reupload_after_mismatch(conn, row, reason)
            else:
                # The server rejected the media itself (format/codec/duration...): our declaration and
                # bytes disagree. Never rewrite either; keep the clip for diagnosis.
                self._rec_set(conn, rid, "quarantined", error=f"server_rejected_media:{reason}")
                bump_counter(conn, "recordings_quarantined", 1, reason)
            return
        if status.status == "purged":
            self._rec_set(conn, rid, "server_purged", error="purged_by_server_retention")
            return
        if status.status == "pending":
            self._rec_set(conn, rid, "awaiting_upload")
            return
        polls = row["attempts"]
        self._rec_set(conn, rid, "awaiting_verification", delay=min(300.0, 5.0 * (1.5 ** min(polls, 20))), attempt_inc=True)

    def _reupload_after_mismatch(self, conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
        rid = row["recording_id"]
        bump_counter(conn, "recordings_server_verification_failed", 1, f"{rid} {reason}")
        if file_sha256(resolve(self.s.state_dir, row["path"])) != row["sha256"]:
            self._rec_set(conn, rid, "quarantined", error="local_file_corrupted")
            bump_counter(conn, "recordings_local_corruption", 1, rid)
            return
        n = conn.execute("SELECT COUNT(*) n FROM upload_attempts WHERE recording_id=?", (rid,)).fetchone()["n"]
        if n > MAX_CHECKSUM_REUPLOADS:
            self._rec_set(conn, rid, "quarantined", error=f"repeated_verification_failure:{reason}")
            return
        self.targets.pop(rid, None)
        self._rec_set(conn, rid, "awaiting_upload", delay=5.0, error=f"server_{reason}_local_ok")

    # ------------------------------------------------------------------ process

    def _lane(self, name: str, step, idle: float) -> None:
        conn = connect(self.s.db_path)
        try:
            while not self.stop_event.is_set():
                try:
                    worked = step(conn)
                    self.lane_errors.pop(name, None)
                except sqlite3.Error as exc:
                    log.error("%s lane database error: %s", name, exc)
                    self.lane_errors[name] = str(exc)[:200]
                    worked = False
                    self.stop_event.wait(5.0)
                except Exception as exc:
                    log.exception("%s lane error", name)
                    self.lane_errors[name] = f"{type(exc).__name__}: {exc}"[:200]
                    worked = False
                    self.stop_event.wait(5.0)
                self.stop_event.wait(0.05 if worked else idle)
        finally:
            conn.close()

    def write_status(self, conn: sqlite3.Connection) -> None:
        write_status(self.status_path, {
            "component": "delivery",
            "auth_blocked": self.auth.blocked,
            "auth_reason": self.auth.reason,
            "last_heartbeat_success": self.last_heartbeat_success,
            "last_batch_ack": self.last_batch_ack,
            "desired_revision": self.desired_revision,
            "lane_errors": self.lane_errors,
            "backlog": outbox.backlog(conn),
            "provenance": provenance.summary(conn),
        })

    def run(self) -> int:
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(self.s.state_dir, "delivery").acquire()
        try:
            migrate(self.s.db_path, backup_dir=self.s.state_dir / "backups")
            conn = connect(self.s.db_path)
            outbox.reset_leases(conn, self.clock(), all_leases=True)
            with transaction(conn):
                conn.execute("UPDATE event_revisions SET delivery_state='pending' WHERE delivery_state='sending'")
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM, lambda *_: self.stop_event.set())
                signal.signal(signal.SIGINT, lambda *_: self.stop_event.set())
            audio = threading.Thread(target=self._lane, args=("audio", self.audio_step, 2.0), name="audio-lane", daemon=True)
            audio.start()

            def control(c: sqlite3.Connection) -> bool:
                self.control_step(c)
                self.write_status(c)
                return False

            self._lane("control", control, 1.0)
            audio.join(30)
            conn.close()
            return 0
        finally:
            self.api.close()
            self.uploader.close()
            lock.release()


def set_meta_tx(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        set_meta(conn, key, value)


def _set_rev(conn: sqlite3.Connection, key: tuple[str, int], state: str, status: int | None, error: str | None,
             next_at: float, ack: bool = False) -> None:
    with transaction(conn):
        conn.execute(
            f"""UPDATE event_revisions SET delivery_state=?, last_status=?, last_error=?, next_attempt_at=?
                {', acknowledged_at=?' if ack else ''} WHERE event_id=? AND revision=?""",
            (state, status, error, next_at, *([iso_utc(time.time())] if ack else []), *key),
        )


def _expire_events(conn: sqlite3.Connection, now: float, window_days: float) -> None:
    cutoff = int(now - window_days * 86400 + 3600)
    with transaction(conn):
        conn.execute(
            """UPDATE event_revisions SET delivery_state='expired_for_automatic_upload' WHERE delivery_state='pending'
               AND event_id IN (SELECT event_id FROM events WHERE start_second < ?)""",
            (cutoff,),
        )
        conn.execute(
            """UPDATE recordings SET delivery_state='expired_for_automatic_upload' WHERE delivery_state='pending_declaration'
               AND event_id IN (SELECT event_id FROM events WHERE start_second < ?)""",
            (cutoff,),
        )


def main(settings: Settings) -> int:
    token = load_token(settings.paths.credentials_file)
    return DeliveryService(settings, token).run()
