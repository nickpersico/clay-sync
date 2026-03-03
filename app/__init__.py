import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    from config import Config

    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)

    from app.admin import admin_bp
    from app.auth import auth_bp
    from app.main import main_bp

    app.register_blueprint(admin_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    # Import models so Flask-Migrate discovers them
    from app import models  # noqa: F401

    # Start the background scheduler only when running the web server.
    # Skip for Flask CLI commands (flask db init/migrate/upgrade, flask shell, etc.)
    import sys
    _script = os.path.basename(sys.argv[0]) if sys.argv else ""
    is_cli_command = _script == "migrate.py" or (
        len(sys.argv) > 1 and sys.argv[1] in ("db", "shell", "routes")
    )
    if not is_cli_command and (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        from app.scheduler import init_scheduler

        init_scheduler(app)

    return app
