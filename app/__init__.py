import os
import logging
from flask import Flask, render_template
from .config import Config, DATA_DIR
from .extensions import db
from .helpers import register_template_globals, check_csrf


def create_app(config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)
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

    from .blueprints import auth, dashboard
    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    # Feature blueprints are registered here as they land. Each module exposes `bp`.
    for modname in ("contacts", "matters", "conflicts", "tasks", "calendar", "documents",
                    "time", "invoices", "reports",
                    "trust", "payments", "portal",
                    "intake", "engagements", "messages", "settings", "exports", "signatures",
                    "rules", "doctemplates", "emailin", "accounting", "api", "webhooks_out", "ai",
                    "statements", "research", "pi", "features", "records", "discovery", "caseaudit",
                    "booking", "questionnaires", "stages", "money", "litigation", "dockets", "pdftools", "criminal", "capture",
                    "importer"):
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

    return app
