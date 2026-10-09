"""In-process fake of the Laravel device API (my-neighbor-sucks) and presigned object storage.

Behaviour mirrors the web app's controllers/services at the vendored contract commit
(contract/upstream/SOURCE): response envelopes, ``error.retry``/``permanent``, batch and row
idempotency (batch hash excludes ``sent_at``), provenance checks, terminal finalized events, two-stage
recording verification. Every request body is validated against the upstream JSON Schemas, so a
collector payload the real server would reject fails here too. It is a test double; the real
server contract test is tests/integration/test_laravel_contract.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field

import httpx

from noise_collector.contract.configuration import canonical_json

from .upstream import errors as schema_errors

API = "https://api.test"
STORAGE_HOST = "bucket.storage.test"
METRICS = ("laeq_db", "lafmax_db", "lceq_db", "lcpeak_db", "low_frequency_leq_db", "rms_dbfs")
RETRY = {
    "invalid_credentials": "after_correction", "forbidden_ability": "after_correction", "rate_limited": "backoff",
    "service_unavailable": "backoff", "internal_error": "backoff", "clock_future_timestamp": "after_clock_sync",
    "unknown_provenance": "after_configuration_refresh",
}
STATUS = {
    "invalid_credentials": 401, "forbidden_ability": 403, "not_found": 404, "batch_conflict": 409, "measurement_conflict": 409,
    "event_revision_conflict": 409, "event_terminal": 409, "recording_conflict": 409, "recording_already_verified": 409,
    "upload_attempt_mismatch": 409, "payload_too_large": 413, "malformed_json": 422, "validation_failed": 422,
    "unsupported_schema_version": 422, "clock_future_timestamp": 422, "outside_backfill_window": 422,
    "unknown_provenance": 422, "recordings_disabled": 422, "rate_limited": 429, "service_unavailable": 503, "internal_error": 500,
}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(round(ts * 1000)) % 1000:03d}Z"


def _parse(ts: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _h(obj) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _norm_record(r: dict) -> dict:
    out = dict(r)
    for m in METRICS:
        v = out.get(m)
        out[m] = None if v is None else float(v)
    out["quality_flags"] = sorted(set(out.get("quality_flags") or []))
    out["null_reasons"] = dict(sorted((out.get("null_reasons") or {}).items()))
    out["captured_at"] = _iso(_parse(out["captured_at"]))
    out.pop("bands", None)
    return out


class ApiError(Exception):
    def __init__(self, code: str, message: str = "", details: dict | None = None) -> None:
        super().__init__(code)
        self.code, self.message, self.details = code, message or code, details


@dataclass
class FakeServer:
    token: str = "nmd_test-device-token-0123456789abcdefghijklmnopqrstuv"
    config_result: dict | None = None
    url_ttl_s: float = 900.0
    clock: callable = time.time
    verify_on_poll: bool = True
    recordings_enabled: bool = True
    down: bool = False
    faults: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    batches: dict[str, tuple[str, dict]] = field(default_factory=dict)
    records: dict[tuple, str] = field(default_factory=dict)
    events: dict[str, dict] = field(default_factory=dict)
    revisions: dict[tuple, str] = field(default_factory=dict)
    recordings: dict[str, dict] = field(default_factory=dict)
    attempts: dict[str, dict] = field(default_factory=dict)
    objects: dict[str, bytes] = field(default_factory=dict)
    acks: list[dict] = field(default_factory=list)
    heartbeats: list[dict] = field(default_factory=list)
    applied_revision: int | None = None
    log: list[tuple] = field(default_factory=list)
    storage_requests: list[httpx.Request] = field(default_factory=list)
    schema_violations: list[tuple[str, list[str]]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)  # calibration attachment id -> bytes served

    # -- fault helpers -----------------------------------------------------------------------

    def inject(self, route: str, *actions: str) -> None:
        """Queue actions for the next requests on ``route`` (batches, events, declare, attempts, complete,
        status, config, acks, heartbeat, storage): 'http_503', 'http_429:5', 'code:<error_code>',
        'drop_after_commit', 'timeout', 'connect', 'tls', 'malformed', 'redirect', 'expire', 'corrupt_object'."""
        self.faults[route].extend(actions)

    def _fault(self, route: str) -> str | None:
        q = self.faults.get(route)
        return q.popleft() if q else None

    def _envelope(self, status: int, body: dict, headers: dict | None = None) -> httpx.Response:
        rid = str(uuid.uuid4())
        return httpx.Response(status, json={"request_id": rid, "server_received_at": _iso(self.clock()), **body},
                              headers={"X-Request-Id": rid, **(headers or {})})

    def _err(self, code: str, message: str = "", details: dict | None = None, headers: dict | None = None) -> httpx.Response:
        retry = RETRY.get(code, "never")
        body = {"error": {"code": code, "message": message or code, "retry": retry, "permanent": retry not in ("backoff",),
                          **({"details": details} if details else {})}}
        return self._envelope(STATUS[code], body, headers)

    def _pre(self, route: str, request: httpx.Request):
        f = self._fault(route)
        if f is None:
            return None, None
        if f == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if f == "connect":
            raise httpx.ConnectError("simulated connection refused", request=request)
        if f == "tls":
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] simulated", request=request)
        if f == "malformed":
            return httpx.Response(200, content=b"<html>proxy</html>"), None
        if f == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.test/"}), None
        if f.startswith("code:"):
            return self._err(f[5:]), None
        m = re.fullmatch(r"http_(\d+)(?::(\d+))?", f)
        if m:
            status = int(m.group(1))
            code = {429: "rate_limited", 503: "service_unavailable", 500: "internal_error", 413: "payload_too_large"}.get(status)
            hdr = {"Retry-After": m.group(2)} if m.group(2) else None
            if code:
                return self._err(code, headers=hdr), None
            return httpx.Response(status), None
        return None, f

    def _body(self, request: httpx.Request, component: str) -> dict:
        try:
            body = json.loads(request.content)
        except ValueError:
            raise ApiError("malformed_json") from None
        if not isinstance(body, dict):
            raise ApiError("malformed_json")
        if body.get("schema_version") != 1:
            raise ApiError("unsupported_schema_version")
        errs = schema_errors(component, body)
        if errs:
            self.schema_violations.append((component, errs))
            raise ApiError("validation_failed", "schema", {"errors": errs[:50]})
        return body

    # -- provenance --------------------------------------------------------------------------

    def _channel_cfg(self, channel: str) -> tuple[dict, dict, dict | None]:
        if not self.config_result:
            raise ApiError("unknown_provenance")
        doc = self.config_result["configuration"]
        ch = next((c for c in doc["channels"] if c["channel"] == channel), None)
        if ch is None:
            raise ApiError("validation_failed", "channel")
        prov = self.config_result["provenance"]
        prof = next(p for p in prov["measurement_profiles"] if p["id"] == ch["measurement_profile_id"])
        cal = next((c for c in prov["calibrations"] if c["id"] == ch["calibration_id"]), None)
        return ch, prof, cal

    def _check_provenance(self, rec: dict) -> None:
        ch, prof, _cal = self._channel_cfg(rec["channel"])
        if (rec["deployment_id"] != ch["deployment_id"] or rec["profile_id"] != prof["id"]
                or rec.get("calibration_id") != ch["calibration_id"] or rec["configuration_revision"] != self.config_result["revision"]):
            raise ApiError("unknown_provenance")

    # -- API -------------------------------------------------------------------------------

    def api_handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.down:
            raise httpx.ConnectError("network unreachable (simulated outage)", request=request)
        self.log.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return self._err("invalid_credentials")
        routes = [
            ("POST", r"/api/v1/device/measurements/batches", self._batches, "batches"),
            ("POST", r"/api/v1/device/events", self._events, "events"),
            ("POST", r"/api/v1/device/events/([^/]+)/recordings", self._declare, "declare"),
            ("POST", r"/api/v1/device/recordings/([^/]+)/upload-attempts", self._reissue, "attempts"),
            ("POST", r"/api/v1/device/recordings/([^/]+)/complete", self._complete, "complete"),
            ("GET", r"/api/v1/device/recordings/([^/]+)", self._status, "status"),
            ("GET", r"/api/v1/device/configuration", self._config, "config"),
            ("POST", r"/api/v1/device/configuration/acknowledgments", self._ack, "acks"),
            ("GET", r"/api/v1/device/calibrations/([^/]+)/attachments/([^/]+)", self._attachment, "attachment"),
            ("POST", r"/api/v1/device/heartbeat", self._heartbeat, "heartbeat"),
        ]
        for method, pattern, fn, name in routes:
            m = re.fullmatch(pattern, path)
            if m and request.method == method:
                early, post = self._pre(name, request)
                if early is not None:
                    return early
                try:
                    resp = fn(request, *m.groups())
                except ApiError as e:
                    return self._err(e.code, e.message, e.details)
                if post == "drop_after_commit":
                    raise httpx.ReadTimeout("response lost after commit", request=request)
                return resp
        return self._err("not_found")

    def _batches(self, request: httpx.Request) -> httpx.Response:
        if len(request.content) > 1024 * 1024:
            raise ApiError("payload_too_large")
        b = self._body(request, "MeasurementBatchRequest")
        recs = [_norm_record(r) for r in b["records"]]
        now = self.clock()
        for r in b["records"]:
            start = _parse(r["captured_at"])
            if start != int(start):
                raise ApiError("validation_failed", "captured_at must be a whole second")
            if start + 1 > now + 300:
                raise ApiError("clock_future_timestamp")
            if start < now - 30 * 86400:
                raise ApiError("outside_backfill_window")
            self._check_provenance(r)
            _ch, prof, _cal = self._channel_cfg(r["channel"])
            absolute_ok = prof["calibration_state"] != "uncalibrated"
            for m in METRICS:
                applicable = m in prof["supported_metrics"] and (m == "rms_dbfs" or absolute_ok)
                v = r.get(m)
                if v is not None and not applicable:
                    raise ApiError("validation_failed", f"{m} not applicable")
                if v is None and applicable and m not in (r.get("null_reasons") or {}) and not r.get("quality_flags"):
                    raise ApiError("validation_failed", f"null {m} needs a reason")
            if all(r.get(m) is None for m in METRICS) and not r.get("quality_flags"):
                raise ApiError("validation_failed", "no metric and no flag")
        batch_hash = _h({"schema_version": 1, "batch_id": b["batch_id"],
                         "records": sorted(recs, key=lambda r: (r["channel"], r["boot_id"], r["sequence"]))})
        prior = self.batches.get(b["batch_id"])
        if prior is not None:
            if prior[0] != batch_hash:
                raise ApiError("batch_conflict")
            return self._envelope(200, {**prior[1], "replayed": True})
        inserted = dup = 0
        conflicts = []
        for r in recs:
            key = (r["channel"], r["boot_id"], r["sequence"])
            if key in self.records:
                if self.records[key] != _h(r):
                    conflicts.append({"channel": key[0], "boot_id": key[1], "sequence": key[2], "reason": "changed_payload"})
                dup += 1
            else:
                inserted += 1
        if conflicts:
            raise ApiError("measurement_conflict", details={"conflicts": conflicts[:50], "conflict_count": len(conflicts)})
        for r in recs:
            self.records[(r["channel"], r["boot_id"], r["sequence"])] = _h(r)
        starts = [_parse(r["captured_at"]) for r in recs]
        result = {"batch_id": b["batch_id"], "status": "accepted", "record_count": len(recs), "inserted_count": inserted,
                  "duplicate_count": dup, "accepted_interval": {"start": _iso(min(starts)), "end": _iso(max(starts) + 1)},
                  "received_at": _iso(self.clock()), "replayed": False}
        self.batches[b["batch_id"]] = (batch_hash, result)
        return self._envelope(201, result)

    def _events(self, request: httpx.Request) -> httpx.Response:
        b = self._body(request, "EventRevisionRequest")
        if b["detection_state"] == "open" and b.get("ended_at") is not None:
            raise ApiError("validation_failed", "open event must have null end")
        if b["detection_state"] == "finalized" and b.get("ended_at") is None:
            raise ApiError("validation_failed", "finalized event requires ended_at")
        if b.get("ended_at") and _parse(b["ended_at"]) < _parse(b["started_at"]):
            raise ApiError("validation_failed", "ended_at precedes started_at")
        self._check_provenance(b)
        _ch, prof, _cal = self._channel_cfg(b["channel"])
        if prof["calibration_state"] == "uncalibrated":
            if b["detection"]["trigger_metric"] != "rms_dbfs":
                raise ApiError("validation_failed", "uncalibrated triggers on rms_dbfs only")
            if any(v is not None for k, v in (b.get("summary") or {}).items() if k not in ("duration_ms", "rms_dbfs")):
                raise ApiError("validation_failed", "absolute summary must be null")
        canon = {k: v for k, v in b.items() if k not in ("sent_at", "schema_version")}
        h = _h(canon)
        eid, rev = b["event_id"], b["revision"]
        ev = self.events.get(eid)
        key = (eid, rev)
        if key in self.revisions:
            if self.revisions[key] != h:
                raise ApiError("event_revision_conflict")
            return self._envelope(200, self._event_body(eid, rev, "duplicate"))
        if ev is not None:
            if ev["channel"] != b["channel"]:
                raise ApiError("event_revision_conflict")
            if rev < ev["current_revision"]:
                self.revisions[key] = h
                return self._envelope(200, self._event_body(eid, rev, "stored_superseded", applied=False))
            if ev["detection_state"] == "finalized":
                raise ApiError("event_terminal")
        self.revisions[key] = h
        self.events[eid] = {"channel": b["channel"], "current_revision": rev, "detection_state": b["detection_state"], "payload": b}
        return self._envelope(201, self._event_body(eid, rev, "stored"))

    def _event_body(self, eid: str, rev: int, outcome: str, applied: bool = True) -> dict:
        ev = self.events[eid]
        return {"event_id": eid, "revision": rev, "outcome": outcome, "applied_to_projection": applied,
                "current_revision": ev["current_revision"], "detection_state": ev["detection_state"],
                "completeness_state": "pending", "recording_state": "pending"}

    def _target(self, rid: str) -> dict:
        for a in self.attempts.values():
            if a["recording_id"] == rid and a["state"] == "issued":
                a["state"] = "superseded"
        aid = str(uuid.uuid4())
        exp = self.clock() + self.url_ttl_s
        self.attempts[aid] = {"recording_id": rid, "expires": exp, "uploaded": False, "state": "issued", "failure": None}
        self.recordings[rid]["latest_attempt"] = aid
        return {"attempt_id": aid, "method": "PUT",
                "url": f"https://{STORAGE_HOST}/staging/acct/dev/{rid}/{aid}.wav?X-Amz-Signature=secret-signature&attempt={aid}",
                "headers": {}, "expires_at": _iso(exp)}

    def _rec_body(self, rid: str) -> dict:
        rec = self.recordings[rid]
        aid = rec.get("latest_attempt")
        att = self.attempts.get(aid) if aid else None
        return {"recording_id": rid, "event_id": rec["event_id"], "segment_number": rec["declaration"]["segment_number"],
                "status": rec["status"], "verified": rec["status"] == "verified",
                "verified_sha256": rec.get("verified_sha256"), "verified_at": rec.get("verified_at"),
                "failure_reason": rec.get("failure"), "retain_local_copy": rec["status"] not in ("verified", "purged"),
                "latest_attempt": None if att is None else {"attempt_id": aid, "state": att["state"], "expires_at": _iso(att["expires"]),
                                                            "failure_reason": att["failure"]}}

    def _declare(self, request: httpx.Request, event_id: str) -> httpx.Response:
        if event_id.lower() not in self.events:
            raise ApiError("not_found", "Unknown event for this device; submit the event before its recording.")
        if not self.recordings_enabled:
            raise ApiError("recordings_disabled")
        d = self._body(request, "RecordingDeclaration")
        if (d["mime_type"] == "audio/flac") != (d["codec"] == "flac"):
            raise ApiError("validation_failed", "codec/mime mismatch")
        rid = d["recording_id"].lower()
        canon = {k: v for k, v in d.items() if k != "schema_version"}
        existing = self.recordings.get(rid)
        if existing is not None:
            if existing["hash"] != _h(canon) or existing["event_id"] != event_id:
                raise ApiError("recording_conflict")
            body = {"recording_id": rid, "event_id": event_id, "segment_number": d["segment_number"], "status": existing["status"],
                    "verified": existing["status"] == "verified"}
            if existing["status"] not in ("verified", "purged"):
                body["upload"] = self._target(rid)
            return self._envelope(200, body)
        if any(r["event_id"] == event_id and r["declaration"]["segment_number"] == d["segment_number"] for r in self.recordings.values()):
            raise ApiError("recording_conflict", "segment taken")
        self.recordings[rid] = {"declaration": d, "event_id": event_id, "status": "pending", "hash": _h(canon)}
        body = {"recording_id": rid, "event_id": event_id, "segment_number": d["segment_number"], "status": "pending", "verified": False,
                "upload": self._target(rid)}
        return self._envelope(201, body)

    def _reissue(self, request: httpx.Request, rid: str) -> httpx.Response:
        rec = self.recordings.get(rid)
        if rec is None:
            raise ApiError("not_found")
        if rec["status"] == "verified":
            raise ApiError("recording_already_verified")
        if rec["status"] == "purged":
            raise ApiError("recording_conflict")
        return self._envelope(201, {"recording_id": rid, "event_id": rec["event_id"], "segment_number": rec["declaration"]["segment_number"],
                                    "status": rec["status"], "verified": False, "upload": self._target(rid)})

    def _complete(self, request: httpx.Request, rid: str) -> httpx.Response:
        rec = self.recordings.get(rid)
        if rec is None:
            raise ApiError("not_found")
        b = self._body(request, "RecordingCompletion")
        att = self.attempts.get(b["attempt_id"])
        if att is None or att["recording_id"] != rid or att["state"] == "superseded":
            raise ApiError("upload_attempt_mismatch")
        if rec["status"] in ("verified", "purged") or att["state"] == "failed":
            return self._envelope(200, self._rec_body(rid))
        att["state"] = "completed"
        rec["status"] = "uploaded"
        rec["pending_attempt"] = b["attempt_id"]
        return self._envelope(202, self._rec_body(rid))

    def _verify(self, rid: str) -> None:
        rec = self.recordings[rid]
        aid = rec.pop("pending_attempt", None)
        if aid is None:
            return
        att = self.attempts[aid]
        data = self.objects.get(aid)
        d = rec["declaration"]
        reason = None
        if data is None:
            reason = "object_missing"
        elif len(data) != d["byte_size"]:
            reason = "size_mismatch"
        elif hashlib.sha256(data).hexdigest() != d["sha256"]:
            reason = "sha256_mismatch"
        if reason:
            rec.update(status="failed", failure=reason)
            att.update(state="failed", failure=reason)
        else:
            rec.update(status="verified", verified_sha256=d["sha256"], verified_at=_iso(self.clock()), failure=None)
            att["state"] = "verified"

    def _status(self, request: httpx.Request, rid: str) -> httpx.Response:
        if rid not in self.recordings:
            raise ApiError("not_found")
        if self.verify_on_poll:
            self._verify(rid)
        return self._envelope(200, self._rec_body(rid))

    def _config(self, request: httpx.Request) -> httpx.Response:
        if not self.config_result:
            raise ApiError("not_found", "No configuration has been published for this device yet.")
        body = {k: v for k, v in self.config_result.items() if k not in ("request_id", "server_received_at")}
        body["applied_revision"] = self.applied_revision
        return self._envelope(200, body, {"ETag": f'"{body["sha256"]}"'})

    def _ack(self, request: httpx.Request) -> httpx.Response:
        b = self._body(request, "ConfigurationAcknowledgment")
        if b["status"] == "rejected" and not b.get("reason"):
            raise ApiError("validation_failed", "reason required")
        if not self.config_result or b["revision"] > self.config_result["revision"]:
            raise ApiError("not_found", "Unknown configuration revision for this device.")
        if b["revision"] == self.config_result["revision"] and b.get("content_hash") and b["content_hash"] != self.config_result["sha256"]:
            raise ApiError("validation_failed", "content_hash")
        last = next((a for a in reversed(self.acks) if a["revision"] == b["revision"]), None)
        duplicate = last is not None and last["status"] == b["status"] and last.get("reason") == b.get("reason")
        if not duplicate:
            self.acks.append(b)
            if b["status"] == "applied":
                self.applied_revision = b["revision"]
        return self._envelope(200 if duplicate else 201, {"revision": b["revision"], "status": b["status"], "recorded": not duplicate,
                                                          "desired_config_revision": self._desired(), "applied_config_revision": self.applied_revision})

    def _attachment(self, request: httpx.Request, cal_id: str, att_id: str) -> httpx.Response:
        cals = (self.config_result or {}).get("provenance", {}).get("calibrations", [])
        cal = next((c for c in cals if c["id"] == cal_id), None)
        att = next((a for a in (cal or {}).get("attachments", []) if a["id"] == att_id and a["purpose"] == "frequency_response"), None)
        if att is None or att_id not in self.files:
            raise ApiError("not_found")
        data = self.files[att_id]
        return httpx.Response(200, content=data, headers={"Content-Type": "application/octet-stream",
                                                          "X-Content-SHA256": att["sha256"], "X-Request-Id": str(uuid.uuid4())})

    def _desired(self) -> int | None:
        return self.config_result["revision"] if self.config_result else None

    def _heartbeat(self, request: httpx.Request) -> httpx.Response:
        b = self._body(request, "Heartbeat")
        self.heartbeats.append(b)
        desired = self._desired()
        return self._envelope(200, {"desired_config_revision": desired, "applied_config_revision": self.applied_revision,
                                    "configuration_pending": desired is not None and desired != b.get("applied_config_revision"),
                                    "heartbeat_interval_seconds": 60, "reporting_interval_seconds": 30})

    # -- storage ---------------------------------------------------------------------------

    def storage_handler(self, request: httpx.Request) -> httpx.Response:
        self.storage_requests.append(request)
        if request.url.host != STORAGE_HOST:
            return httpx.Response(404)
        if "authorization" in request.headers:
            return httpx.Response(400, content=b"credentials must not be forwarded")
        aid = request.url.params.get("attempt")
        att = self.attempts.get(aid)
        f = self._fault("storage")
        if f == "timeout":
            raise httpx.WriteTimeout("simulated upload timeout", request=request)
        if att is None:
            return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")
        if f == "expire" or self.clock() > att["expires"]:
            return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>")
        if f and f.startswith("http_"):
            return httpx.Response(int(f[5:]))
        data = request.read()
        if f == "corrupt_object":
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        self.objects[aid] = data
        att["uploaded"] = True
        return httpx.Response(200, headers={"ETag": '"not-a-sha256"'})

    def transports(self) -> tuple[httpx.MockTransport, httpx.MockTransport]:
        return httpx.MockTransport(self.api_handler), httpx.MockTransport(self.storage_handler)
