import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from .settings import SMTP_HOST, SMTP_PORT, SMTP_SECURITY, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM, SMTP_TO


def send_email_sync(
    title,
    message,
    settings=None,
):
    settings = settings or {}
    if not settings.get("enabled", True):
        raise RuntimeError("Gateway is disabled")
    smtp_host = settings.get('host', SMTP_HOST)
    smtp_port = settings.get('port', SMTP_PORT)
    smtp_security = settings.get('security', SMTP_SECURITY)
    smtp_username = settings.get('username', SMTP_USERNAME)
    smtp_password = settings.get('password', SMTP_PASSWORD)
    smtp_from = settings.get('sender', SMTP_FROM)
    smtp_to = settings.get('recipient', SMTP_TO)


    if not smtp_host:
        raise RuntimeError(
            "smtp_host is not configured"
        )

    if not smtp_to:
        raise RuntimeError(
            "smtp_to is not configured"
        )

    mail = EmailMessage()

    mail["Subject"] = title
    mail["From"] = smtp_from
    mail["To"] = smtp_to

    mail.set_content(message)

    context = ssl.create_default_context()

    if smtp_security == "ssl":

        with smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            timeout=10,
            context=context,
        ) as smtp:

            if smtp_username:
                smtp.login(
                    smtp_username,
                    smtp_password,
                )

            smtp.send_message(mail)

    else:

        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=10,
        ) as smtp:

            smtp.ehlo()

            if smtp_security == "starttls":
                smtp.starttls(
                    context=context
                )
                smtp.ehlo()

            if smtp_username:
                smtp.login(
                    smtp_username,
                    smtp_password,
                )

            smtp.send_message(mail)


async def send_email(
    title,
    message,
    settings=None,
):

    await asyncio.to_thread(
        send_email_sync,
        title,
        message,
        settings,
    )
