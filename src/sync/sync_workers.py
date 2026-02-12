import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from core.email_utils import parse_date, parse_email_address, parse_email_list
from core.models import (
    AccountLabel,
    Email,
    EmailAccount,
    EmailAttachment,
    EmailThread,
    SyncEvent,
)

from .gmail_client import GmailClient

logger = logging.getLogger(__name__)


class SyncWorker:
    def __init__(self, gmail_client: GmailClient, db: Session):
        self.gmail = gmail_client
        self.db = db

    async def _sync_labels(self, account_id: int) -> None:
        try:
            raw = await asyncio.to_thread(self.gmail.list_labels)
        except Exception as e:
            logger.warning("Failed to fetch labels for account %s: %s", account_id, e)
            return
        self.db.execute(
            delete(AccountLabel).where(AccountLabel.account_id == account_id)
        )
        for lb in raw:
            label_id = lb.get("id") or ""
            name = lb.get("name") or label_id
            if label_id:
                self.db.add(
                    AccountLabel(
                        account_id=account_id, label_id=label_id, label_name=name
                    )
                )
        self.db.commit()
        logger.info("Synced %s labels for account %s", len(raw), account_id)

    async def full_sync(
        self,
        account_id: int,
        limit: int | None = 1000,
        concurrency: int = 10,
    ):
        account = self.db.get(EmailAccount, account_id)
        if not account:
            logger.error("Account %s not found", account_id)
            return

        logger.info(
            "Starting full sync for account %s (%s), limit=%s",
            account_id,
            account.email_address,
            limit or "none",
        )

        sync_event = SyncEvent(
            account_id=account_id,
            event_type="full_sync",
            status="started",
        )
        self.db.add(sync_event)
        self.db.commit()

        try:
            await self._sync_labels(account_id)

            processed = 0
            total_new = 0
            page_token = None
            page_num = 0
            _logged_403_hint = False

            while True:
                page_num += 1
                logger.info(
                    "Fetching message list (page %s, max_results=500)%s",
                    page_num,
                    f", page_token=...{page_token[-8:] if page_token else ''}"
                    if page_token
                    else "",
                )
                response = self.gmail.list_messages(
                    max_results=500, page_token=page_token
                )
                messages = response.get("messages", [])

                if not messages:
                    logger.info("No more messages.")
                    break

                batch_size = len(messages)
                logger.info(
                    "Downloading and storing %s messages (max %s concurrent)...",
                    batch_size,
                    concurrency,
                )
                sem = asyncio.Semaphore(concurrency)
                tasks = [
                    self._process_email_with_sem(sem, account_id, msg["id"])
                    for msg in messages
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                new_count = 0
                for i, res in enumerate(results):
                    if isinstance(res, Exception):
                        logger.warning(
                            "Failed to process message %s: %s", messages[i]["id"], res
                        )
                        if not _logged_403_hint and "403" in str(res):
                            _logged_403_hint = True
                            logger.info(
                                "403 hint: Add scope gmail.readonly in OAuth consent screen, "
                                "add yourself as Test user, then run: uv run python main.py add-account <client_secrets>"
                            )
                    else:
                        processed += 1
                        if res is not None:
                            new_count += 1
                total_new += new_count

                self.db.commit()
                logger.info(
                    "Stored batch: %s new, %s total so far (run 'transform %s' to generate embeddings)",
                    new_count,
                    processed,
                    account_id,
                )

                sync_event.emails_processed = processed
                self.db.commit()

                page_token = response.get("nextPageToken")
                if not page_token:
                    logger.info("Reached end of mailbox.")
                    break
                if limit and processed >= limit:
                    logger.info("Reached limit %s.", limit)
                    break

            profile = self.gmail.service.users().getProfile(userId="me").execute()
            account.last_history_id = profile.get("historyId")
            account.last_sync_at = datetime.now()
            sync_event.status = "completed"
            sync_event.completed_at = datetime.now()
            self.db.commit()

            logger.info(
                "Full sync completed for %s: %s processed (%s new). Run 'transform %s' to classify and embed.",
                account.email_address,
                processed,
                total_new,
                account_id,
            )

        except Exception as e:
            logger.exception("Full sync failed")
            sync_event.status = "failed"
            sync_event.error_message = str(e)
            sync_event.completed_at = datetime.now()
            self.db.commit()

    async def _process_email_with_sem(
        self, sem: asyncio.Semaphore, account_id: int, gmail_id: str
    ) -> Email | None:
        async with sem:
            return await self.process_email(account_id, gmail_id)

    async def process_email(self, account_id: int, gmail_id: str) -> Email | None:
        existing = self.db.execute(
            select(Email)
            .where(Email.gmail_id == gmail_id)
            .where(Email.deleted_at.is_(None))
        ).scalar_one_or_none()
        if existing:
            logger.debug("Skip (already stored): gmail_id=%s", gmail_id)
            return None

        msg = self.gmail.get_message(gmail_id)
        payload = msg["payload"]
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

        body_text = self.gmail.parse_body(payload)
        subject = headers.get("Subject", "")
        sent_at = parse_date(headers.get("Date", "")) or datetime.now()
        from_email, from_name = parse_email_address(headers.get("From", ""))

        email = Email(
            account_id=account_id,
            gmail_id=gmail_id,
            gmail_thread_id=msg["threadId"],
            history_id=int(msg["historyId"]) if msg.get("historyId") else None,
            subject=subject,
            from_email=from_email or headers.get("From", "unknown"),
            from_name=from_name or None,
            to_emails=parse_email_list(headers.get("To", "")),
            cc_emails=parse_email_list(headers.get("Cc", "")) or None,
            bcc_emails=parse_email_list(headers.get("Bcc", "")) or None,
            reply_to=headers.get("Reply-To") or None,
            sent_at=sent_at,
            labels=msg.get("labelIds") or [],
            body_text=body_text,
            snippet=msg.get("snippet", ""),
            privacy_tier=None,
            body_redacted=None,
            has_attachments=any(
                p.get("filename") and p["body"].get("attachmentId")
                for p in payload.get("parts", [])
            ),
        )
        self.db.add(email)
        self.db.flush()

        if email.has_attachments:
            await self._process_attachments(email, payload)

        await self._update_thread(account_id, msg["threadId"], subject)
        return email

    async def _process_attachments(self, email: Email, payload: dict[str, Any]) -> None:
        if "parts" not in payload:
            return
        for part in payload.get("parts", []):
            if part.get("filename") and part["body"].get("attachmentId"):
                part_headers = {
                    h["name"].lower(): h["value"] for h in part.get("headers", [])
                }
                disp = part_headers.get("content-disposition", "")
                att = EmailAttachment(
                    email_id=email.id,
                    gmail_attachment_id=part["body"]["attachmentId"],
                    filename=part["filename"],
                    mime_type=part.get("mimeType", "application/octet-stream"),
                    size_bytes=int(part["body"].get("size", 0)),
                    is_inline="inline" in disp.lower(),
                )
                self.db.add(att)
                email.attachment_count += 1

    async def incremental_sync(
        self, account_id: int, start_history_id: int | None
    ) -> None:
        account = self.db.get(EmailAccount, account_id)
        if not account:
            logger.error("Account %s not found", account_id)
            return

        if not start_history_id:
            logger.info("No start_history_id; running full sync instead")
            await self.full_sync(account_id)
            return

        logger.info(
            "Incremental sync for account %s (%s) from historyId=%s",
            account_id,
            account.email_address,
            start_history_id,
        )

        sync_event = SyncEvent(
            account_id=account_id, event_type="incremental", status="started"
        )
        self.db.add(sync_event)
        self.db.commit()

        try:
            processed = 0
            page_token = None
            last_history_id = start_history_id

            while True:
                response = self.gmail.get_history(start_history_id, page_token)
                history = response.get("history", [])
                if response.get("historyId"):
                    last_history_id = int(response["historyId"])

                for change in history:
                    if "messagesAdded" in change:
                        for msg_added in change["messagesAdded"]:
                            msg = msg_added.get("message", {})
                            if msg:
                                await self.process_email(account_id, msg["id"])
                                processed += 1
                    if "messagesDeleted" in change:
                        for msg_del in change["messagesDeleted"]:
                            msg = msg_del.get("message", {})
                            if msg:
                                existing = self.db.execute(
                                    select(Email).where(Email.gmail_id == msg["id"])
                                ).scalar_one_or_none()
                                if existing:
                                    existing.deleted_at = datetime.now()
                                    self.db.commit()

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

            account.last_history_id = last_history_id
            account.last_sync_at = datetime.now()
            sync_event.status = "completed"
            sync_event.emails_processed = processed
            sync_event.completed_at = datetime.now()
            self.db.commit()

            logger.info(
                "Incremental sync completed: %s messages added/updated", processed
            )

        except Exception as e:
            logger.exception("Incremental sync failed")
            sync_event.status = "failed"
            sync_event.error_message = str(e)
            sync_event.completed_at = datetime.now()
            self.db.commit()

    async def _update_thread(
        self, account_id: int, thread_id: str, subject: str
    ) -> None:
        thread = self.db.execute(
            select(EmailThread)
            .where(EmailThread.account_id == account_id)
            .where(EmailThread.gmail_thread_id == thread_id)
        ).scalar_one_or_none()
        if not thread:
            thread = EmailThread(
                account_id=account_id,
                gmail_thread_id=thread_id,
                subject=subject,
                message_count=1,
                last_message_at=datetime.now(),
            )
            self.db.add(thread)
        else:
            thread.message_count += 1
            thread.last_message_at = datetime.now()
