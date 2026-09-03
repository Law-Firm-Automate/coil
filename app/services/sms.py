"""Two-way SMS via Twilio REST API (no SDK dependency)."""
import requests
from flask import current_app


def configured():
    c = current_app.config
    return bool(c.get("TWILIO_ACCOUNT_SID") and c.get("TWILIO_AUTH_TOKEN") and c.get("TWILIO_FROM_NUMBER"))


def send_sms(to, body):
    """Returns (provider_id, status). When Twilio is not configured, returns ('', 'unconfigured')."""
    if not configured():
        current_app.logger.info("[SMS-DEV] to=%s body=%s", to, body)
        return "", "unconfigured"
    c = current_app.config
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{c['TWILIO_ACCOUNT_SID']}/Messages.json",
        auth=(c["TWILIO_ACCOUNT_SID"], c["TWILIO_AUTH_TOKEN"]),
        data={"To": to, "From": c["TWILIO_FROM_NUMBER"], "Body": body}, timeout=20)
    if r.status_code >= 300:
        return "", f"error:{r.status_code}"
    j = r.json()
    return j.get("sid", ""), j.get("status", "queued")
