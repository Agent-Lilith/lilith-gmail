#!/usr/bin/env python3
import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("sync").setLevel(logging.INFO)
logging.getLogger("transform").setLevel(logging.INFO)

from core.database import db_session
from core.models import EmailAccount, Email, EmailChunk
from sqlalchemy import delete, func, select, update
from sync.oauth_helpers import run_local_oauth, token_from_credentials
from core.config import settings
from sync.gmail_client import GmailClient
from sync.sync_workers import SyncWorker
from transform.privacy import PrivacyManager
from core.embeddings import Embedder
from transform.pipeline import TransformPipeline


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run(
        "daemon.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def cmd_add_account(args: argparse.Namespace) -> int:
    creds = run_local_oauth(args.secrets, args.token_output)
    from googleapiclient.discovery import build
    from sqlalchemy import select

    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    email_address = profile.get("emailAddress", "unknown")

    encrypted = token_from_credentials(creds)
    with db_session() as db:
        existing = db.execute(
            select(EmailAccount).where(EmailAccount.email_address == email_address)
        ).scalar_one_or_none()
        if existing:
            existing.oauth_token_encrypted = encrypted
            db.commit()
            print(f"Updated token for account: {email_address} (id={existing.id})")
        else:
            account = EmailAccount(
                email_address=email_address,
                oauth_token_encrypted=encrypted,
            )
            db.add(account)
            db.commit()
            print(f"Added account: {email_address} (id={account.id})")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from sync.oauth_helpers import credentials_from_token, ensure_valid_credentials

    with db_session() as db:
        account = db.get(EmailAccount, args.account_id)
        if not account:
            print(f"Account {args.account_id} not found", file=sys.stderr)
            return 1

        creds = credentials_from_token(account.oauth_token_encrypted)
        creds = ensure_valid_credentials(creds)
        gmail = GmailClient(creds)
        worker = SyncWorker(gmail, db)
        limit = None if args.limit == 0 else (args.limit if args.limit is not None else 1000)
        asyncio.run(
            worker.full_sync(
                account.id,
                limit=limit,
                concurrency=args.concurrency,
            )
        )
        print(f"Sync completed for {account.email_address}. Run 'transform {args.account_id}' to classify and embed.")
    return 0


def _require_confirm(message: str, yes_flag: bool) -> bool:
    if yes_flag:
        return True
    print(message, file=sys.stderr)
    try:
        answer = input("Type 'yes' to continue: ").strip().lower()
    except EOFError:
        answer = ""
    if answer in ("yes", "y"):
        return True
    print("Aborted.", file=sys.stderr)
    return False


def _transform_log_file_handler():
    from datetime import datetime
    from pathlib import Path

    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    name = f"transform_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    path = log_dir / name
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    return handler, path


def cmd_transform(args: argparse.Namespace) -> int:
    from sqlalchemy import select, func

    email_id = getattr(args, "email_id", None)

    if args.force and email_id is None:
        with db_session() as db:
            stmt = (
                select(func.count())
                .select_from(Email)
                .where(Email.deleted_at.is_(None))
                .where(Email.body_text.isnot(None))
            )
            if email_id is not None:
                stmt = stmt.where(Email.id == email_id)
            if args.account_id is not None:
                stmt = stmt.where(Email.account_id == args.account_id)
            if args.limit is not None:
                sub = (
                    select(Email.id)
                    .where(Email.deleted_at.is_(None))
                    .where(Email.body_text.isnot(None))
                )
                if email_id is not None:
                    sub = sub.where(Email.id == email_id)
                if args.account_id is not None:
                    sub = sub.where(Email.account_id == args.account_id)
                sub = sub.order_by(Email.id).limit(args.limit)
                count = db.execute(select(func.count()).select_from(sub.subquery())).scalar_one() or 0
            else:
                count = db.execute(stmt).scalar_one() or 0
        scope = f"account {args.account_id}" if args.account_id is not None else "all accounts"
        limit_s = f" (limit {args.limit})" if args.limit is not None else ""
        if not _require_confirm(
            f"Recompute will overwrite derived data (privacy_tier, embeddings, chunks) for {count} emails in {scope}{limit_s}. This cannot be undone.",
            args.yes,
        ):
            return 1

    with db_session() as db:
        stmt = (
            select(func.count())
            .select_from(Email)
            .where(Email.deleted_at.is_(None))
            .where(Email.body_text.isnot(None))
        )
        if email_id is not None:
            stmt = stmt.where(Email.id == email_id)
        if args.account_id is not None:
            stmt = stmt.where(Email.account_id == args.account_id)
        if not args.force and email_id is None:
            stmt = stmt.where(Email.transform_completed_at.is_(None))
        if args.limit is not None and email_id is None:
            sub = (
                select(Email.id)
                .where(Email.deleted_at.is_(None))
                .where(Email.body_text.isnot(None))
            )
            if email_id is not None:
                sub = sub.where(Email.id == email_id)
            if args.account_id is not None:
                sub = sub.where(Email.account_id == args.account_id)
            if not args.force:
                sub = sub.where(Email.transform_completed_at.is_(None))
            sub = sub.order_by(Email.id).limit(args.limit)
            total = db.execute(select(func.count()).select_from(sub.subquery())).scalar_one() or 0
        else:
            total = db.execute(stmt).scalar_one() or 0

    if total == 0:
        if email_id is not None:
            print(f"No email found with id={email_id} or it has no body_text.", file=sys.stderr)
        else:
            print("No emails to transform.", file=sys.stderr)
            print("Use --force to recompute already-transformed emails.", file=sys.stderr)
        return 0

    print(f"Emails to transform: {total:,}", file=sys.stderr)

    file_handler, log_path = _transform_log_file_handler()
    transform_logger = logging.getLogger("transform")
    transform_logger.addHandler(file_handler)
    try:
        use_tui = not getattr(args, "no_tui", False) and sys.stdout.isatty()
        if use_tui:
            from transform.tui import run_transform_with_tui

            def run_pipeline(progress_callback):
                with db_session() as db2:
                    pipeline = TransformPipeline(db2, PrivacyManager(), Embedder())
                    return pipeline.run(
                        account_id=args.account_id,
                        email_id=email_id,
                        force=args.force or (email_id is not None),
                        batch_size=args.batch_size,
                        limit=args.limit,
                        progress_callback=progress_callback,
                    )

            n = run_transform_with_tui(run_pipeline, total)
        else:
            with db_session() as db:
                pipeline = TransformPipeline(db, PrivacyManager(), Embedder())
                n = pipeline.run(
                    account_id=args.account_id,
                    email_id=email_id,
                    force=args.force or (email_id is not None),
                    batch_size=args.batch_size,
                    limit=args.limit,
                )
        print(f"Transformed {n} emails.")
        print(f"Log: {log_path}", file=sys.stderr)
        return 0
    finally:
        transform_logger.removeHandler(file_handler)
        file_handler.close()


def cmd_get_email(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    email_id = args.email_id.strip()
    with db_session() as db:
        if email_id.isdigit():
            email = db.get(Email, int(email_id))
        else:
            row = db.execute(
                select(Email).where(Email.gmail_id == email_id).where(Email.deleted_at.is_(None))
            ).scalars().one_or_none()
            email = row
        if not email:
            print(f"No email found for id={args.email_id!r}", file=sys.stderr)
            return 1
        tier_names = {1: "SENSITIVE", 2: "PERSONAL", 3: "PUBLIC"}
        tier = tier_names.get(email.privacy_tier, str(email.privacy_tier))
        body = (email.body_text if getattr(args, "raw", False) else (email.body_redacted or email.body_text)) or ""
        if not args.full and len(body) > 2000:
            body = body[:2000] + "\n... [truncated, use --full for full body]"
        print(f"id:           {email.id}")
        print(f"gmail_id:     {email.gmail_id}")
        print(f"thread_id:    {email.gmail_thread_id}")
        print(f"account_id:   {email.account_id}")
        print(f"subject:      {email.subject or ''}")
        print(f"from:         {email.from_name or ''} <{email.from_email}>")
        print(f"to:           {email.to_emails or []}")
        print(f"sent_at:      {email.sent_at}")
        print(f"privacy_tier: {tier}")
        print(f"snippet:      {(email.snippet or '')[:200]}")
        if not getattr(args, "raw", False) and (email.body_redacted or "").strip():
            print("--- body (redacted; use --raw for original) ---")
        else:
            print("--- body ---")
        print(body or "(no body)")
    return 0


def cmd_check_account(args: argparse.Namespace) -> int:
    from sync.oauth_helpers import credentials_from_token, ensure_valid_credentials

    with db_session() as db:
        account = db.get(EmailAccount, args.account_id)
        if not account:
            print(f"Account {args.account_id} not found", file=sys.stderr)
            return 1
        creds = credentials_from_token(account.oauth_token_encrypted)
        creds = ensure_valid_credentials(creds)
        scopes = list(creds.scopes or [])
        print(f"Account {args.account_id}: {account.email_address}")
        print("Token scopes:")
        for s in scopes:
            has_readonly = "gmail.readonly" in s or "mail.google.com" in s
            mark = "  <- need this for sync" if has_readonly else ""
            print(f"  {s}{mark}")
        if not any("readonly" in s or "mail.google.com" in s for s in scopes):
            print("\nWARNING: No gmail.readonly scope. Run add-account again after adding scope in OAuth consent screen.")
    return 0


def cmd_prepare_debug(args: argparse.Namespace) -> int:
    import asyncio
    from transform.pipeline import TransformPipeline
    from transform.privacy import PrivacyManager
    from core.embeddings import Embedder

    email_id = args.email_id.strip()
    with db_session() as db:
        if email_id.isdigit():
            email = db.get(Email, int(email_id))
        else:
            from sqlalchemy import select
            email = db.execute(
                select(Email).where(Email.gmail_id == email_id).where(Email.deleted_at.is_(None))
            ).scalars().one_or_none()
        if not email:
            print(f"No email found for id={email_id!r}", file=sys.stderr)
            return 1
        pipeline = TransformPipeline(db, PrivacyManager(), Embedder())
        try:
            payload = asyncio.run(pipeline._prepare_one(email))
            print(f"OK: email id={email.id} prepared successfully.")
            print(f"  privacy_tier={payload.privacy_tier}, body_type={payload.body_type}, subject_text len={len(payload.subject_text)}, body_text len={len(payload.body_text or '') or 0}, chunks={len(payload.chunks)}")
            return 0
        except Exception as e:
            import traceback
            print(f"Prepare failed for email id={email.id}: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()
            return 1


def cmd_debug_fetch(args: argparse.Namespace) -> int:
    from sync.oauth_helpers import credentials_from_token, ensure_valid_credentials

    with db_session() as db:
        account = db.get(EmailAccount, args.account_id)
        if not account:
            print(f"Account {args.account_id} not found", file=sys.stderr)
            return 1
        creds = credentials_from_token(account.oauth_token_encrypted)
        creds = ensure_valid_credentials(creds)
        gmail = GmailClient(creds)

        print("1. Listing 1 message...")
        resp = gmail.list_messages(max_results=1)
        messages = resp.get("messages", [])
        if not messages:
            print("No messages in mailbox.")
            return 0
        msg_id = messages[0]["id"]
        print(f"   Got message id: {msg_id}")

        print("2. Fetching message (format=full)...")
        try:
            msg = gmail.get_message(msg_id, format="full")
            print(f"   OK: subject={msg.get('snippet', '')[:50]}...")
        except Exception as e:
            import json
            print(f"   ERROR: {type(e).__name__}: {e}")
            if hasattr(e, "resp"):
                print(f"   Response status: {getattr(e.resp, 'status', '?')}")
            if hasattr(e, "error_details"):
                print(f"   Error details: {e.error_details}")
            if hasattr(e, "content") and e.content:
                try:
                    raw = e.content.decode() if isinstance(e.content, bytes) else e.content
                    err = json.loads(raw)
                    print("   Error body (JSON):")
                    print(json.dumps(err, indent=2))
                except Exception:
                    print(f"   Error body (raw): {e.content}")
            return 1
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from sqlalchemy import select, func
    from sync.oauth_helpers import credentials_from_token, ensure_valid_credentials
    from core.capabilities_loader import get_embed_max_chars, get_classify_max_chars
    CLASSIFY_BODY_MAX_CHARS = get_classify_max_chars()
    EMBED_TEXT_MAX_CHARS = get_embed_max_chars()

    with db_session() as db:
        account = db.get(EmailAccount, args.account_id)
        if not account:
            print(f"Account {args.account_id} not found", file=sys.stderr)
            return 1
        creds = credentials_from_token(account.oauth_token_encrypted)
        creds = ensure_valid_credentials(creds)
        gmail = GmailClient(creds)

        profile = gmail.get_profile()
        api_messages = profile.get("messagesTotal", 0)
        api_threads = profile.get("threadsTotal", 0)

        base_filter = (
            Email.account_id == args.account_id,
            Email.deleted_at.is_(None),
        )
        db_count = db.execute(
            select(func.count()).select_from(Email).where(*base_filter)
        ).scalar_one() or 0

        with_body = db.execute(
            select(func.count())
            .select_from(Email)
            .where(*base_filter)
            .where(Email.body_text.isnot(None))
        ).scalar_one() or 0

        body_len = func.length(Email.body_text)
        with_body_filter = (*base_filter, Email.body_text.isnot(None))
        within_classify = db.execute(
            select(func.count())
            .select_from(Email)
            .where(*with_body_filter)
            .where(body_len <= CLASSIFY_BODY_MAX_CHARS)
        ).scalar_one() or 0
        within_embed = db.execute(
            select(func.count())
            .select_from(Email)
            .where(*with_body_filter)
            .where(body_len <= EMBED_TEXT_MAX_CHARS)
        ).scalar_one() or 0

        max_body_chars = None
        if with_body:
            max_body_chars = db.execute(
                select(func.max(body_len)).select_from(Email).where(*with_body_filter)
            ).scalar_one()

        print(f"Account {args.account_id}: {account.email_address}")
        print(f"  Gmail API:  messages={api_messages}, threads={api_threads}")
        print(f"  Database:   messages={db_count} (non-deleted)")
        if api_messages == db_count:
            print("  Count:      OK — counts match.")
        else:
            diff = db_count - api_messages
            print(f"  Count:      MISMATCH — DB differs by {diff:+d} (run sync to align).")
        print(f"  Context:    {with_body} emails have body_text (transform eligible)")
        print(f"             {within_classify} fit classification context (≤{CLASSIFY_BODY_MAX_CHARS} chars)")
        print(f"             {within_embed} fit embedding context (≤{EMBED_TEXT_MAX_CHARS} chars)")
        if with_body and (within_classify < with_body or within_embed < with_body):
            over_classify = with_body - within_classify
            over_embed = with_body - within_embed
            print(f"             → {over_classify} truncated for classification, {over_embed} truncated for embedding")
        elif with_body:
            print("             → All fit in context.")
        if with_body and max_body_chars is not None:
            print(f"             max body_text size: {max_body_chars:,} chars")
        exit_ok = api_messages == db_count
        return 0 if exit_ok else 1


def cmd_capabilities(args: argparse.Namespace) -> int:
    from pathlib import Path
    from core.capabilities import run_all, write_capabilities_json

    data = run_all()
    print("Capabilities (discovered from deployed services):")
    emb = data.get("embedding") or {}
    print(f"  Embedding:  max_tokens={emb.get('max_tokens')}, max_chars={emb.get('max_chars')} (source: {emb.get('source')})")
    vllm = data.get("vllm") or {}
    print(f"  vLLM:      max_model_len={vllm.get('max_model_len')} (source: {vllm.get('source')})")
    api = data.get("spacy_api") or {}
    print(f"  Spacy API: url={api.get('url')}, available={api.get('available')}")
    ft = data.get("fasttext_langdetect") or {}
    print(f"  FastText:  url={ft.get('url')}, available={ft.get('available')}")
    if data.get("classify_body_max_chars") is not None:
        print(f"  Suggested:  classify_body_max_chars={data['classify_body_max_chars']}")

    if args.output:
        path = Path(args.output).resolve()
        write_capabilities_json(path, data)
        print(f"  Wrote {path} — transform/validate will use these limits when run from this project.")
    return 0


def cmd_reset_transform(args: argparse.Namespace) -> int:
    with db_session() as db:
        count_stmt = select(func.count()).select_from(Email).where(Email.deleted_at.is_(None)).where(Email.body_text.isnot(None))
        if args.account_id is not None:
            count_stmt = count_stmt.where(Email.account_id == args.account_id)
        n_emails = db.execute(count_stmt).scalar_one() or 0
        if n_emails == 0:
            scope = f"account {args.account_id}" if args.account_id is not None else "all accounts"
            print(f"No emails to reset in {scope} (need body_text, non-deleted).", file=sys.stderr)
            return 0
        if not _require_confirm(
            f"Reset transform state for {n_emails:,} emails (clear derived fields + chunks). Then run 'transform' to recompute. This cannot be undone.",
            getattr(args, "yes", False),
        ):
            return 1
        scope_conditions = [Email.deleted_at.is_(None), Email.body_text.isnot(None)]
        if args.account_id is not None:
            scope_conditions.append(Email.account_id == args.account_id)
        subq = select(Email.id).where(*scope_conditions)
        db.execute(delete(EmailChunk).where(EmailChunk.email_id.in_(subq)))
        cleared = db.execute(
            update(Email).where(*scope_conditions).values(
                privacy_tier=None,
                body_redacted=None,
                snippet_redacted=None,
                subject_embedding=None,
                body_embedding=None,
                body_pooled_embedding=None,
                transform_completed_at=None,
            )
        )
        db.commit()
        transform_cmd = f"transform {args.account_id}" if args.account_id is not None else "transform"
        print(f"Reset {cleared.rowcount:,} emails (cleared derived fields; deleted chunks). Run '{transform_cmd}' to recompute.")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    import alembic.config
    alembic.config.main(argv=["upgrade", "head"])
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from mcp_server.server import main as mcp_main
    return mcp_main(transport=args.transport, port=args.port)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="lilith-gmail",
        description="Lilith Email System. Commands: serve (daemon) | add-account, check-account | sync, transform | reset-transform | get-email | validate, capabilities, migrate | prepare-debug, debug-fetch | mcp.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Run webhook daemon (Pub/Sub)")
    serve_p.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    serve_p.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    serve_p.add_argument("--reload", action="store_true", help="Reload on code change")
    serve_p.set_defaults(func=cmd_serve)

    add_p = sub.add_parser("add-account", help="Add or update Gmail account (OAuth)")
    add_p.add_argument("secrets", help="Path to client_secrets.json")
    add_p.add_argument("--token-output", "-o", metavar="PATH", help="Save encrypted token to PATH")
    add_p.set_defaults(func=cmd_add_account)

    sync_p = sub.add_parser("sync", help="Download emails from Gmail (raw only); then run transform")
    sync_p.add_argument("account_id", type=int, help="Account ID")
    sync_p.add_argument("-n", "--limit", type=int, default=None, metavar="N", help="Max messages (default: 1000, 0 = no limit)")
    sync_p.add_argument("-j", "--concurrency", type=int, default=10, metavar="N", help="Concurrent API calls (default: 10)")
    sync_p.set_defaults(func=cmd_sync)

    transform_p = sub.add_parser("transform", help="Classify, sanitize, embed stored emails (no download)")
    transform_p.add_argument("account_id", type=int, nargs="?", default=None, help="Account ID (default: all)")
    transform_p.add_argument("--force", action="store_true", help="Recompute already-transformed emails (asks confirmation unless -y)")
    transform_p.add_argument("-y", "--yes", action="store_true", dest="yes", help="Skip confirmation for --force")
    transform_p.add_argument("--batch-size", type=int, default=50, metavar="N", help="Emails per batch (default: 50); use 1 for one-at-a-time (safest for small TEI)")
    transform_p.add_argument("-n", "--limit", type=int, default=None, metavar="N", help="Max emails to transform")
    transform_p.add_argument("--email-id", type=int, default=None, metavar="ID", help="Transform only this email (database id)")
    transform_p.add_argument("--no-tui", action="store_true", help="No TUI (for pipes/CI)")
    transform_p.set_defaults(func=cmd_transform)

    reset_transform_p = sub.add_parser("reset-transform", help="Clear all transform-derived data; then run transform to recompute from clean state")
    reset_transform_p.add_argument("account_id", type=int, nargs="?", default=None, help="Account ID (default: all)")
    reset_transform_p.add_argument("-y", "--yes", action="store_true", dest="yes", help="Skip confirmation")
    reset_transform_p.set_defaults(func=cmd_reset_transform)

    get_email_p = sub.add_parser("get-email", help="Print one email by id or Gmail message id")
    get_email_p.add_argument("email_id", help="Database id (int) or Gmail message id")
    get_email_p.add_argument("--full", action="store_true", help="Full body (default: truncate 2000)")
    get_email_p.add_argument("--raw", action="store_true", help="Original body_text (default: sanitized for PERSONAL)")
    get_email_p.set_defaults(func=cmd_get_email)

    check_p = sub.add_parser("check-account", help="Print token scopes (debug 403)")
    check_p.add_argument("account_id", type=int, help="Account ID")
    check_p.set_defaults(func=cmd_check_account)

    prepare_debug_p = sub.add_parser("prepare-debug", help="Run prepare for one email; full traceback on failure")
    prepare_debug_p.add_argument("email_id", help="Database id or Gmail message id")
    prepare_debug_p.set_defaults(func=cmd_prepare_debug)

    debug_p = sub.add_parser("debug-fetch", help="Fetch one email from Gmail; print error on failure (debug 403)")
    debug_p.add_argument("account_id", type=int, nargs="?", default=1, help="Account ID (default: 1)")
    debug_p.set_defaults(func=cmd_debug_fetch)

    validate_p = sub.add_parser("validate", help="Compare DB count vs Gmail API")
    validate_p.add_argument("account_id", type=int, help="Account ID")
    validate_p.set_defaults(func=cmd_validate)

    cap_p = sub.add_parser("capabilities", help="Discover embedding/vLLM/Spacy limits; write capabilities.json with -o")
    cap_p.add_argument("-o", "--output", metavar="PATH", help="Write capabilities.json to PATH")
    cap_p.set_defaults(func=cmd_capabilities)

    mig_p = sub.add_parser("migrate", help="Run DB migrations (alembic upgrade head)")
    mig_p.set_defaults(func=cmd_migrate)

    mcp_p = sub.add_parser("mcp", help="Run MCP server (or: uv run mcp)")
    mcp_p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio", help="Transport (default: stdio)")
    mcp_p.add_argument("--port", type=int, default=8001, help="Port for streamable-http (default: 8001)")
    mcp_p.set_defaults(func=cmd_mcp)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
