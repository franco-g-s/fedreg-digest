"""SMTP delivery. Optional: a digest written to disk is a complete result."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from fedreg_digest.config import Secrets


class EmailError(RuntimeError):
    pass


def recipients(profile_to: list[str], secrets: Secrets) -> list[str]:
    # The environment wins, so a public profile never has to contain an address.
    return secrets.email_to or profile_to


def check_settings(secrets: Secrets, to: list[str]) -> list[str]:
    missing = []
    if not secrets.smtp_host:
        missing.append("SMTP_HOST")
    if not secrets.smtp_username:
        missing.append("SMTP_USERNAME")
    if not secrets.smtp_password:
        missing.append("SMTP_PASSWORD")
    if not to:
        missing.append("FEDREG_EMAIL_TO (or email.to in the profile)")
    return missing


def _connect(secrets: Secrets) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if secrets.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(secrets.smtp_host, 465, context=context, timeout=30)
    else:
        server = smtplib.SMTP(secrets.smtp_host, secrets.smtp_port, timeout=30)
        server.starttls(context=context)
    server.login(secrets.smtp_username, secrets.smtp_password)
    return server


def test_login(secrets: Secrets) -> None:
    try:
        with _connect(secrets):
            pass
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(
            f"SMTP login to {secrets.smtp_host}:{secrets.smtp_port} failed: {exc}"
        ) from exc


def send(secrets: Secrets, to: list[str], subject: str, html: str, text: str) -> None:
    missing = check_settings(secrets, to)
    if missing:
        raise EmailError(f"Cannot send email; not set: {', '.join(missing)}")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = secrets.smtp_from or secrets.smtp_username
    message["To"] = ", ".join(to)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    try:
        with _connect(secrets) as server:
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Sending to {', '.join(to)} failed: {exc}") from exc
