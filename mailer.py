"""Sends the password-reset email via blocking/synchronous smtplib - the
only email this backend ever sends. config.SMTP_HOST blank (the default)
disables real sending entirely: the reset link is logged instead, which
keeps the whole password-reset flow functional in local dev/CI without
any SMTP server, at the cost of an operator needing to grep logs instead
of receiving an email. See README's "Password reset" section for setting
up a real SMTP provider.
"""

import logging
import smtplib
from email.message import EmailMessage

import config

logger = logging.getLogger("stc.mailer")


def send_password_reset_email(to_address: str, reset_link: str) -> None:
    """Send (or, with SMTP_HOST blank, log) a password-reset email
    containing reset_link. Never raises - blueprints/auth.py's
    request-reset route returns the same response whether or not the
    account/email exists (enumeration-safe), so a mail-server outage
    must not turn into a 500 that would give an enumeration attacker a
    way to distinguish "sent" from "server broke" anyway; there's also no
    way for the caller to react differently to a delivery failure since
    the response is already fixed regardless."""
    if not config.SMTP_HOST:
        logger.info(
            "password reset link (SMTP_HOST unset, not emailed)",
            extra={"to": to_address, "reset_link": reset_link},
        )
        return

    message = EmailMessage()
    message["Subject"] = "Reset your password"
    message["From"] = config.SMTP_FROM or config.SMTP_USERNAME
    message["To"] = to_address
    message.set_content(
        "A password reset was requested for your account.\n\n"
        f"Reset it here: {reset_link}\n\n"
        f"This link expires in {config.PASSWORD_RESET_EXPIRY_MINUTES} minutes. "
        "If you didn't request this, you can ignore this email - your "
        "password won't change unless this link is used."
    )

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
            if config.SMTP_USE_TLS:
                smtp.starttls()
            if config.SMTP_USERNAME:
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError):
        logger.exception("failed to send password reset email", extra={"to": to_address})
