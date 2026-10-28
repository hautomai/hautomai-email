from __future__ import annotations

import base64
import html
import json
import random
import re
import socket
import ssl
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from http.client import HTTPException

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import CREDENTIALS_FILE, GMAIL_SCOPES, ROOT, TOKEN_FILE, env


class TokenBucket:
    """Token-bucket limiter to keep API request rates under quota."""

    def __init__(self, rate_per_minute: float, burst: int = 1):
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.rate = rate_per_minute / 60.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def wait(self, cost: int = 1) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                sleep_for = (cost - self.tokens) / self.rate
            time.sleep(min(sleep_for, 10.0))


SHEETS_READ_LIMITER = TokenBucket(rate_per_minute=40, burst=10)


def _retry(fn, retries: int = 4, base_delay: float = 2.0, max_delay: float = 60.0):
    for attempt in range(retries):
        try:
            return fn()
        except HttpError as exc:
            status = exc.resp.status
            retryable = status in (429, 500, 502, 503, 504) or (
                status == 400 and "failedPrecondition" in (exc.content or b"").decode(errors="ignore")
            )
            if not retryable or attempt >= retries - 1:
                raise
            if status == 429:
                retry_after = exc.resp.get("retry-after")
                if retry_after:
                    delay = float(retry_after)
                else:
                    delay = (60 - time.time() % 60) + random.uniform(1, 5)
            else:
                delay = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, 0.5)
            time.sleep(delay)
        except (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError, HTTPException, OSError):
            if attempt >= retries - 1:
                raise
            delay = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, 0.5)
            time.sleep(delay)


def _plain_to_html(body: str) -> str:
    paragraphs = re.split(r"\n{2,}", body)
    blocks = []
    for paragraph in paragraphs:
        lines = [html.escape(line) for line in paragraph.split("\n") if line.strip()]
        if not lines:
            continue
        blocks.append("<p>" + "<br>".join(lines) + "</p>")
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.5;color:#222;">' + "".join(blocks) + "</div>"
    )


class GoogleAuth:
    @staticmethod
    def from_env() -> Credentials | None:
        token_json = env("GMAIL_TOKEN_JSON")
        if token_json:
            return Credentials.from_authorized_user_info(json.loads(token_json), GMAIL_SCOPES)
        refresh = env("GMAIL_REFRESH_TOKEN")
        if refresh:
            return Credentials(
                token=None,
                refresh_token=refresh,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=env("GMAIL_CLIENT_ID"),
                client_secret=env("GMAIL_CLIENT_SECRET"),
                scopes=GMAIL_SCOPES,
            )
        return None

    @staticmethod
    def token() -> Credentials:
        from_env = GoogleAuth.from_env()
        if from_env is not None:
            return GoogleAuth.refresh(from_env, persist=False)
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
            return GoogleAuth.refresh(creds, persist=True)
        return GoogleAuth.run_oauth()

    @staticmethod
    def refresh(creds: Credentials, persist: bool = True) -> Credentials:
        if creds and creds.valid:
            return creds
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise SystemExit("Google OAuth refresh failed. Run 'python main.py auth' again.") from exc
        if persist and TOKEN_FILE.exists():
            TOKEN_FILE.write_text(creds.to_json())
        return creds

    @staticmethod
    def run_oauth(force: bool = False) -> Credentials:
        if not CREDENTIALS_FILE.exists():
            raise SystemExit(
                "credentials.json missing. Download the OAuth desktop client file into this folder."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
        kwargs = {"port": 0}
        if force:
            kwargs["prompt"] = "consent"
        creds = flow.run_local_server(**kwargs)
        TOKEN_FILE.write_text(creds.to_json())
        return creds


class GmailClient:
    def __init__(self, credentials: Credentials | None = None):
        self.credentials = credentials or GoogleAuth.token()
        self._gmail = None
        self._my_email = None

    def _service(self):
        if self._gmail is None:
            self.credentials = GoogleAuth.refresh(self.credentials)
            self._gmail = build("gmail", "v1", credentials=self.credentials, cache_discovery=False)
        return self._gmail

    def my_email(self) -> str:
        if self._my_email is None:
            profile = _retry(lambda: self._service().users().getProfile(userId="me").execute())
            self._my_email = profile["emailAddress"]
        return self._my_email

    def send_mime(self, mime: MIMEText, thread_id: str | None = None) -> tuple[str, str]:
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        sent = _retry(lambda: self._service().users().messages().send(userId="me", body=body).execute())
        return sent["id"], sent.get("threadId", thread_id or "")

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> tuple[str, str]:
        mime = MIMEMultipart("alternative")
        mime.attach(MIMEText(body, "plain", "utf-8"))
        mime.attach(MIMEText(_plain_to_html(body), "html", "utf-8"))
        mime["To"] = to
        mime["Subject"] = subject
        mime["Date"] = formatdate(localtime=True)
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
        if references:
            mime["References"] = references
        return self.send_mime(mime, thread_id=thread_id)

    def get_thread(self, thread_id: str) -> list[dict]:
        return _retry(
            lambda: self._service()
            .users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Message-ID", "References", "Date"],
            )
            .execute()
        )["messages"]

    def list_messages(self, query: str, max_results: int = 50) -> list[dict]:
        return _retry(
            lambda: self._service()
            .users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        ).get("messages", [])

    def get_message(self, message_id: str, metadata_headers: list[str] | None = None) -> dict:
        return _retry(
            lambda: self._service()
            .users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=metadata_headers
                or ["From", "To", "Subject", "Message-ID", "X-Failed-Recipients"],
            )
            .execute()
        )

    @staticmethod
    def headers(msg: dict) -> dict[str, str]:
        return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

    @staticmethod
    def body_text(msg: dict) -> str:
        payload = msg.get("payload", {})
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return GmailClient._decode(payload["body"]["data"])
        for part in payload.get("parts", []):
            data = part.get("body", {}).get("data")
            if not data:
                continue
            mime_type = part.get("mimeType", "")
            if mime_type == "text/plain":
                return GmailClient._decode(data)
            if mime_type == "text/html":
                return html.unescape(GmailClient._decode(data))
        return msg.get("snippet", "")

    @staticmethod
    def _decode(data: str) -> str:
        missing = len(data) % 4
        if missing:
            data += "=" * (4 - missing)
        return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")