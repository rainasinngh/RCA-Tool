# app/services/mailer.py

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import (
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_USE_TLS,
    MAIL_FROM,
    MAIL_TO,
)

logger = logging.getLogger(__name__)


def send_rca_report(subject: str, html_body: str, to: list | None = None) -> bool:
    """
    Send an RCA report over SMTP.

    Returns True on success, False on failure (never raises — a failed
    email should not crash the window-processing pipeline; the report
    itself is still persisted in the DB either way).
    """

    recipients = to or MAIL_TO

    if not SMTP_HOST:
        logger.warning(
            "SMTP_HOST not configured — skipping email send. "
            "Set SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/MAIL_TO in your .env"
        )
        return False

    if not recipients:
        logger.warning("No MAIL_TO recipients configured — skipping email send.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM or SMTP_USERNAME
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USERNAME and SMTP_PASSWORD:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(msg["From"], recipients, msg.as_string())

        logger.info("RCA report emailed to %s", recipients)
        return True

    except Exception:
        logger.exception("Failed to send RCA report email")
        return False
