import os
from dotenv import load_dotenv

load_dotenv()

def _db_url():
    """
    SQLAlchemy requires 'postgresql://' but Fly.io Postgres issues URLs
    starting with 'postgres://' — normalise here.
    """
    url = os.getenv("DATABASE_URL", "postgresql://localhost/closeclay")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CLOSE_CLIENT_ID = os.getenv("CLOSE_CLIENT_ID")
    CLOSE_CLIENT_SECRET = os.getenv("CLOSE_CLIENT_SECRET")
    CLOSE_REDIRECT_URI = os.getenv(
        "CLOSE_REDIRECT_URI", "http://localhost:5000/auth/close/callback"
    )

    # Session cookie — must be Lax (not Strict) so the cookie is included
    # when Close redirects back to us (a cross-site top-level navigation).
    # Set SESSION_COOKIE_SECURE=true in production (HTTPS only).
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    SESSION_COOKIE_HTTPONLY = True

    # Close OAuth endpoints
    CLOSE_AUTH_URL = "https://app.close.com/oauth2/authorize"
    CLOSE_TOKEN_URL = "https://api.close.com/oauth2/token"
    CLOSE_API_BASE = "https://api.close.com/api/v1"

    # Hard cap matching Clay's 50,000-row workbook limit.
    # The Close API limits each cursor session to 10k records, but we batch
    # across sessions using date_created range windows — so the only real
    # ceiling is Clay's 50k row limit.
    MAX_SYNC_RECORDS = 50_000
