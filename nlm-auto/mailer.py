"""Gmail failure-notification helper."""

import logging
import smtplib
import traceback
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def send_failure(config: dict, subject: str, exc: Exception) -> None:
    notif = config.get("notifications", {})
    if not notif.get("notify_on_failure", True):
        return

    address = notif.get("email", "")
    password = notif.get("gmail_app_password", "").replace(" ", "")

    if not address or not password or "xxxx" in password:
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
    msg["From"] = address
    msg["To"] = address

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(address, password)
            smtp.send_message(msg)
        log.info("Failure email sent: %s", subject)
    except Exception as mail_exc:
        log.error("Could not send failure email: %s", mail_exc)
