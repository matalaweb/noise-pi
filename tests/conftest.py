from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from noise_collector.config.settings import Settings  # noqa: E402
from noise_collector.synth import Burst, Scenario, write_wav  # noqa: E402
from support.fake_server import API, STORAGE_HOST, FakeServer  # noqa: E402

START_UTC = 1_790_000_000.0


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def make_settings(tmp_path: Path, state_dir: Path):
    def _make(**overrides) -> Settings:
        cred = tmp_path / "credentials.toml"
        cred.write_text('device_token = "nmd_test-device-token-0123456789abcdefghijklmnopqrstuv"\n')
        os.chmod(cred, 0o600)
        data = {
            "paths": {"state_dir": str(state_dir), "credentials_file": str(cred)},
            "server": {"base_url": API, "trusted_storage_hosts": [STORAGE_HOST]},
            "microphone": {"usb_vendor_id": "2752", "usb_product_id": "0007", "usb_serial": "7000001",
                           "gain_reference_check": "test: synthetic, no physical gain",
                           "microphone_model": "SYNTHETIC test microphone"},
            # SYNTHETIC calibrated chain: 94 dB SPL reads -18 dBFS (the examples' placeholder scale).
            "calibration": {"state": "calibrated", "sensitivity_dbfs_at_94db": -18.0, "reference_method": "SYNTHETIC test"},
            "channel": {"id": "mic-1"},
        }
        for k, v in overrides.items():
            data.setdefault(k, {}).update(v)
        return Settings.model_validate(data)

    return _make


@pytest.fixture
def fake_server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def delivery(make_settings, fake_server, state_dir):
    from noise_collector.delivery.service import DeliveryService
    from noise_collector.store.db import migrate

    def _make(clock=None, **settings_overrides):
        s = make_settings(**settings_overrides)
        migrate(s.db_path)
        api_t, st_t = fake_server.transports()
        svc = DeliveryService(s, fake_server.token, transport=api_t, storage_transport=st_t, rng=random.Random(7),
                              clock=clock or fake_server.clock)
        return svc

    return _make


_CACHE: dict[str, Path] = {}


@pytest.fixture(scope="session")
def scenario_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("scenarios")


@pytest.fixture(scope="session")
def wav_factory(scenario_dir: Path):
    """Render and cache SYNTHETIC scenarios by name."""

    def _wav(name: str, scenario: Scenario, bits: int = 24) -> Path:
        key = f"{name}-{bits}"
        if key not in _CACHE:
            p = scenario_dir / f"{key}.wav"
            write_wav(str(p), scenario.render(), scenario.fs, bits)
            _CACHE[key] = p
        return _CACHE[key]

    return _wav


def engine_scenario(duration: float = 240.0, start: float = 150.0, length: float = 20.0, level: float = -35.0) -> Scenario:
    return Scenario(duration_s=duration, background_dbfs=-60, bursts=[Burst(start, length, "engine_like", level)])
