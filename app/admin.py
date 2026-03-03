"""
Admin blueprint — accessible only to whitelisted email addresses.

Access is controlled by the ADMIN_EMAILS config value (comma-separated list).
Any logged-in user whose Close email is not in that list gets a 403.
"""

import json
from datetime import timezone
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    session,
    url_for,
)
from sqlalchemy import func

from app import db
from app.models import Sync, SyncEvent, User

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _admin_emails():
    raw = current_app.config.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return redirect(url_for("main.index"))
        user = User.query.get(uid)
        if not user or user.email.lower() not in _admin_emails():
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Dashboard — summary stats
# ---------------------------------------------------------------------------


@admin_bp.route("/")
@admin_required
def dashboard():
    total_users = User.query.count()
    total_syncs = Sync.query.count()
    active_syncs = Sync.query.filter_by(status="active").count()
    error_syncs = Sync.query.filter_by(status="error").count()
    total_records = db.session.query(func.sum(Sync.total_records)).scalar() or 0
    recent_events = (
        SyncEvent.query
        .order_by(SyncEvent.created_at.desc())
        .limit(15)
        .all()
    )
    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        total_syncs=total_syncs,
        active_syncs=active_syncs,
        error_syncs=error_syncs,
        total_records=total_records,
        recent_events=recent_events,
    )


# ---------------------------------------------------------------------------
# Users table
# ---------------------------------------------------------------------------


@admin_bp.route("/users")
@admin_required
def users():
    users = User.query.order_by(User.created_at.desc()).all()
    sync_counts = dict(
        db.session.query(Sync.user_id, func.count(Sync.id))
        .group_by(Sync.user_id)
        .all()
    )
    return render_template("admin/users.html", users=users, sync_counts=sync_counts)


# ---------------------------------------------------------------------------
# Syncs table
# ---------------------------------------------------------------------------


@admin_bp.route("/syncs")
@admin_required
def syncs():
    syncs = (
        Sync.query
        .join(User)
        .order_by(Sync.created_at.desc())
        .all()
    )
    return render_template("admin/syncs.html", syncs=syncs)


# ---------------------------------------------------------------------------
# Per-sync activity log
# ---------------------------------------------------------------------------


@admin_bp.route("/syncs/<sync_id>/log")
@admin_required
def sync_log(sync_id):
    sync = Sync.query.get_or_404(sync_id)
    events = (
        SyncEvent.query
        .filter_by(sync_id=sync_id)
        .order_by(SyncEvent.created_at.desc())
        .all()
    )
    close_query_pretty = json.dumps(sync.close_query, indent=2)
    created_ts = int(sync.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    last_polled_ts = (
        int(sync.last_polled_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        if sync.last_polled_at else None
    )
    return render_template(
        "admin/sync_log.html",
        sync=sync,
        events=events,
        close_query_pretty=close_query_pretty,
        created_ts=created_ts,
        last_polled_ts=last_polled_ts,
    )


# ---------------------------------------------------------------------------
# Admin actions (JSON API)
# ---------------------------------------------------------------------------


@admin_bp.route("/api/syncs/<sync_id>/poll", methods=["POST"])
@admin_required
def admin_trigger_poll(sync_id):
    sync = Sync.query.get_or_404(sync_id)
    if sync.status != "active":
        return jsonify({"error": f"Cannot poll — sync status is '{sync.status}'."}), 409
    from app.scheduler import trigger_poll
    trigger_poll(current_app._get_current_object(), sync.id)
    return jsonify({"ok": True})


@admin_bp.route("/api/syncs/<sync_id>/reset", methods=["POST"])
@admin_required
def admin_reset_sync(sync_id):
    sync = Sync.query.get_or_404(sync_id)
    if sync.status != "error":
        return jsonify({"error": "Only errored syncs can be reset."}), 409
    sync.status = "active"
    sync.error_message = None
    db.session.add(SyncEvent(
        sync_id=sync_id,
        event_type="admin_reset",
        message="Status manually reset from error → active by admin.",
    ))
    db.session.commit()
    return jsonify({"ok": True})
