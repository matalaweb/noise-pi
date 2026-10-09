
from noise_collector.health.status import write_status
from noise_collector.supervisor import Supervisor


def test_watchdog_requires_progressing_capture(make_settings, monkeypatch, tmp_path):
    monkeypatch.setenv("NOISE_COLLECTOR_RUN_DIR", str(tmp_path / "run"))
    s = make_settings()
    sup = Supervisor(s, None)
    assert sup.acquisition_progressing() == (False, "no acquisition status")
    p = tmp_path / "run" / "acquisition-status.json"
    write_status(p, {"microphone_state": "ok", "last_durable_commit_age_s": 1.0})
    assert sup.acquisition_progressing()[0]
    write_status(p, {"microphone_state": "ok", "last_durable_commit_age_s": 120.0})
    ok, why = sup.acquisition_progressing()
    assert not ok and "no durable measurement" in why
    # a disconnected microphone is a reported state, not a hung process: no restart loop
    write_status(p, {"microphone_state": "disconnected", "last_durable_commit_age_s": 500.0})
    assert sup.acquisition_progressing()[0]
    # a storage failure is critical but restarting capture would not fix it
    write_status(p, {"microphone_state": "ok", "last_durable_commit_age_s": 500.0, "durable_capture": "critical"})
    assert sup.acquisition_progressing()[0]
