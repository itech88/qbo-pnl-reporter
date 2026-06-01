"""
Email delivery layer.

Reads EMAIL_PROVIDER from .env and dispatches to the correct backend.
Supported providers: smtp | sendgrid | ses

Public API:
    send_report(html: str, subject: str | None = None) -> None
"""

import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_subject() -> str:
    return f"COGS Report — {datetime.now().strftime('%B %Y')}"


def _build_mime(subject: str, html: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(html, "html"))
    return msg


# ---------------------------------------------------------------------------
# SMTP backend
# ---------------------------------------------------------------------------

def _send_smtp(subject: str, html: str) -> None:
    host     = os.environ["SMTP_HOST"]
    port     = int(os.getenv("SMTP_PORT", "587"))
    user     = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    msg = _build_mime(subject, html)
    context = ssl.create_default_context()

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(msg["From"], msg["To"].split(","), msg.as_string())

    print(f"[smtp] Sent to {msg['To']}")


# ---------------------------------------------------------------------------
# SendGrid backend
# ---------------------------------------------------------------------------

def _send_sendgrid(subject: str, html: str) -> None:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=os.environ["EMAIL_FROM"],
        to_emails=os.environ["EMAIL_TO"],
        subject=subject,
        html_content=html,
    )
    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    response = sg.send(message)
    print(f"[sendgrid] Sent — status {response.status_code}")


# ---------------------------------------------------------------------------
# AWS SES backend
# ---------------------------------------------------------------------------

def _send_ses(subject: str, html: str) -> None:
    import boto3

    client = boto3.client(
        "ses",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    response = client.send_email(
        Source=os.environ["EMAIL_FROM"],
        Destination={"ToAddresses": os.environ["EMAIL_TO"].split(",")},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body":    {"Html": {"Data": html, "Charset": "UTF-8"}},
        },
    )
    print(f"[ses] Sent — MessageId {response['MessageId']}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BACKENDS = {
    "smtp":      _send_smtp,
    "sendgrid":  _send_sendgrid,
    "ses":       _send_ses,
}


def send_report(html: str, subject: str | None = None) -> None:
    """
    Deliver `html` as an email report.
    Provider is selected by EMAIL_PROVIDER in .env (smtp | sendgrid | ses).
    """
    provider = os.getenv("EMAIL_PROVIDER", "smtp").lower().strip()
    if provider not in _BACKENDS:
        raise ValueError(
            f"Unknown EMAIL_PROVIDER '{provider}'. Choose from: {', '.join(_BACKENDS)}"
        )

    subject = subject or _default_subject()
    _BACKENDS[provider](subject, html)


# ---------------------------------------------------------------------------
# CLI dry-run — prints what would be sent without delivering
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from fetcher import fetch_all
    from analytics import run_all
    from report import build_report

    dry_run = "--dry-run" in sys.argv

    print("Fetching data…")
    raw = fetch_all()
    mom, yoy, flags = run_all(raw)
    html = build_report(mom, yoy, flags)

    if dry_run:
        subject = _default_subject()
        print(f"\n[dry-run] Would send via {os.getenv('EMAIL_PROVIDER', 'smtp')}")
        print(f"  From:    {os.getenv('EMAIL_FROM')}")
        print(f"  To:      {os.getenv('EMAIL_TO')}")
        print(f"  Subject: {subject}")
        print(f"  Body:    {len(html):,} chars of HTML")
    else:
        send_report(html)
