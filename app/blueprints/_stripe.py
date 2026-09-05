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


# ---- cards on file and off-session charges (Agent P, money.py) ----
class StripeNotConfigured(RuntimeError):
    """Raised by the card-on-file helpers when STRIPE_SECRET_KEY is blank, so callers can show a plain message."""


NOT_CONFIGURED_MSG = "Online payments are not configured. Set STRIPE_SECRET_KEY on the server to use cards on file."


def _require():
    if not configured():
        raise StripeNotConfigured(NOT_CONFIGURED_MSG)
    return _sdk()


def create_customer(email="", name="", metadata=None):
    """Stripe Customer for a contact. Returns the Customer object (has .id)."""
    return _require().Customer.create(email=email or None, name=name or None, metadata=metadata or {})


def create_setup_session(customer_id, success_url, cancel_url, metadata=None):
    """Checkout Session in setup mode: collects a card and attaches it to the customer for later off-session
    charges. Returns the Session object (has .url and .id)."""
    return _require().checkout.Session.create(
        mode="setup", customer=customer_id, payment_method_types=["card"],
        success_url=success_url, cancel_url=cancel_url, metadata=metadata or {})


def retrieve_setup_session(session_id):
    """Setup-mode session with the SetupIntent and its payment method expanded, so brand and last4 are on hand."""
    return _require().checkout.Session.retrieve(session_id, expand=["setup_intent.payment_method"])


def retrieve_setup_intent(setup_intent_id):
    return _require().SetupIntent.retrieve(setup_intent_id, expand=["payment_method"])


def retrieve_payment_method(payment_method_id):
    return _require().PaymentMethod.retrieve(payment_method_id)


def detach_payment_method(payment_method_id):
    """Best effort: detach the card from the customer at Stripe when staff remove it. Errors are for the caller."""
    return _require().PaymentMethod.detach(payment_method_id)


def charge_payment_method(customer_id, payment_method_id, amount_cents, description="", metadata=None,
                          idempotency_key=None):
    """Charge a saved card without the client present. Returns the PaymentIntent (has .id and .status).
    amount_cents is the integer total to take, surcharge included. Raises stripe.error.CardError on a decline."""
    params = dict(amount=int(amount_cents), currency="usd", customer=customer_id, payment_method=payment_method_id,
                  off_session=True, confirm=True, description=description or None, metadata=metadata or {})
    if idempotency_key:
        params["idempotency_key"] = idempotency_key
    return _require().PaymentIntent.create(**params)
