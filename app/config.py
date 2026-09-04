import os
from dotenv import load_dotenv
load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'practice.db')}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5055").rstrip("/")
    UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
    PDF_DIR = os.path.join(DATA_DIR, "pdf")
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASS = os.environ.get("SMTP_PASS", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "billing@example.com")
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
    # Install key. Self-hosted installs register once against lawfirmautomate.com at first-run setup.
    # Hosted instances and development set COIL_SKIP_INSTALL_KEY=1.
    COIL_KEY_VERIFY_URL = os.environ.get("COIL_KEY_VERIFY_URL", "https://lawfirmautomate.com/api/coil-verify-key")
    COIL_SKIP_INSTALL_KEY = os.environ.get("COIL_SKIP_INSTALL_KEY", "") in ("1", "true", "yes")
    COIL_VERSION = os.environ.get("COIL_VERSION", "0.3.0")
    # Email filing (python -m app.cli emailin). Leave IMAP_HOST blank to disable.
    IMAP_HOST = os.environ.get("IMAP_HOST", "")
    IMAP_PORT = int(os.environ.get("IMAP_PORT", "993") or 993)
    IMAP_USER = os.environ.get("IMAP_USER", "")
    IMAP_PASS = os.environ.get("IMAP_PASS", "")
    IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX") or "INBOX"
    # ---- AI features (Agent H). Provider precedence at call time: OPENROUTER_API_KEY, then ANTHROPIC_API_KEY,
    # else the AI pages explain that no key is set. Firm.ai_enabled must also be on. See app/llm.py.
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")  # direct Anthropic id (no date suffix)
    AI_OPENROUTER_MODEL = os.environ.get("AI_OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
    AI_DAILY_CAP_CENTS = int(os.environ.get("AI_DAILY_CAP_CENTS", "300") or 300)  # estimated spend per UTC day
    LLM_ENABLED = os.environ.get("LLM_ENABLED", "true")  # 0/false/no/off turns every model call off
    LLM_DAILY_CAP = int(os.environ.get("LLM_DAILY_CAP", "0") or 0)  # max model calls per UTC day, 0 = no limit
    BOOKING_URL = os.environ.get("BOOKING_URL", "")  # consult booking page, shown after intake and as {{ booking_url }}
