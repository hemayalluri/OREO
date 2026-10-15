"""
Actions: making sales reports and emailing them (step 3).

One small Haiku call turns the request into structured fields (what, which period,
region, formats, recipient). Everything after that is plain code: the report numbers
come from the read-only MCP tools, and an email is only drafted, never sent, until
the next message is a clear "yes" (checked by code in phrases.is_confirmation).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import reports
from .emailer import Draft, Emailer, EmailError
from .models import ModelAdapter, text_of
from .phrases import is_cancel, is_confirmation

log = logging.getLogger("oreo.actions")

PERIODS = ["last_7_days", "last_30_days", "last_90_days", "this_month", "last_month", "this_quarter",
           "last_quarter", "this_year", "last_year", "all_time", "custom"]
FORMAT_NAMES = {"pdf": "a PDF", "docx": "a Word document", "xlsx": "an Excel file"}

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "enum": ["report", "email_report", "email_previous_report", "unsupported"]},
        "period": {"type": "string", "enum": PERIODS},
        "start_date": {"type": "string"},
        "end_date": {"type": "string"},
        "region": {"type": "string"},
        "formats": {"type": "array", "items": {"type": "string", "enum": list(reports.FORMATS)}},
        "recipient": {"type": "string"},
    },
    "required": ["task", "period", "start_date", "end_date", "region", "formats", "recipient"],
    "additionalProperties": False,
}

ACTION_SYSTEM = """You turn a request to Oreo, a sales team's desk robot, into fields. Today is {today}.
- task: "report" to create a sales report; "email_report" to create one and email it;
  "email_previous_report" to email the report that was just made ("email it to me", "send that to Sam");
  "unsupported" for any other action (calendar invites, Slack, editing files...).
- period: the named period that matches; "custom" with start_date and end_date (YYYY-MM-DD) for anything else,
  e.g. a named month. Default "last_month". Leave the dates "" unless period is "custom".
- region: a sales region if one is named (e.g. "Europe"), else "".
- formats: pdf, docx (Word) and/or xlsx (Excel) as asked; [] if not said.
- recipient: an email address if one is given, "me" if they want it themselves, else ""."""


@dataclass
class ActionResult:
    text: str
    files: list[Path] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    as_of: str | None = None
    tools_used: list[str] = field(default_factory=list)
    error: bool = False


class Actions:
    def __init__(self, settings: Any, models: ModelAdapter, gateway: Any):
        self.settings = settings
        self.models = models
        self.gateway = gateway
        self.emailer = Emailer(settings.raw.get("email", {}), settings.root)
        self.reports_dir = settings.root / settings.raw.get("reports", {}).get("dir", "reports")
        self.pending: Draft | None = None
        self.last_report: reports.Report | None = None

    def reset(self) -> None:
        self.pending = None
        self.last_report = None

    # ------------------------------------------------------------------ #
    # Pending email: only a clear yes sends it
    # ------------------------------------------------------------------ #

    async def resolve_pending(self, question: str) -> ActionResult | None:
        """If an email is waiting for confirmation, handle the reply. Returns None when the
        message is something else (the draft is then dropped and the question answered normally)."""
        draft, self.pending = self.pending, None
        if draft is None:
            return None
        if draft.expired:
            log.info("Pending email expired unsent")
            return None
        if is_confirmation(question):
            try:
                where = await self.emailer.send(draft)
            except EmailError as exc:
                return ActionResult(str(exc), error=True)
            if where == "outbox":
                return ActionResult(f"Done! Email sending isn't switched on yet, so I saved the email to {draft.to} "
                                    "in my outbox folder instead.")
            return ActionResult(f"Sent! It's on its way to {draft.to}.")
        if is_cancel(question):
            return ActionResult("Okay, I won't send it.")
        log.info("Pending email dropped: the reply wasn't a clear yes")
        return None

    # ------------------------------------------------------------------ #
    # New action requests
    # ------------------------------------------------------------------ #

    async def handle(self, question: str, recent: list[str], session_id: str) -> ActionResult:
        r = self.settings.routing
        context = "".join(f"Earlier: {q}\n" for q in recent[-3:])
        msg = await self.models.create(
            model=r.get("action_model", r["general_model"]),
            system=ACTION_SYSTEM.format(today=datetime.now().strftime("%A, %B %-d, %Y")),
            messages=[{"role": "user", "content": f"{context}Request: {question}"}],
            effort="low", max_tokens=2000, output_schema=ACTION_SCHEMA, cache=False)
        try:
            req = json.loads(text_of(msg))
        except (ValueError, TypeError):
            return ActionResult("Sorry, I didn't quite get what you'd like me to make.", error=True)
        log.info("Action: %s", req)

        task = req.get("task")
        if task == "unsupported":
            return ActionResult("I can make sales reports and email them, but I can't do that one yet.")
        if task == "email_previous_report":
            if not self.last_report:
                task = "email_report"  # nothing made yet: make it first
            else:
                return self._draft_email(self.last_report, req.get("recipient", ""))

        try:
            report = await self._make_report(req, session_id)
        except reports.ReportError as exc:
            return ActionResult(f"I couldn't make that report: {exc}", error=True)
        if task == "email_report":
            result = self._draft_email(report, req.get("recipient", ""))
            result.files = report.files
            return result
        kinds = " and ".join(FORMAT_NAMES[f.suffix[1:]] for f in report.files)
        return ActionResult(f"Your {report.data.name} is ready as {kinds}. "
                            f"{reports.spoken_summary(report.data)} Want me to email it?",
                            report.files, [report.data.source], report.data.as_of, ["sales report"])

    async def _make_report(self, req: dict[str, Any], session_id: str) -> reports.Report:
        custom = req.get("period") == "custom" and req.get("start_date") and req.get("end_date")
        data = await reports.gather(
            self.gateway, period=req.get("period") if not custom else None,
            start_date=req.get("start_date") if custom else None, end_date=req.get("end_date") if custom else None,
            region=req.get("region") or None, session_id=session_id)
        formats = req.get("formats") or self.settings.raw.get("reports", {}).get("default_formats", ["pdf"])
        self.last_report = reports.Report(data, reports.build(data, formats, self.reports_dir))
        return self.last_report

    def _draft_email(self, report: reports.Report, recipient: str) -> ActionResult:
        to = recipient.strip()
        if to.lower() in ("", "me", "myself"):
            to = self.emailer.default_recipient
        if not to:
            return ActionResult("Your report is ready, but I don't have an email address for you yet. "
                                "Add default_recipient under email in my settings, or tell me the address.",
                                report.files)
        d = report.data
        body = (f"Hi,\n\nHere's the {d.name} ({reports.period_text(d.period)}).\n\n"
                + "\n".join(f"- {h}" for h in d.highlights)
                + f"\n\nSource: {d.source}, data as of {d.as_of}.\n\n- Oreo")
        try:
            self.pending = self.emailer.draft(to, f"{d.title} ({reports.period_text(d.period)})", body, report.files)
        except EmailError as exc:
            return ActionResult(str(exc), report.files, error=True)
        names = " and ".join(FORMAT_NAMES[f.suffix[1:]] for f in report.files)
        return ActionResult(f"I've got the {d.name} ready as {names}. "
                            f"Should I email it to {to}? Just say yes to send it.",
                            report.files, [d.source], d.as_of, ["sales report"])
