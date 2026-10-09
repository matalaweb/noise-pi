-- Initial collector schema. Immutable capture facts are never updated in place; only delivery
-- and file-state columns change.

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE acquisition_sessions (
    session_id TEXT PRIMARY KEY,               -- wire boot_id (acquisition-session UUID)
    channel TEXT NOT NULL,
    stream_id TEXT NOT NULL,                   -- capture stream instance (sample index origin)
    timing_epoch INTEGER NOT NULL,
    start_sample INTEGER NOT NULL,
    end_sample INTEGER,
    started_mono REAL NOT NULL,
    started_utc TEXT,
    ended_utc TEXT,
    end_reason TEXT,
    os_boot_id TEXT,
    agent_version TEXT NOT NULL,
    microphone_json TEXT NOT NULL,
    format_json TEXT NOT NULL,
    gain_json TEXT NOT NULL,
    timing_json TEXT,
    profile_id TEXT,
    configuration_revision INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE profiles (
    profile_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    document_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE profile_assets (
    sha256 TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE configurations (
    revision INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL,
    document_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('staged', 'applied', 'rejected', 'superseded')),
    received_at TEXT NOT NULL,
    applied_at TEXT,
    reason_code TEXT,
    detail TEXT
);

CREATE TABLE measurements (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES acquisition_sessions(session_id),
    sequence INTEGER,
    channel TEXT NOT NULL,
    utc_second INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete', 'omitted')),
    omit_reason TEXT,
    first_sample INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    timing_trusted INTEGER NOT NULL,
    timing_note TEXT,
    wire_json TEXT,
    diagnostics_json TEXT NOT NULL,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN
        ('local_only', 'pending', 'batched', 'acknowledged', 'quarantined', 'expired_for_automatic_upload')),
    batch_id TEXT REFERENCES outbox_batches(batch_id),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);
CREATE INDEX measurements_delivery ON measurements(delivery_state, utc_second);
CREATE INDEX measurements_batch ON measurements(batch_id);
CREATE INDEX measurements_second ON measurements(channel, utc_second);

CREATE TABLE gaps (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    channel TEXT NOT NULL,
    start_utc TEXT,
    end_utc TEXT,
    start_mono REAL,
    end_mono REAL,
    start_sample INTEGER,
    end_sample INTEGER,
    cause TEXT NOT NULL,
    clock_quality TEXT,
    detail_json TEXT,
    report_state TEXT NOT NULL DEFAULT 'local_only',
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES acquisition_sessions(session_id),
    channel TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'complete', 'incomplete', 'interrupted')),
    start_second INTEGER NOT NULL,
    timing_trusted INTEGER NOT NULL,
    latest_revision INTEGER NOT NULL,
    termination_reason TEXT,
    last_observed_at TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL,
    finalized_at TEXT
);

CREATE TABLE event_revisions (
    event_id TEXT NOT NULL REFERENCES events(event_id),
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN
        ('local_only', 'pending', 'sending', 'acknowledged', 'quarantined', 'expired_for_automatic_upload')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_expires_at REAL,
    last_status INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    PRIMARY KEY (event_id, revision)
);

CREATE TABLE recordings (
    recording_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    segment_number INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    start_sample INTEGER NOT NULL,
    end_sample INTEGER,
    sample_count INTEGER,
    capture_started_at TEXT NOT NULL,
    duration_ms INTEGER,
    format_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'finalizing', 'finalized', 'failed')),
    incomplete INTEGER NOT NULL DEFAULT 0,
    close_reason TEXT,
    spool_dir TEXT NOT NULL,
    path TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN
        ('local_only', 'pending_declaration', 'awaiting_upload', 'uploading', 'awaiting_verification',
         'verified', 'failed', 'quarantined', 'expired_for_automatic_upload', 'server_purged')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_expires_at REAL,
    last_error TEXT,
    verified_at TEXT,
    verified_sha256 TEXT,
    server_json TEXT,
    local_deleted_at TEXT,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    UNIQUE (event_id, segment_number)
);
CREATE INDEX recordings_delivery ON recordings(delivery_state);

CREATE TABLE audio_chunks (
    recording_id TEXT NOT NULL REFERENCES recordings(recording_id),
    chunk_index INTEGER NOT NULL,
    path TEXT NOT NULL,
    start_sample INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('closed', 'superseded', 'deleted')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (recording_id, chunk_index)
);

CREATE TABLE outbox_batches (
    batch_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,                     -- exact serialized request body, never rewritten
    record_count INTEGER NOT NULL,
    byte_size INTEGER NOT NULL,
    first_second INTEGER NOT NULL,
    last_second INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('pending', 'sending', 'acknowledged', 'quarantined', 'expired_for_automatic_upload', 'superseded')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_expires_at REAL,
    last_status INTEGER,
    last_error TEXT,
    response_json TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    replaces_batch_id TEXT
);
CREATE INDEX outbox_state ON outbox_batches(state, first_second);

CREATE TABLE upload_attempts (
    attempt_id TEXT PRIMARY KEY,
    recording_id TEXT NOT NULL REFERENCES recordings(recording_id),
    storage_host TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('issued', 'uploading', 'uploaded', 'completion_sent', 'completed', 'expired', 'failed')),
    put_status INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE config_acknowledgments (
    ack_id TEXT PRIMARY KEY,                   -- local identity only (the wire ack has none)
    revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'rejected')),
    payload_json TEXT NOT NULL,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('pending', 'acknowledged', 'quarantined')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (revision, status)
);

CREATE TABLE health_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE deletion_receipts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    reference TEXT NOT NULL,
    sha256 TEXT,
    detail_json TEXT,
    deleted_at TEXT NOT NULL
);
