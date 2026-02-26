"""
OAuth 2.0 flow for Close CRM.
Close is the only way to authenticate — no passwords, no other providers.
"""

import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    request,
    session,
    url_for,
)

from app import db
from app.models import User

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

CLOSE_API_BASE = "https://api.close.com/api/v1"


@auth_bp.route("/close")
def close_login():
    """Redirect the user to Close's OAuth authorization page."""
    # Clear any stale session data (e.g. from a previous failed attempt)
    session.clear()
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session.modified = True
    params = {
        "response_type": "code",
        "client_id": current_app.config["CLOSE_CLIENT_ID"],
        "redirect_uri": current_app.config["CLOSE_REDIRECT_URI"],
        "state": state,
    }
    return redirect(f"{current_app.config['CLOSE_AUTH_URL']}?{urlencode(params)}")


@auth_bp.route("/close/callback")
def close_callback():
    """Handle the OAuth callback from Close."""
    error = request.args.get("error")
    if error:
        flash(f"Close returned an error: {error}", "error")
        return redirect(url_for("main.index"))

    code = request.args.get("code")
    returned_state = request.args.get("state")

    if not code:
        flash("No authorization code received from Close.", "error")
        return redirect(url_for("main.index"))

    if returned_state != session.pop("oauth_state", None):
        flash("Invalid OAuth state parameter. Please try again.", "error")
        return redirect(url_for("main.index"))

    # Exchange authorization code for tokens
    token_resp = requests.post(
        current_app.config["CLOSE_TOKEN_URL"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": current_app.config["CLOSE_REDIRECT_URI"],
            "client_id": current_app.config["CLOSE_CLIENT_ID"],
            "client_secret": current_app.config["CLOSE_CLIENT_SECRET"],
        },
        timeout=30,
    )
    if not token_resp.ok:
        flash(f"Failed to obtain access token from Close: {token_resp.text}", "error")
        return redirect(url_for("main.index"))

    token_data = token_resp.json()
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    # Fetch the authenticated user's profile
    me_resp = requests.get(
        f"{CLOSE_API_BASE}/me/",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if not me_resp.ok:
        flash("Could not retrieve your Close profile. Please try again.", "error")
        return redirect(url_for("main.index"))

    me = me_resp.json()
    close_user_id = me["id"]

    # Upsert — create on first login, update tokens on subsequent logins
    user = User.query.filter_by(close_user_id=close_user_id).first()
    if not user:
        user = User(close_user_id=close_user_id)
        db.session.add(user)

    user.close_org_id = me.get("organization_id")
    first = me.get("first_name", "")
    last = me.get("last_name", "")
    user.name = me.get("display_name") or f"{first} {last}".strip() or me.get("email")
    user.email = me.get("email")
    user.access_token = access_token
    user.refresh_token = refresh_token
    user.token_expires_at = expires_at
    db.session.commit()

    session["user_id"] = user.id
    return redirect(url_for("main.dashboard"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))
