-- Device-reported measurement chain (contract/device-reported-provenance.md): profile and
-- calibration records built locally, registered with POST /api/v1/device/provenance before any
-- measurement or event that references them is sent.
CREATE TABLE provenance_records (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('measurement_profile', 'calibration')),
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    registered_at TEXT,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'registered', 'rejected')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_status INTEGER,
    last_error TEXT
);
CREATE INDEX provenance_records_state ON provenance_records(state, next_attempt_at);
