"""
Run Flask-Migrate upgrade — called by Fly.io as a release command
so the database schema is always up to date before the new app version starts.
"""
from flask_migrate import upgrade
from app import create_app

app = create_app()
with app.app_context():
    upgrade()
