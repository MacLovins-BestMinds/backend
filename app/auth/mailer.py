"""Письмо с кодом подтверждения почты. Без настроенного SMTP письмо не уходит — код пишется в лог сервера."""

import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


def mail_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def send_code(email: str, nick: str, code: str) -> bool:
    """True — письмо отправлено. False — почта не настроена или не ответила (код остаётся в логе сервера)."""
    if not mail_configured():
        logger.warning("Почта не настроена (SMTP_HOST, SMTP_FROM): код подтверждения для %s — %s", email, code)
        return False
    message = EmailMessage()
    message["Subject"] = f"{code} — your Stager code"
    message["From"] = settings.SMTP_FROM
    message["To"] = email
    message.set_content(
        f"Hi {nick},\n\nYour Stager confirmation code is {code}.\n"
        f"It works for {settings.EMAIL_CODE_TTL_MIN} minutes.\n\nIf you did not sign up, ignore this email."
    )
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        logger.exception("Не удалось отправить письмо с кодом на %s", email)
        return False
