import os
import logging
from flask import Flask, render_template
from .config import ProductionConfig, DATA_DIR
from .extensions import db
from .helpers import register_template_globals, check_csrf


def create_app(config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(ProductionConfig)
    # DATABASE_URL is read again here so a test that sets it after app.config was imported still gets its own DB.
    if os.environ.get("DATABASE_URL"):
        app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
    if config:
        app.config.update(config)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    os.makedirs(app.config["PDF_DIR"], exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    db.init_app(app)
    register_template_globals(app)
    app.before_request(check_csrf)
    from .permissions import enforce; app.before_request(enforce)

    # Add security headers in production
    @app.after_request
    def add_security_headers(response):
        for header, value in ProductionConfig.SECURITY_HEADERS.items():
            response.headers[header] = value
        return response

    if app.config.get("COIL_QA_HEADERS"):
        _expose_flashes(app)

    from .blueprints import auth, dashboard
    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    # Feature blueprints, registered as they land. Each module exposes `bp`.
    #
    # Only list modules that exist. A name here that has no file logs a warning on every
    # startup AND every CLI run, and six such names were burying the warnings that matter.
    # The try/except below stays because it makes a partial checkout survivable, not
    # because this list is a wishlist.
    for modname in ("contacts", "matters", "conflicts", "tasks", "calendar", "documents",
                    "time", "invoices", "reports",
                    "trust", "payments", "portal",
                    "intake", "engagements", "messages", "settings", "exports", "signatures",
                    "rules", "doctemplates", "emailin", "accounting", "api", "webhooks_out", "ai",
                    "statements", "research", "pi", "features", "records", "discovery", "caseaudit",
                    "money", "criminal", "capture",
                    "importer", "voice", "feedback", "setupguide"):
        try:
            mod = __import__(f"app.blueprints.{modname}", fromlist=["bp"])
            app.register_blueprint(mod.bp)
        except ModuleNotFoundError as e:
            if f"app.blueprints.{modname}" in str(e):
                app.logger.warning("blueprint %s not present yet", modname)
            else:
                raise

    with app.app_context():
        from . import models  # noqa
        db.create_all()
        from .migrate import add_missing_columns
        add_missing_columns()

    @app.errorhandler(404)
    def nf(e):
        return render_template("error.html", code=404, message="Not found"), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="Not allowed"), 403

    @app.route("/health")
    def health():
        """What the updater polls after a restart, so it must not depend on the database.

        `app` is closed over rather than imported as current_app: this route used to
        reference current_app without importing it, so every call raised NameError and
        returned 500. Nothing noticed, because nothing was checking.
        """
        return {
            "status": "healthy",
            "version": app.config.get("COIL_VERSION", "unknown"),
            "commit": app.config.get("COIL_COMMIT", "unknown"),
            "channel": app.config.get("COIL_CHANNEL", "unknown"),
        }, 200

    return app

def _expose_flashes(app):
    """Repeat flashed messages in an X-Coil-Flash header. QA only; see config."""
    from flask import g, message_flashed

    def _collect(sender, message, category, **extra):
        g.setdefault("_qa_flashes", []).append(f"{category}: {message}")

    # weak=False: blinker holds receivers weakly by default, and a local closure is collected the
    # moment this function returns, leaving the signal connected to nothing.
    message_flashed.connect(_collect, app, weak=False)

    @app.after_request
    def _emit(response):
        msgs = getattr(g, "_qa_flashes", None)
        if msgs:
            # Headers are latin-1 on the wire; a client name outside that must survive.
            joined = " | ".join(msgs).replace("\r", " ").replace("\n", " ")
            response.headers["X-Coil-Flash"] = joined.encode("utf-8").decode("latin-1", "replace") \
                if joined.isascii() else joined.encode("utf-8").hex()
            response.headers["X-Coil-Flash-Encoding"] = "text" if joined.isascii() else "utf8-hex"
        return response

