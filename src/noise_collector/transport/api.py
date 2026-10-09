"""Authenticated device API client and storage uploader with outcome classification.

* HTTPS with certificate validation for API and storage; redirects are never followed (a
  redirect could carry the bearer token elsewhere), and are reported as errors.
* The bearer token is sent only to the configured API origin. Storage PUTs carry only the
  headers returned by the server, and only to hosts allowed by the local trusted-provider policy.
* Every response is classified into an ``Outcome`` the delivery state machine acts on.
"""

from __future__ import annotations

import fnmatch
import uuid
import hashlib
import logging
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from .. import __version__
from ..config.settings import ServerSettings
from ..contract.models import REQUEST_ID_HEADER, ErrorEnvelope

log = logging.getLogger(__name__)

OK, RETRY, AUTH, NOT_FOUND, CONFLICT, TOO_LARGE, INVALID, MALFORMED, REDIRECT = (
    "ok", "retry", "auth", "not_found", "conflict", "too_large", "invalid", "malformed", "redirect"
)
# Server ``error.retry`` hints that need their own handling (contract "Retry behaviour").
AFTER_CLOCK_SYNC, AFTER_CONFIG_REFRESH, OUTSIDE_WINDOW = "after_clock_sync", "after_configuration_refresh", "outside_window"


@dataclass
class Outcome:
    kind: str
    status: int | None = None
    body: Any = None
    model: BaseModel | None = None
    error_code: str | None = None
    retry_after: float | None = None
    request_id: str | None = None
    tls_error: bool = False
    permanent: bool | None = None
    details: dict | None = None

    @property
    def ok(self) -> bool:
        return self.kind == OK


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        import time

        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def redact(text: str) -> str:
    """Strip bearer tokens and presigned query strings from text destined for logs/exports."""
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']*", r"\1?[REDACTED]", text)
    text = re.sub(r"(?i)(x-amz-[a-z-]+|signature|token)=[^&\s\"']+", r"\1=[REDACTED]", text)
    return text


def classify(resp: httpx.Response, model: type[BaseModel] | None) -> Outcome:
    """Map a response to an outcome, preferring the server's machine-readable ``error.retry``."""
    rid = resp.headers.get("x-request-id")
    st = resp.status_code
    body: Any = None
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    err = None
    if body is not None and st >= 400:
        try:
            err = ErrorEnvelope.model_validate(body).error
        except ValidationError:
            err = None
    code = err.code[:64] if err else None
    extra = {"request_id": rid, "permanent": err.permanent if err else None, "details": err.details if err else None}
    if 200 <= st < 300:
        if model is None:
            return Outcome(OK, st, body, request_id=rid)
        try:
            return Outcome(OK, st, body, model=model.model_validate(body), request_id=rid)
        except ValidationError as exc:
            return Outcome(MALFORMED, st, None, error_code=f"malformed_response:{exc.error_count()}", request_id=rid)
    if 300 <= st < 400:
        return Outcome(REDIRECT, st, None, error_code="redirect_refused", request_id=rid)
    retry_after = _retry_after(resp.headers.get("retry-after"))
    hint = err.retry if err else None
    # Only credential problems block authenticated traffic; other ``after_correction`` errors (for
    # example ``provenance_conflict``) concern one payload and are classified by status below.
    if st in (401, 403) or code in ("invalid_credentials", "forbidden_ability"):
        return Outcome(AUTH, st, None, error_code=code or "unauthorized", **extra)
    if hint == "after_clock_sync":
        return Outcome(AFTER_CLOCK_SYNC, st, None, error_code=code, **extra)
    if hint == "after_configuration_refresh":
        return Outcome(AFTER_CONFIG_REFRESH, st, None, error_code=code, **extra)
    if code == "outside_backfill_window":
        return Outcome(OUTSIDE_WINDOW, st, None, error_code=code, **extra)
    if st == 429 or st >= 500 or hint == "backoff":
        return Outcome(RETRY, st, None, error_code=code or f"http_{st}", retry_after=retry_after, **extra)
    if st == 404:
        return Outcome(NOT_FOUND, st, None, error_code=code or "not_found", **extra)
    if st == 409:
        return Outcome(CONFLICT, st, None, error_code=code or "conflict", **extra)
    if st == 413:
        return Outcome(TOO_LARGE, st, None, error_code=code or "payload_too_large", **extra)
    return Outcome(INVALID, st, None, error_code=code or f"http_{st}", **extra)


