"""Outbound email. Uses SMTP when configured, otherwise logs the message so dev flows still work."""
import smtplib
import logging
from email.message import EmailMessage
from flask import current_app

log = logging.getLogger("mail")


def send_email(to, subject, html, text=None, attachments=None, reply_to=None):
    """attachments: list of (filename, bytes, mime). Returns True if handed to SMTP, False if logged only."""
    cfg = current_app.config
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text or "This message contains HTML content.")
    msg.add_alternative(html, subtype="html")
    for fn, data, mime in (attachments or []):
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=fn)
    if not cfg.get("SMTP_HOST"):
        log.warning("SMTP not configured. Email to %s | %s\n%s", to, subject, html[:800])
        current_app.logger.info("[MAIL-DEV] to=%s subject=%s", to, subject)
        _dev_outbox.append({"to": to, "subject": subject, "html": html})
        return False
    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=20) as s:
        s.starttls()
        if cfg.get("SMTP_USER"):
            s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        s.send_message(msg)
    return True


_dev_outbox = []  # last emails when SMTP is unset; surfaced at /dev/outbox for testing


def dev_outbox():
    return list(reversed(_dev_outbox[-50:]))
