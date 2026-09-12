import os
import secrets
import sys
from dotenv import load_dotenv

# Under pytest, do not read the developer's .env. It carries real SMTP credentials and a
# real DATABASE_URL, so loading it made the test suite authenticate against a live mail
# server and point at the production database. Tests pass what they need through the
# environment, which wins over .env anyway (load_dotenv does not override).
if "pytest" not in sys.modules:
    load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


class Config:
    """Base configuration. Self-hosted installs should use ProductionConfig."""
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'practice.db')}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
    UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
    PDF_DIR = os.path.join(DATA_DIR, "pdf")
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024

    # Email
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASS = os.environ.get("SMTP_PASS", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "billing@example.com")

    # Payments
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # Voice / SMS
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

    # Install key verification
    COIL_KEY_VERIFY_URL = os.environ.get("COIL_KEY_VERIFY_URL", "https://coil.legal/api/coil-verify-key")
    COIL_SKIP_INSTALL_KEY = os.environ.get("COIL_SKIP_INSTALL_KEY", "") in ("1", "true", "yes")
    # Injected by the image build. "dev" means a source checkout or a hand-built image,
    # which is a useful thing to see in a bug report rather than a fake version number.
    COIL_VERSION = os.environ.get("COIL_VERSION", "dev")
    COIL_COMMIT = os.environ.get("COIL_COMMIT", "unknown")
    COIL_CHANNEL = os.environ.get("COIL_CHANNEL", "stable")
    # In-app feedback. Set FEEDBACK_ENABLED=0 to remove the button and the route; some
    # firms will have a policy about anything leaving their server, and that is fine.
    COIL_HOSTING = os.environ.get("COIL_HOSTING", "self-hosted")
    COIL_FEEDBACK_URL = os.environ.get("COIL_FEEDBACK_URL", "https://coil.legal/api/coil-feedback")
    FEEDBACK_ENABLED = os.environ.get("FEEDBACK_ENABLED", "1")
    # Repeat every flashed message in an X-Coil-Flash response header. For automated QA
    # only: a browser-driving tester repeatedly reported "no message shown" on refusals
    # that were on the page, because it read the DOM before or after the flash region
    # rendered. The header is on the response that flashed, redirect or not, so nothing
    # has to be scraped. Off by default because flashes name clients and amounts and
    # headers get logged by proxies.
    COIL_QA_HEADERS = os.environ.get("COIL_QA_HEADERS", "0") == "1"

    # Email filing
    IMAP_HOST = os.environ.get("IMAP_HOST", "")
    IMAP_PORT = int(os.environ.get("IMAP_PORT", "993") or 993)
    IMAP_USER = os.environ.get("IMAP_USER", "")
    IMAP_PASS = os.environ.get("IMAP_PASS", "")
    IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX") or "INBOX"

    # AI
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")
    AI_OPENROUTER_MODEL = os.environ.get("AI_OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
    # OpenRouter routes to whichever provider is cheapest or fastest unless told otherwise,
    # and its default permits providers that may store and train on prompts. Coil's prompts
    # carry matter content, so the default here is the restrictive one: zero-retention
    # endpoints only, and no provider that collects data. Set either to 0 to lift it, which
    # widens the pool of models that will answer at the cost of that guarantee.
    AI_OPENROUTER_ZDR = os.environ.get("AI_OPENROUTER_ZDR", "1")
    AI_OPENROUTER_NO_TRAINING = os.environ.get("AI_OPENROUTER_NO_TRAINING", "1")
    AI_DAILY_CAP_CENTS = int(os.environ.get("AI_DAILY_CAP_CENTS", "300") or 300)
    LLM_ENABLED = os.environ.get("LLM_ENABLED", "true")
    LLM_DAILY_CAP = int(os.environ.get("LLM_DAILY_CAP", "0") or 0)
    BOOKING_URL = os.environ.get("BOOKING_URL", "")

    # Research
    COURTLISTENER_TOKEN = os.environ.get("COURTLISTENER_TOKEN", "")


class ProductionConfig(Config):
    """Production settings for self-hosted deployments."""
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 31 * 24 * 60 * 60  # 31 days

    # Security headers
    SECURITY_HEADERS = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self';",
    }
