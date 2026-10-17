"""Password-reset email delivery with an explicit local-development fallback."""
from email.message import EmailMessage
import smtplib
import ssl

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("email")


def send_password_reset_email(recipient: str, reset_url: str) -> str:
    """Send reset mail or print a one-time link in development when SMTP is absent.

    The development fallback is intentionally restricted to ENV=development. It
    allows local password recovery without making production appear to send mail
    when no mail provider has been configured.
    """
    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        if settings.ENV != "development":
            raise RuntimeError("Password reset email is not configured.")
        print(f"[DEV ONLY] Password reset link: {reset_url}", flush=True)
        return "development_log"

    message = EmailMessage()
    message["Subject"] = "Reset your VoxAI password"
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = recipient
    message.set_content(
        "We received a request to reset your VoxAI password. "
        f"Use this link within {settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES} minutes:\n\n"
        f"{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS) as smtp:
        if settings.SMTP_USE_STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)
    return "email"
