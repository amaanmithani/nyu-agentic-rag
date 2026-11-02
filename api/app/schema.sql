-- Applied on every API start; every statement is idempotent.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,               -- slug, e.g. "pag-2018-2nd-ed"; used in citations
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,           -- dedupe: same bytes are never ingested twice
    page_count  INT NOT NULL DEFAULT 0,
    chunk_count INT NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'processing',  -- processing | ready | failed
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Full page text, so read_document_section returns exact pages (not reassembled chunks).
CREATE TABLE IF NOT EXISTS pages (
    doc_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page    INT NOT NULL,                       -- 1-based PDF page index
    text    TEXT NOT NULL,
    PRIMARY KEY (doc_id, page)
);

CREATE TABLE IF NOT EXISTS chunks (
    id         BIGSERIAL PRIMARY KEY,
    doc_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page       INT NOT NULL,
    chunk_idx  INT NOT NULL,                    -- position within the page
    text       TEXT NOT NULL,
    embedding  vector(384) NOT NULL,            -- bge-small-en-v1.5, normalized
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    UNIQUE (doc_id, page, chunk_idx)
);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks (doc_id);

-- Single-row counter bumped on every corpus change; part of the retrieval cache key,
-- so stale cached results become unreachable the moment a document is added or removed.
CREATE TABLE IF NOT EXISTS corpus_state (
    id       BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    version  BIGINT NOT NULL DEFAULT 0
);
INSERT INTO corpus_state (id, version) VALUES (TRUE, 0) ON CONFLICT DO NOTHING;

-- Durable copy of session state. Redis (session:{id}) is the fast path; every write goes to
-- both, so a Redis outage or eviction loses nothing.
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,               -- random token; knowing it is the only access
    state       JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per question (agent or baseline): what the eval and the UI history read.
CREATE TABLE IF NOT EXISTS runs (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    mode            TEXT NOT NULL,              -- agent | baseline
    question        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',  -- running|answered|clarification|partial|error
    stop_reason     TEXT,
    answer          TEXT,
    citation_report JSONB,
    steps           INT,
    tool_calls      INT,
    tokens_in       INT,                        -- uncached input tokens
    tokens_out      INT,
    tokens_cache_read  INT,
    tokens_cache_write INT,
    cost_usd        NUMERIC(10, 6),
    latency_ms      INT,
    model           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS runs_session ON runs (session_id, created_at);

-- One row per trace event (LLM call, tool call, tool result, limit, ...).
CREATE TABLE IF NOT EXISTS trace_events (
    id       BIGSERIAL PRIMARY KEY,
    run_id   BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq      INT NOT NULL,
    type     TEXT NOT NULL,
    step     TEXT,
    tool     TEXT,
    status   TEXT,
    ms       INT,
    payload  JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);
