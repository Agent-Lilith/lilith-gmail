# Lilith Email System

Gmail sync daemon + Lilith agent tools for semantic email search, with privacy-aware classification and PII sanitization.

## Quick Start

### 1. Database (shared Postgres)

This project uses a **shared** PostgreSQL server. Database name for this app: `lilith_emails`.

Ensure the shared Postgres (with pgvector) is running. Clone the lilith-compose project first.

### 2. Run migrations

```bash
uv run alembic upgrade head
```

### 3. Add a Gmail account

Download OAuth client secrets from [Google Cloud Console](https://console.cloud.google.com/apis/credentials), then:

```bash
uv run python main.py add-account path/to/client_secrets.json
```

### 4. Sync (download only)

```bash
uv run python main.py sync 1
```

Logs show progress: pages fetched, messages stored, total so far.

### 5. Transform (classify + sanitize + embed)

Run after sync to generate `privacy_tier`, `body_redacted`, and multi-level embeddings (subject, body or chunks) from stored data. Re-run anytime you change models or logic (no re-download).

```bash
uv run python main.py transform 1
```

Clean all derived columns added by transform command.
```bash
uv run python main.py reset-transform 1
```

### 6. Run the sync daemon (Pub/Sub webhook)

```bash
uv run python main.py serve
```

Incremental sync (webhook) only downloads new/changed mail; run `transform` separately to process new messages.

## Configuration

Environment variables (`.env` or shell):

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `EMAIL_ENCRYPTION_KEY` | Fernet key for OAuth token encryption (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |
| `GOOGLE_CLOUD_PROJECT` | GCP project ID for Pub/Sub |
| `EMBEDDING_URL` | TEI embedding service (default `http://127.0.0.1:6003`); must expose `/embed` and `/tokenize` |
| `SPACY_API_URL` | Spacy API for NER/PII sanitization (default `http://127.0.0.1:6004`) |
| `FASTTEXT_LANGDETECT_URL` | fastText language detection API (default `http://127.0.0.1:6005`); used for NER language before sanitizing PERSONAL emails |
| `VLLM_URL` | vLLM OpenAI-compatible API (default `http://127.0.0.1:6001/v1`) |
| `VLLM_MODEL` | Model id for chat completions when not in capabilities (default `Qwen3-8B-AWQ`) |

Transform uses `capabilities.json`: run `uv run python main.py capabilities` before transform so the file exists and has `embedding.max_tokens`, `vllm.model_id`, `spacy_api.available`, and `fasttext_langdetect.available`. No env fallback for transform. Emails with `transform_completed_at` set are skipped unless you use `--force` (which prompts for confirmation); if transform fails mid-run, those emails are retried next time.

## MCP Server (Agent Tools)

The Lilith Email MCP server exposes your transformed Gmail.

```bash
uv run mcp
uv run mcp --transport streamable-http --port 6201
```

### MCP Tools

| Tool | Description |
|------|-------------|
| `emails_search` | Search by natural language + optional filters (from_email, labels, has_attachments, date_after, date_before, limit). Returns list of email dicts. |
| `email_get` | Fetch one email by Gmail message ID. Returns email dict or error. |
| `email_get_thread` | Fetch all messages in a thread by thread_id. Returns thread dict with `messages` list. |
| `emails_summarize` | Summarize by `thread_id` or `email_ids`. Returns a short summary string. |

All responses use **external** privacy: SENSITIVE content is redacted, PERSONAL content is shown sanitized.
