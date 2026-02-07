# Lilith Email System

Gmail sync daemon + Lilith agent tools for semantic email search, with privacy-aware classification and PII sanitization.

## Project layout

- **Single configuration**: All env vars live in `core.config` (one `Settings` class, one `.env`). Used by sync, transform, daemon, and MCP.
- **src layout**: Packages live under `src/` (recommended Python layout: `src/core`, `src/sync`, `src/transform`, `src/daemon`, `src/mcp_server`). The CLI and `uv run` use the installed packages.
- **MCP**: The agent MCP server runs with `uv run mcp` or `uv run python -m mcp_server` (package `mcp_server` to avoid shadowing the PyPI `mcp` SDK).

## Architecture

- **Two-phase workflow**: **Sync** downloads raw email from Gmail and stores it once. **Transform** (separate step) runs classification, sanitization, and embedding on stored data. You can re-run transform after changing models or logic without re-downloading.
- **PostgreSQL + pgvector**: Unified relational + vector storage
- **Gmail API + Pub/Sub**: Real-time push notifications (sync phase only)
- **Privacy**: 3-tier (SENSITIVE / PERSONAL / PUBLIC) with PII sanitization (transform phase). Preprocessing also strips tracking pixels from HTML, replaces tracking URLs with [LINK], and removes invisible Unicode.
- **Embeddings**: Local TEI container (nomic-embed-text-v1.5, 768d, 8192 token context) at `EMBEDDING_URL`; uses `/embed` and `/tokenize`
- **Privacy classification**: Local vLLM (e.g. Qwen3-8B-AWQ) at `VLLM_URL`. The prompt includes **header hints** to improve accuracy with minimal tokens: **has_attachments** (often signals formal/serious mail) and **label names** (e.g. INBOX, SENT, Work). Labels are stored per account (`account_labels`) and resolved to names for both the classifier and MCP. Other signals you could add later: **thread size** (long threads → discussion), **recipient count** (many To/CC → bulk), **List-* / Auto-Submitted** headers (newsletters), **is_reply** (in-reply-to present).

## Quick Start

### 1. Start database

```bash
docker compose up -d db
```

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
uv run python main.py sync 1          # Download up to 1000 messages (10 concurrent by default)
uv run python main.py sync 1 --limit 5000 --concurrency 5   # Lower concurrency if you see 403
```

Logs show progress: pages fetched, messages stored, total so far.

### 5. Transform (classify + sanitize + embed)

Run after sync to generate `privacy_tier`, `body_redacted`, and multi-level embeddings (subject, body or chunks) from stored data. Re-run anytime you change models or logic (no re-download).

When stdout is a TTY, a **TUI** (terminal UI) is shown: live progress bar, stats (processed/failed, by tier, body full/chunked), and recent warnings/errors. For 60k+ emails this keeps the terminal readable. Use `--no-tui` for plain log output (e.g. when piping or in CI).

```bash
uv run python main.py transform 1     # Account 1, only emails not yet completed
uv run python main.py transform       # All accounts
uv run python main.py transform 1 --force   # Recompute (overwrite) already-transformed; prompts for confirmation
uv run python main.py transform 1 --force -y   # Same, non-interactive (e.g. CI)
```

**Recompute (--force)**: Re-runs transform on emails that already have derived data (overwrites privacy_tier, embeddings, chunks). The CLI **prompts for confirmation** before running unless you pass **`-y`/`--yes`**.

#### Transform Batching and Error Handling

The transform pipeline batches work to be efficient. This is important to understand if you are using a resource-limited TEI server, which might cause `413 Payload Too Large` errors.

- **Pipeline Batch Size**: This is the number of emails processed in one go. You can control this with the `--batch-size` CLI argument (default is 50).
  - `transform --batch-size 10`: Use smaller batches if you see errors.
  - `transform --batch-size 1`: The safest option, processing one email at a time.
- **Embed Sub-batch Size**: Within a pipeline batch, texts are sent to the TEI server in sub-batches. This is set to `1` in the code, meaning one TEI `/embed` request per text. This avoids most 413 errors.
- **Automatic Error Handling**:
  - If a multi-text request fails with a 413 error, the system automatically retries one text at a time.
  - If a single large text fails, it's truncated and retried once.

This ensures that emails are processed reliably even with strict server limits.

### 6. Run the sync daemon (Pub/Sub webhook)

```bash
uv run python main.py serve
# or
uv run uvicorn daemon.app:app --reload  # from project root; packages live in src/
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

Transform **always uses** `capabilities.json`: run `uv run python main.py capabilities` before transform so the file exists and has `embedding.max_tokens`, `vllm.model_id`, `spacy_api.available`, and `fasttext_langdetect.available`. No env fallback for transform. Emails with `transform_completed_at` set are skipped unless you use `--force` (which prompts for confirmation); if transform fails mid-run, those emails are retried next time.

## MCP Server (Agent Tools)

