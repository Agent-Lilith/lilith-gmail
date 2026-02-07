# core — Shared package

Code used by **sync**, **transform**, **daemon**, and **mcp_server** (email tools) lives here. This keeps a single place for database access, models, embeddings, and privacy semantics.

## Contents

- **config** — Minimal settings: `DATABASE_URL`, `EMBEDDING_URL`, service URLs, etc.
- **database** — Session factory, engine, `db_session()`, `get_db()`.
- **models** — SQLAlchemy models: Email, EmailAccount, EmailChunk, EmailThread, etc.
- **embeddings** — Embedder (TEI client for query encoding and batch embed).
- **privacy** — `PrivacyTier` constants (SENSITIVE, PERSONAL, PUBLIC) used when formatting email content.
- **capabilities** — Probe embedding/vLLM/Spacy/FastText services and write `capabilities.json` (used by CLI and transform).
- **capabilities_loader** — Load and validate `capabilities.json`; expose max_tokens, max_chars, vLLM model_id, etc.
- **email_utils** — Email parsing: `parse_email_address`, `parse_email_list`, `parse_date` (RFC 5322 headers).

## Who uses it

- **sync** — Imports from `core` for DB, models. Gmail fetch and store only.
- **transform** — Imports from `core` for DB, models, embeddings, `PrivacyTier`, preprocessing. Classify, sanitize, embed.
- **daemon** — Imports from `core` and **sync**. Webhook + incremental sync.
- **mcp_server** — Imports only from `core` (and MCP SDK). No dependency on sync/transform/daemon.

## Rules

- **core** must not import from `sync`, `transform`, `daemon`, or `mcp_server`.
- Keep dependencies minimal (sqlalchemy, pgvector, httpx, pydantic, etc.). No FastAPI, MCP, or Gmail here.

