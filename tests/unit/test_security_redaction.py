import json
import os
import zipfile

import httpx
import pytest

from noise_collector.config.settings import CredentialError, ServerSettings, load_token
from noise_collector.transport.api import ApiClient, StorageUploader, classify, redact


def test_redact_tokens_and_signed_urls():
    s = "Authorization: Bearer abc.def-123 url=https://bucket.s3.test/x/y.wav?X-Amz-Signature=deadbeef&X-Amz-Credential=AK"
    r = redact(s)
    assert "abc.def-123" not in r and "deadbeef" not in r and "X-Amz-Credential=AK" not in r
    assert "https://bucket.s3.test/x/y.wav?[REDACTED]" in r


def test_credentials_file_permissions_enforced(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('device_token = "0123456789abcdefXYZ"\n')
    os.chmod(p, 0o644)
    with pytest.raises(CredentialError):
        load_token(p)
    os.chmod(p, 0o600)
    assert load_token(p) == "0123456789abcdefXYZ"


def test_base_url_must_be_origin_and_https():
    with pytest.raises(ValueError):
        ServerSettings(base_url="https://x.test/api")
    with pytest.raises(ValueError):
        ApiClient(ServerSettings(base_url="http://x.test"), "t" * 20)


def test_redirects_not_followed_and_token_not_leaked():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.host == "api.test":
            return httpx.Response(302, headers={"Location": "https://evil.test/steal"})
        return httpx.Response(200, json={})

    api = ApiClient(ServerSettings(base_url="https://api.test"), "secret-token-123456", transport=httpx.MockTransport(handler))
    out = api.request("GET", "/api/v1/device/configuration")
    assert out.kind == "redirect"
    assert all(r.url.host == "api.test" for r in seen)


def test_storage_host_policy_and_header_filter(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"abc")
    up = StorageUploader(ServerSettings(base_url="https://api.test", trusted_storage_hosts=["*.r2.cloudflarestorage.com"]),
                         transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert up.put("https://evil.test/x", {}, f, 3).error_code == "storage_host_not_trusted"
    assert up.put("http://a.r2.cloudflarestorage.com/x", {}, f, 3).error_code == "storage_host_not_trusted"
    assert up.put("https://a.r2.cloudflarestorage.com/x", {"Authorization": "x"}, f, 3).error_code == "storage_headers_rejected"
    assert up.put("https://a.r2.cloudflarestorage.com/x", {"Content-Type": "audio/wav"}, f, 3).ok


def test_classification_table():
    def r(status, **kw):
        return classify(httpx.Response(status, **kw), None).kind

    assert [r(500), r(503), r(429), r(401), r(403), r(404), r(409), r(413), r(422)] == [
        "retry", "retry", "retry", "auth", "auth", "not_found", "conflict", "too_large", "invalid"]
    out = classify(httpx.Response(422, json={"error": {"code": "sequence_gap_" + "x" * 200}}), None)
    assert len(out.error_code) <= 64  # bounded server validation code


def test_export_diagnostics_excludes_secrets_and_audio(make_settings, tmp_path, monkeypatch):
    from noise_collector import cli
    from noise_collector.store.db import migrate

    s = make_settings()
    migrate(s.db_path)
    logs = s.state_dir / "logs"
    logs.mkdir()
    (logs / "delivery.log").write_text("PUT https://bucket.storage.test/x?X-Amz-Signature=supersecret Bearer test-device-token-0123456789\n")
    (s.state_dir / "recordings").mkdir()
    (s.state_dir / "recordings" / "a.wav").write_bytes(b"RIFF")
    cfg = tmp_path / "collector.toml"
    data = s.model_dump(mode="json")
    from noise_collector.cli import _to_toml

    data["paths"] = {k: str(v) for k, v in data["paths"].items()}
    data["server"].pop("ca_bundle")
    cfg.write_text(_to_toml(data))
    out = tmp_path / "diag.zip"
    assert cli.main(["--config", str(cfg), "export-diagnostics", str(out)]) == 0
    with zipfile.ZipFile(out) as z:
        blob = b"".join(z.read(n) for n in z.namelist())
        assert not any(n.startswith("audio/") for n in z.namelist())
    assert b"supersecret" not in blob and b"test-device-token-0123456789" not in blob
    assert b"REDACTED" in blob
    json.loads(zipfile.ZipFile(out).read("database-summary.json"))