The Lilith Email MCP server exposes your synced and transformed Gmail as **MCP tools** so any MCP client (e.g. Cursor, Claude Desktop, or your own agent) can search, read, and summarize emails over the Model Context Protocol.

### Prerequisites

- **PostgreSQL** running and migrated (`uv run alembic upgrade head`).
- **TEI** embedding service at `EMBEDDING_URL` (default `http://127.0.0.1:6003`) so semantic search works.
- At least one account **synced** and **transformed** (so there are emails with embeddings and privacy tiers).

### Run the Server

**Stdio (default)** — for Cursor, Claude Desktop, and other clients that spawn the server as a subprocess:

```bash
uv run mcp
```

**Streamable HTTP** — for remote clients or the MCP Inspector:

```bash
uv run mcp --transport streamable-http --port 6201
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string (required) |
| `EMBEDDING_URL` | TEI embedding service URL (default `http://127.0.0.1:6003`) |
| `MCP_EMAIL_ACCOUNT_ID` | Optional. If set, restrict search/get/thread to this account id. |

### Tools

| Tool | Description |
|------|-------------|
| `search_emails` | Search by natural language + optional filters (from_email, labels, has_attachments, date_after, date_before, limit). Returns list of email dicts. |
| `get_email` | Fetch one email by Gmail message ID. Returns email dict or error. |
| `get_email_thread` | Fetch all messages in a thread by thread_id. Returns thread dict with `messages` list. |
| `summarize_emails` | Summarize by `thread_id` or `email_ids`. Returns a short summary string. |

All responses use **external** privacy: SENSITIVE content is redacted, PERSONAL content is shown sanitized.

### Connect from Cursor

1. Open Cursor settings (e.g. **Cursor → Settings → MCP** or `.cursor/mcp.json`).
2. Add a server that runs the Lilith Email MCP server over stdio:

```json
{
  "mcpServers": {
    "lilith-email": {
      "command": "uv",
      "args": ["run", "mcp"],
      "cwd": "/path/to/lilith-gmail",
      "env": {}
    }
  }
}
```

Use the real path to your `lilith-gmail` project for `cwd`.

### Connect from Claude Desktop

Configure Claude Desktop to use an MCP server with the same command:

- **Command:** `uv` (or full path to `uv`)
- **Args:** `run`, `mcp`
- **Cwd:** project directory where `.env` and `pyproject.toml` live.

<details>
<summary>Testing the MCP Server with the Inspector</summary>

Use this guide to test the Lilith Email MCP server with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

#### 1. Start the MCP server (Streamable HTTP)

```bash
uv run mcp --transport streamable-http --port 6201
```

#### 2. Open the MCP Inspector

- **Web**: Go to [inspector.modelcontextprotocol.io](https://inspector.modelcontextprotocol.io/).
- **Local**: Run it locally with `npx @modelcontextprotocol/inspector`.

#### 3. Connect to the server

1. In the Inspector, choose **Streamable HTTP**.
2. **URL**: Use `http://localhost:6201` or `http://localhost:6201/mcp`.
3. Click **Connect**. The Inspector should show “Connected” and list the server’s tools.

#### 4. Test the tools

- **`search_emails`**:
  - **query**: e.g. `invoice` or `meeting next week`.
  - **limit**: e.g. `5`.
  - **Expected**: A list of email objects.
- **`get_email`**:
  - Pick a `gmail_id` from a search result.
  - **Expected**: A single email object.
- **`get_email_thread`**:
  - Pick a `gmail_thread_id` from a search result.
  - **Expected**: A thread object with a `messages` array.
- **`summarize_emails`**:
  - Use a `thread_id` or a list of `email_ids`.
  - **Expected**: A short text summary.

</details>

## Docker

```bash
docker compose up -d
```

Runs PostgreSQL and the sync daemon. Ensure `EMAIL_ENCRYPTION_KEY` and `GOOGLE_CLOUD_PROJECT` are set for the daemon.

## Troubleshooting

### Transform and vLLM 404

If you see **404** for `POST .../v1/chat/completions` during transform:

1. Run `uv run python main.py capabilities` to write the correct model ID to `capabilities.json`.
2. Ensure vLLM is running and a model is loaded.

### 403 Forbidden during sync

Sync needs **read access to email bodies**. If you see 403:

1.  **Add the right scope to the OAuth consent screen**: Add `https://www.googleapis.com/auth/gmail.readonly` to your GCP project's OAuth consent screen.
2.  **Add yourself as a test user** if your app is in "Testing" mode.
3.  **Get a new token** by re-running the `add-account` command.
4.  **Other checks**: Ensure the Gmail API is enabled and consider reducing sync concurrency (`--concurrency 5`).

## Pub/Sub Setup

1. Enable Gmail and Pub/Sub APIs in GCP.
2. Create a Pub/Sub topic (e.g., `gcloud pubsub topics create gmail-notifications`).
3. Create a push subscription pointing to your webhook URL.
4. The application will call `gmail.users().watch()` for each account to set up notifications.