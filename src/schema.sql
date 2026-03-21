CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id        TEXT NOT NULL,
    stream_position  BIGINT NOT NULL,
    global_position  BIGINT GENERATED ALWAYS AS IDENTITY,
    event_type       TEXT NOT NULL,
    event_version    SMALLINT NOT NULL DEFAULT 1,
    payload          JSONB NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

CREATE INDEX IF NOT EXISTS idx_events_stream
    ON events (stream_id, stream_position);

CREATE INDEX IF NOT EXISTS idx_events_global
    ON events (global_position);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON events (event_type);

CREATE INDEX IF NOT EXISTS idx_events_recorded
    ON events (recorded_at);

CREATE TABLE IF NOT EXISTS event_streams (
    stream_id        TEXT PRIMARY KEY,
    aggregate_type   TEXT NOT NULL,
    current_version  BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at      TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_streams_type
    ON event_streams (aggregate_type);

CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name  TEXT PRIMARY KEY,
    last_position    BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS outbox (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID NOT NULL REFERENCES events(event_id),
    destination      TEXT NOT NULL,
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at     TIMESTAMPTZ,
    attempts         SMALLINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox (created_at)
    WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id        TEXT NOT NULL REFERENCES event_streams(stream_id),
    stream_position  BIGINT NOT NULL,
    aggregate_type   TEXT NOT NULL,
    snapshot_version INT NOT NULL,
    state            JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS application_summary (
    application_id            TEXT PRIMARY KEY,
    state                     TEXT NOT NULL,
    applicant_id              TEXT,
    requested_amount_usd      NUMERIC(18, 2),
    approved_amount_usd       NUMERIC(18, 2),
    risk_tier                 TEXT,
    fraud_score               DOUBLE PRECISION,
    compliance_status         TEXT,
    decision                  TEXT,
    agent_sessions_completed  JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_event_type           TEXT,
    last_event_at             TIMESTAMPTZ,
    human_reviewer_id         TEXT,
    final_decision_at         TIMESTAMPTZ,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_application_summary_state
    ON application_summary (state);

CREATE INDEX IF NOT EXISTS idx_application_summary_decision
    ON application_summary (decision);

CREATE TABLE IF NOT EXISTS agent_performance_ledger (
    agent_type             TEXT NOT NULL,
    agent_id               TEXT NOT NULL,
    model_version          TEXT NOT NULL,
    total_sessions         INTEGER NOT NULL DEFAULT 0,
    analyses_completed     INTEGER NOT NULL DEFAULT 0,
    decisions_generated    INTEGER NOT NULL DEFAULT 0,
    avg_confidence_score   DOUBLE PRECISION,
    avg_duration_ms        DOUBLE PRECISION,
    approve_count          INTEGER NOT NULL DEFAULT 0,
    decline_count          INTEGER NOT NULL DEFAULT 0,
    refer_count            INTEGER NOT NULL DEFAULT 0,
    human_override_count   INTEGER NOT NULL DEFAULT 0,
    approve_rate           DOUBLE PRECISION NOT NULL DEFAULT 0,
    decline_rate           DOUBLE PRECISION NOT NULL DEFAULT 0,
    refer_rate             DOUBLE PRECISION NOT NULL DEFAULT 0,
    human_override_rate    DOUBLE PRECISION NOT NULL DEFAULT 0,
    confidence_samples     INTEGER NOT NULL DEFAULT 0,
    duration_samples       INTEGER NOT NULL DEFAULT 0,
    first_seen_at          TIMESTAMPTZ,
    last_seen_at           TIMESTAMPTZ,
    last_updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_type, agent_id, model_version)
);

CREATE TABLE IF NOT EXISTS agent_session_projection_index (
    session_id       TEXT PRIMARY KEY,
    application_id   TEXT NOT NULL,
    agent_type       TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    stream_id        TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_session_projection_application
    ON agent_session_projection_index (application_id);

CREATE TABLE IF NOT EXISTS application_decision_projection_index (
    application_id   TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    agent_type       TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS compliance_audit_current (
    application_id          TEXT PRIMARY KEY,
    session_id              TEXT,
    regulation_set_version  TEXT,
    required_rules          JSONB NOT NULL DEFAULT '[]'::jsonb,
    passed_rules            JSONB NOT NULL DEFAULT '[]'::jsonb,
    failed_rules            JSONB NOT NULL DEFAULT '[]'::jsonb,
    noted_rules             JSONB NOT NULL DEFAULT '[]'::jsonb,
    hard_block_rules        JSONB NOT NULL DEFAULT '[]'::jsonb,
    completed               BOOLEAN NOT NULL DEFAULT FALSE,
    overall_verdict         TEXT,
    last_event_type         TEXT,
    as_of                   TIMESTAMPTZ,
    checkpoint_position     BIGINT NOT NULL DEFAULT 0,
    snapshot_version        BIGINT NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS compliance_audit_history (
    application_id    TEXT NOT NULL,
    snapshot_version  BIGINT NOT NULL,
    as_of             TIMESTAMPTZ NOT NULL,
    global_position   BIGINT NOT NULL,
    event_type        TEXT NOT NULL,
    state             JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (application_id, snapshot_version)
);

CREATE INDEX IF NOT EXISTS idx_compliance_audit_history_lookup
    ON compliance_audit_history (application_id, as_of DESC);

CREATE INDEX IF NOT EXISTS idx_compliance_audit_history_position
    ON compliance_audit_history (global_position);
