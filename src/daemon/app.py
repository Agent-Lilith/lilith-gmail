import asyncio
import base64
import json
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.database import SessionLocal, get_db
from core.embeddings import Embedder
from core.models import EmailAccount
from sync.gmail_client import GmailClient
from sync.oauth_helpers import credentials_from_token, ensure_valid_credentials
from sync.sync_workers import SyncWorker
from transform.pipeline import TransformPipeline
from transform.privacy import PrivacyManager

logger = logging.getLogger(__name__)

app = FastAPI()


def _run_transform_for_account(account_id: int) -> None:
    """Run transform pipeline for the given account (new emails only). Logs and swallows errors."""
    db = SessionLocal()
    try:
        pipeline = TransformPipeline(db, PrivacyManager(), Embedder())
        n = pipeline.run(
            account_id=account_id,
            force=False,
            batch_size=50,
            limit=None,
        )
        if n > 0:
            logger.info(
                "Post-sync transform completed for account %s: %s emails", account_id, n
            )
    except Exception as e:
        logger.exception("Post-sync transform failed for account %s: %s", account_id, e)
    finally:
        db.close()


def _run_incremental_sync(account_id: int, start_history_id: int | None) -> None:
    db = SessionLocal()
    try:
        account = db.get(EmailAccount, account_id)
        if not account:
            return
        creds = credentials_from_token(account.oauth_token_encrypted)
        creds = ensure_valid_credentials(creds)
        gmail = GmailClient(creds)
        worker = SyncWorker(gmail, db)
        asyncio.run(worker.incremental_sync(account_id, start_history_id))
    except Exception as e:
        logger.exception("Background incremental sync failed: %s", e)
    finally:
        db.close()

    _run_transform_for_account(account_id)


def handle_gmail_notification(email_address: str, history_id: int | None) -> None:
    """Process a single Gmail Pub/Sub notification (sync + transform). Used by webhook and pull."""
    db = SessionLocal()
    try:
        account = db.execute(
            select(EmailAccount).where(EmailAccount.email_address == email_address)
        ).scalar_one_or_none()
        if not account:
            logger.warning("No account found for %s", email_address)
            return
        start_history_id = account.last_history_id or history_id
        account_id = account.id
        db.commit()
    finally:
        db.close()
    _run_incremental_sync(account_id, start_history_id)


@app.post("/webhook/gmail")
async def gmail_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()

        if "message" not in data or "data" not in data["message"]:
            raise HTTPException(
                status_code=400, detail="Invalid Pub/Sub message format"
            )

        message_json = base64.b64decode(data["message"]["data"]).decode("utf-8")
        message = json.loads(message_json)

        email_address = message.get("emailAddress")
        history_id = message.get("historyId")
        if isinstance(history_id, str):
            history_id = int(history_id) if history_id.isdigit() else None

        logger.info(
            "Received sync notification for %s (History ID: %s)",
            email_address,
            history_id,
        )

        account = db.execute(
            select(EmailAccount).where(EmailAccount.email_address == email_address)
        ).scalar_one_or_none()

        if not account:
            logger.warning("No account found for %s", email_address)
            return {"status": "ignored"}

        start_history_id = account.last_history_id or history_id
        account_id = account.id
        db.commit()

        asyncio.create_task(
            asyncio.to_thread(handle_gmail_notification, email_address, history_id)
        )

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error handling webhook")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    return {"status": "healthy"}
