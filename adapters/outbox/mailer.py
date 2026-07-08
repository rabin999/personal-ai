"""Welcome-email sender over Gmail SMTP via fastapi-mail (brief §4/§5).

No hand-rolled smtplib — fastapi-mail handles async STARTTLS delivery. Gmail
requires an APP PASSWORD (2FA enabled); the normal account password will not
work. When MAIL_* is unconfigured the mailer is disabled and the worker marks
outbox records ``skipped`` (signup still succeeds).
"""

import logging

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from config.settings import Settings

logger = logging.getLogger(__name__)


def _connection_config(settings: Settings) -> ConnectionConfig:
    return ConnectionConfig(
        MAIL_USERNAME=settings.mail_username,
        MAIL_PASSWORD=settings.mail_password,
        MAIL_FROM=settings.mail_from or settings.mail_username,
        MAIL_FROM_NAME=settings.mail_from_name,
        MAIL_PORT=settings.mail_port,
        MAIL_SERVER=settings.mail_server,
        MAIL_STARTTLS=settings.mail_starttls,
        MAIL_SSL_TLS=settings.mail_ssl_tls,
        USE_CREDENTIALS=True,
        VALIDATE_CERTS=True,
    )


class WelcomeMailer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Disabled unless real Gmail credentials are present (brief §6).
        self._enabled = bool(settings.mail_username and settings.mail_password)
        self._fastmail = FastMail(_connection_config(settings)) if self._enabled else None
        # Make the disabled state LOUD (F16): "I didn't get a welcome email" is
        # otherwise a silent mystery — signup still succeeds and the outbox record is
        # marked 'skipped' with no obvious signal. Warn once, at construction, in
        # BOTH the API and worker processes (both build this via the composition root).
        if self._enabled:
            logger.info(
                "welcome mailer enabled (from %s)", settings.mail_from or settings.mail_username
            )
        else:
            logger.warning(
                "welcome emails are DISABLED — set MAIL_USERNAME + MAIL_PASSWORD "
                "(a Google App Password, not your login password) in .env to send them. "
                "Signups still succeed; welcome outbox records are marked 'skipped'."
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_welcome(self, email: str, name: str | None) -> None:
        if self._fastmail is None:
            raise RuntimeError("mailer disabled — MAIL_USERNAME/MAIL_PASSWORD not set")
        message = MessageSchema(
            subject="Hey — your companion is ready",
            recipients=[email],
            body=welcome_html(name),
            subtype=MessageType.html,
        )
        await self._fastmail.send_message(message)
        logger.info("welcome email sent to %s", email)


def welcome_html(name: str | None) -> str:
    """Warm, short welcome in the companion's voice — no marketing bloat (brief §5)."""
    greeting = f"Hi {name.split()[0]}," if name else "Hi there,"
    font = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
    body_style = f"margin:0;background:#f8fafc;font-family:{font};color:#0f172a;"
    return f"""\
<!doctype html>
<html>
  <body style="{body_style}">
    <div style="max-width:520px;margin:0 auto;padding:32px 24px;">
      <p style="font-size:18px;font-weight:600;margin:0 0 16px;">{greeting}</p>
      <p style="font-size:15px;line-height:1.6;margin:0 0 14px;">
        I'm your companion — really glad you're here. I'm a voice-first friend you
        can just talk to, and I actually remember you across our conversations.
      </p>
      <p style="font-size:15px;line-height:1.6;margin:0 0 14px;">
        I pick up on how you're feeling and meet you there, I can look things up
        when you're curious, and I can help you keep track of the projects and
        goals you care about. Everything you share stays private to you.
      </p>
      <p style="font-size:15px;line-height:1.6;margin:0 0 20px;">
        Whenever you're ready, just open the app and start talking. I'm listening.
      </p>
      <p style="font-size:14px;color:#64748b;margin:0;">— your companion</p>
    </div>
  </body>
</html>"""
