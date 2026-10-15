"""
Email for Oreo: drafts are only ever sent after a clear spoken or typed "yes".

Safety rules, enforced here in code (not by the language model):
- Recipients must be in `email.allowed_domains` (config.yaml).
- Nothing is sent when a draft is created; the orchestrator sends it only when the
  next message is a clear confirmation (see phrases.is_confirmation), and drops it otherwise.
- `email.mode: outbox` (the default) writes the message to outbox/ as an .eml file
  instead of sending, so you can test the whole flow without a mail account.
  `email.mode: smtp` sends through the server in .env (SMTP_HOST, SMTP_PORT, SMTP_USER,
  SMTP_PASSWORD, EMAIL_FROM), using STARTTLS.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

log = logging.getLogger("oreo.email")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)$")
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
DRAFT_TTL_SECONDS = 180


class EmailError(Exception):
    """The email can't be drafted or sent; the message is safe to say out loud."""


@dataclass
class Draft:
    to: str
    subject: str
    body: str
    attachments: list[Path] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > DRAFT_TTL_SECONDS


class Emailer:
    def __init__(self, settings: dict[str, Any], root: Path):
        self.allowed = [d.lower().lstrip("@") for d in settings.get("allowed_domains", [])]
        self.mode = settings.get("mode", "outbox")
        self.default_recipient = (settings.get("default_recipient") or "").strip()
        self.outbox = root / settings.get("outbox_dir", "outbox")
        self.sender = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER") or "oreo@localhost"

    def check_recipient(self, address: str) -> str:
        address = (address or "").strip().strip("<>.").lower()
        match = EMAIL_RE.match(address)
        if not match:
            raise EmailError("I need a full email address for that.")
        domain = match.group(1)
        if not any(domain == d or domain.endswith("." + d) for d in self.allowed):
            allowed = ", ".join(self.allowed) or "no domains yet"
            raise EmailError(f"I can only email addresses at {allowed}. That's a safety rule in my settings.")
        return address

    def draft(self, to: str, subject: str, body: str, attachments: list[Path] | None = None) -> Draft:
        to = self.check_recipient(to)
        files = [Path(a) for a in attachments or []]
        if sum(f.stat().st_size for f in files) > MAX_ATTACHMENT_BYTES:
            raise EmailError("Those attachments are too big to email.")
        return Draft(to, subject.strip()[:200], body.strip(), files)

    def _message(self, d: Draft) -> EmailMessage:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, d.to, d.subject
        msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain="oreo.local")
        msg.set_content(d.body)
        types = {".pdf": ("application", "pdf"),
                 ".docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
                 ".xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        for f in d.attachments:
            main, sub = types.get(f.suffix.lower(), ("application", "octet-stream"))
            msg.add_attachment(f.read_bytes(), maintype=main, subtype=sub, filename=f.name)
        return msg

    async def send(self, d: Draft) -> str:
        """Send (or file in the outbox) a confirmed draft. Returns where it went."""
        self.check_recipient(d.to)  # re-check: config may have changed since drafting
        msg = self._message(d)
        if self.mode == "smtp":
            await asyncio.to_thread(self._send_smtp, msg)
            log.info("Emailed %s to %s", d.subject, d.to)
            return "sent"
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9]+', '-', d.subject.lower())[:50]}.eml"
        path.write_bytes(bytes(msg))
        log.info("Saved email to %s (outbox mode)", path)
        return "outbox"

    @staticmethod
    def _send_smtp(msg: EmailMessage) -> None:
        host = os.environ.get("SMTP_HOST")
        if not host:
            raise EmailError("Email sending isn't set up yet: there's no SMTP server in my settings.")
        port = int(os.environ.get("SMTP_PORT", "587"))
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if os.environ.get("SMTP_USER"):
                    smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("SMTP send failed: %s", exc)
            raise EmailError("I couldn't send that email; the mail server said no.") from exc
