"""CLI commands that change state (provision) or compute reports (calibration-check)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from noise_collector import cli
from noise_collector.contract.examples import configuration_result
from noise_collector.synth import write_wav


@pytest.fixture
def bootstrap(tmp_path, make_settings, fake_server, monkeypatch):
    from noise_collector.transport import api as api_mod

    s = make_settings()
    fake_server.config_result = configuration_result(3)
    real = api_mod.ApiClient

    class Patched(real):
        def __init__(self, settings, token, transport=None):
            super().__init__(settings, token, transport=fake_server.transports()[0])

    monkeypatch.setattr(api_mod, "ApiClient", Patched)
    boot = tmp_path / "bootstrap.toml"
    boot.write_text(f"""
[paths]
state_dir = "{s.state_dir}"
credentials_file = "{s.paths.credentials_file}"

[server]
base_url = "https://api.test"
trusted_storage_hosts = ["bucket.storage.test"]

[microphone]
usb_vendor_id = "2752"
usb_product_id = "0007"
usb_serial = "7000001"
microphone_model = "SYNTHETIC test microphone"
gain_reference_check = "test: synthetic"

[calibration]
state = "calibrated"
sensitivity_dbfs_at_94db = -18.0
reference_method = "SYNTHETIC test"

[channel]
id = "mic-1"
""")
    return boot, s, tmp_path


def test_provision_requires_confirmation_then_stages(bootstrap, capsys):
    boot, s, tmp = bootstrap
    out = tmp / "collector.toml"
    assert cli.main(["provision", "--bootstrap", str(boot), "--output", str(out)]) == 1
    assert not out.exists()  # nothing changed without --yes
    assert cli.main(["provision", "--bootstrap", str(boot), "--output", str(out), "--yes"]) == 0
    from noise_collector.config.settings import load_settings

    loaded = load_settings(out)
    assert loaded.microphone.usb_serial == "7000001"
    assert "device_token" not in out.read_text()  # secrets never land in settings
    from noise_collector.store.db import connect

    row = connect(s.db_path).execute("SELECT revision, state FROM configurations").fetchone()
    assert (row["revision"], row["state"]) == (3, "staged")


def test_provision_reports_an_unusable_chain(bootstrap, capsys):
    boot, s, tmp = bootstrap
    boot.write_text(boot.read_text().replace('sensitivity_dbfs_at_94db = -18.0\n', ""))
    assert cli.main(["provision", "--bootstrap", str(boot), "--output", str(tmp / "c.toml"), "--yes"]) == 3
    assert "needs sensitivity_dbfs_at_94db" in capsys.readouterr().out


def test_provision_without_published_configuration_runs_on_defaults(bootstrap, fake_server, capsys):
    boot, s, tmp = bootstrap
    fake_server.config_result = None
    assert cli.main(["provision", "--bootstrap", str(boot), "--output", str(tmp / "c.toml"), "--yes"]) == 0
    assert "local defaults" in capsys.readouterr().out


def test_calibration_check_from_reference_file(tmp_path, capsys, monkeypatch):
    fs = 48000
    t = np.arange(fs * 5) / fs
    rms_dbfs = -26.0
    x = math.sqrt(2) * 10 ** (rms_dbfs / 20) * np.sin(2 * np.pi * 1000 * t)
    x += np.random.default_rng(0).standard_normal(len(x)) * 1e-4  # background well below the tone
    ref = tmp_path / "ref.wav"
    write_wav(str(ref), x)
    monkeypatch.setenv("NOISE_COLLECTOR_CONFIG", str(tmp_path / "missing.toml"))
    assert cli.main(["calibration-check", "--level", "94.0", "--file", str(ref), "--sensitivity-dbfs", "-26.0"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert abs(rep["measured_band_dbfs"] - rms_dbfs) < 0.05
    expected_scale = 20e-6 * 10 ** (94 / 20) / 10 ** (rms_dbfs / 20)
    assert abs(rep["scale_pa_per_fs_from_reference"] / expected_scale - 1) < 0.01
    assert abs(rep["sensitivity_vs_reference_db"]) < 0.05
