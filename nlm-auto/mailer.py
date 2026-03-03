"""Gmail failure-notification helper."""

import logging
import smtplib
import traceback
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def send_failure(config: dict, subject: str, exc: Exception) -> None:
    """Send a failure email via Gmail App Password."""
    gmail_address = config.get("gmail_address", "")
    app_password = config.get("gmail_app_password", "")

    if not gmail_address or not app_password or "xxxx" in app_password:
        log.warning("Gmail not configured — skipping failure email.")
        return

    body = (
        f"NLM Automation App encountered an error.\n\n"
        f"Subject: {subject}\n\n"
        f"Error: {exc}\n\n"
        f"Traceback:\n{traceback.format_exc()}"
    )

    msg = MIMEText(body)
    msg["Subject"] = f"[NLM-Auto] FAILURE: {subject}"
    msg["From"] = gmail_address
    msg["To"] = gmail_address

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_address, app_password.replace(" ", ""))
            smtp.send_message(msg)
        log.info("Failure email sent: %s", subject)
    except Exception as mail_exc:
        log.error("Could not send failure email: %s", mail_exc)