def _transport_outcome(exc: Exception) -> Outcome:
    tls = isinstance(exc, httpx.ConnectError) and "CERTIFICATE" in str(exc).upper()
    return Outcome(RETRY, None, None, error_code=("tls_error" if tls else type(exc).__name__)[:64], tls_error=tls)


class ApiClient:
    def __init__(self, settings: ServerSettings, token: str, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.origin_ok():
            raise ValueError("API origin must be https")
        self.settings = settings
        self.origin = settings.base_url
        verify: Any = str(settings.ca_bundle) if settings.ca_bundle else True
        self.client = httpx.Client(
            base_url=self.origin,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": f"noise-collector/{__version__}",
            },
            timeout=httpx.Timeout(settings.response_timeout_s, connect=settings.connect_timeout_s),
            follow_redirects=False,
            verify=verify,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, *, content: bytes | None = None, model: type[BaseModel] | None = None) -> Outcome:
        if not path.startswith("/api/v1/device/"):
            raise ValueError("device API path required")
        headers = {REQUEST_ID_HEADER: uuid.uuid4().hex}
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self.client.request(method, path, content=content, headers=headers)
        except httpx.HTTPError as exc:
            return _transport_outcome(exc)
        return classify(resp, model)

    def get_bytes(self, url_or_path: str, max_bytes: int = 16 * 1024 * 1024) -> tuple[Outcome, bytes | None]:
        """Download a profile asset from the API origin only (never another host)."""
        u = urlparse(url_or_path)
        if u.scheme or u.netloc:
            if f"{u.scheme}://{u.netloc}" != self.origin:
                return Outcome(INVALID, None, error_code="asset_origin_not_permitted"), None
            path = u.path + (f"?{u.query}" if u.query else "")
        else:
            path = url_or_path
        try:
            with self.client.stream("GET", path) as resp:
                if resp.status_code != 200:
                    resp.read()
                    return classify(resp, None), None
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        return Outcome(INVALID, 200, error_code="asset_too_large"), None
                return Outcome(OK, 200), bytes(buf)
        except httpx.HTTPError as exc:
            return _transport_outcome(exc), None


class StorageUploader:
    """PUT a finalized file to a presigned staging URL without API credentials."""

    def __init__(self, settings: ServerSettings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        verify: Any = str(settings.ca_bundle) if settings.ca_bundle else True
        self.client = httpx.Client(follow_redirects=False, verify=verify, transport=transport,
                                   headers={"User-Agent": f"noise-collector/{__version__}"})

    def host_allowed(self, url: str) -> bool:
        u = urlparse(url)
        if u.scheme != "https" and not self.settings.allow_insecure_http_for_tests:
            return False
        host = (u.hostname or "").lower()
        return any(fnmatch.fnmatchcase(host, pat.lower()) for pat in self.settings.trusted_storage_hosts)

    def timeout_for(self, size: int) -> float:
        return max(60.0, size / self.settings.upload_min_bandwidth_bytes_per_s * 1.5)

    def put(self, url: str, headers: dict[str, str], path: Path, size: int) -> Outcome:
        if not self.host_allowed(url):
            return Outcome(INVALID, None, error_code="storage_host_not_trusted")
        bad = {k for k in headers if k.lower() in ("authorization", "cookie", "proxy-authorization")}
        if bad:
            return Outcome(INVALID, None, error_code="storage_headers_rejected")
        hdrs = dict(headers)
        hdrs.setdefault("Content-Length", str(size))

        def body():
            with open(path, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    yield chunk

        try:
            resp = self.client.put(url, content=body(), headers=hdrs,
                                   timeout=httpx.Timeout(self.timeout_for(size), connect=self.settings.connect_timeout_s))
        except httpx.HTTPError as exc:
            return _transport_outcome(exc)
        if 200 <= resp.status_code < 300:
            return Outcome(OK, resp.status_code)
        if resp.status_code in (400, 403) and b"xpired" in resp.content[:2048]:
            return Outcome(NOT_FOUND, resp.status_code, error_code="upload_url_expired")
        if resp.status_code == 403:
            return Outcome(NOT_FOUND, resp.status_code, error_code="upload_forbidden_or_expired")
        if resp.status_code in (408, 429) or resp.status_code >= 500:
            return Outcome(RETRY, resp.status_code, error_code=f"storage_http_{resp.status_code}",
                           retry_after=_retry_after(resp.headers.get("retry-after")))
        return Outcome(INVALID, resp.status_code, error_code=f"storage_http_{resp.status_code}")

    def close(self) -> None:
        self.client.close()


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["ApiClient", "StorageUploader", "Outcome", "classify", "redact"]
