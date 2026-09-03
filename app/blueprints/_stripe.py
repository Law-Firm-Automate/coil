"""Thin wrapper around the Stripe SDK. Every Stripe call in the app goes through here so tests can
monkeypatch these functions and so the API key is always read from app config at call time."""
from flask import current_app


def configured():
    return bool(current_app.config.get("STRIPE_SECRET_KEY"))


def _sdk():
    import stripe
    stripe.api_key = current_app.config.get("STRIPE_SECRET_KEY", "")
    return stripe


def create_checkout_session(**params):
    """Returns the Checkout Session object (dict-like, has .url and .id)."""
    return _sdk().checkout.Session.create(**params)


def retrieve_checkout_session(session_id):
    return _sdk().checkout.Session.retrieve(session_id)


def construct_event(payload, sig_header, secret):
    """Verify a webhook signature and return the event. Raises on a bad signature."""
    return _sdk().Webhook.construct_event(payload, sig_header, secret)


def fee_cents_for_payment_intent(payment_intent_id):
    """Processor fee (cents) for a PaymentIntent, or None when not available yet.
    One API call with an expand, so it is cheap enough to run inside the webhook."""
    if not payment_intent_id:
        return None
    pi = _sdk().PaymentIntent.retrieve(payment_intent_id, expand=["latest_charge.balance_transaction"])
    charge = pi.get("latest_charge") if hasattr(pi, "get") else None
    if not charge or isinstance(charge, str):
        return None
    bt = charge.get("balance_transaction")
    if not bt or isinstance(bt, str):
        return None
    fee = bt.get("fee")
    return int(fee) if fee is not None else None
