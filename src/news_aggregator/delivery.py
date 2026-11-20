"""Render the markdown briefing to email-safe HTML and send it via SES or SMTP."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol

import markdown as md

from .config import AppConfig
from .logging_setup import log

EMAIL_CSS = """
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; color: #1f2933;
       line-height: 1.5; margin: 0; padding: 0; background: #f5f7fa; }
.wrap { max-width: 680px; margin: 0 auto; background: #ffffff; padding: 28px 32px; }
h1 { font-size: 22px; margin: 0 0 16px; color: #102a43; }
h2 { font-size: 17px; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 2px solid #d9e2ec;
     color: #243b53; }
h3 { font-size: 15px; margin: 18px 0 4px; color: #102a43; }
p, li { font-size: 14px; margin: 4px 0; }
a { color: #0b69a3; }
hr { border: none; border-top: 1px solid #d9e2ec; margin: 24px 0 8px; }
em { color: #627d98; font-size: 12px; }
"""


def render_html(markdown_text: str) -> str:
    body = md.markdown(markdown_text, extensions=["extra", "sane_lists"])
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{EMAIL_CSS}</style></head><body><div class='wrap'>{body}</div></body></html>"
    )
    try:
        from premailer import transform

        return transform(html, keep_style_tags=False, remove_classes=True)
    except Exception as exc:  # noqa: BLE001 - inline CSS is cosmetic; never block sending
        log.warning("premailer_failed", error=repr(exc)[:200])
        return html


class EmailSender(Protocol):
    def send(self, subject: str, markdown_text: str, html: str) -> str: ...


class SESSender:
    def __init__(self, sender: str, recipients: list[str], region: str | None = None, client=None):
        import boto3

        self.sender = sender
        self.recipients = recipients
        self.client = client or boto3.client("sesv2", region_name=region)

    def send(self, subject: str, markdown_text: str, html: str) -> str:
        resp = self.client.send_email(
            FromEmailAddress=self.sender,
            Destination={"ToAddresses": self.recipients},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": markdown_text, "Charset": "UTF-8"},
                        "Html": {"Data": html, "Charset": "UTF-8"},
                    },
                }
            },
        )
        return resp.get("MessageId", "")


class SMTPSender:
    def __init__(self, config: AppConfig):
        self.env = config.env
        self.sender = config.sender
        self.recipients = config.recipients

    def send(self, subject: str, markdown_text: str, html: str) -> str:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(markdown_text)
        msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(self.env.smtp_host, self.env.smtp_port, timeout=30) as smtp:
            if self.env.smtp_starttls:
                smtp.starttls()
            if self.env.smtp_username:
                smtp.login(self.env.smtp_username, self.env.smtp_password or "")
            smtp.send_message(msg)
        return msg.get("Message-ID", "")


class NullSender:
    """Logs instead of sending (local development and dry runs)."""

    def send(self, subject: str, markdown_text: str, html: str) -> str:
        log.info("email_skipped", subject=subject, chars=len(markdown_text))
        return "not-sent"


def build_sender(config: AppConfig) -> EmailSender:
    backend = config.env.email_backend
    if backend == "ses":
        if not config.recipients:
            raise ValueError("no email recipients configured")
        return SESSender(config.sender, config.recipients, config.env.aws_region)
    if backend == "smtp":
        return SMTPSender(config)
    return NullSender()
