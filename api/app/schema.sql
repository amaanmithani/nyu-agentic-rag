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
