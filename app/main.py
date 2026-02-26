"""
Main application routes: dashboard, sync CRUD, and the progress API endpoint.
"""

import json
from datetime import timezone
from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app import db
from app.models import Sync, User

main_bp = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("main.index"))
        return f(*args, **kwargs)

    return decorated


def get_current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@main_bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    return render_template("index.html")


@main_bp.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    syncs = user.syncs.order_by(Sync.created_at.desc()).all()
    return render_template("dashboard.html", user=user, syncs=syncs)


@main_bp.route("/syncs/new", methods=["GET", "POST"])
@login_required
def new_sync():
    user = get_current_user()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        entity_type = request.form.get("entity_type", "").strip()
        clay_webhook_url = request.form.get("clay_webhook_url", "").strip()
        filter_json_raw = request.form.get("close_query", "").strip()

        errors = []

        if not name:
            errors.append("Sync name is required.")

        if entity_type not in ("lead", "contact"):
            errors.append('Entity type must be "Lead" or "Contact".')

        if not clay_webhook_url or not clay_webhook_url.startswith("http"):
            errors.append("A valid Clay webhook URL is required.")

        close_query = None
        if not filter_json_raw:
            errors.append("Filter JSON is required.")
        else:
            try:
                close_query = json.loads(filter_json_raw)
                if not isinstance(close_query, dict) or "query" not in close_query:
                    errors.append('Filter JSON must be an object containing a "query" key.')
            except json.JSONDecodeError as exc:
                errors.append(f"Invalid JSON: {exc}")

        if errors:
            for msg in errors:
                flash(msg, "error")
            return render_template("syncs/new.html", user=user, form_data=request.form)

        sync = Sync(
            user_id=user.id,
            name=name,
            entity_type=entity_type,
            clay_webhook_url=clay_webhook_url,
            close_query=close_query,
            status="pending",
        )
        db.session.add(sync)
        db.session.commit()

        # Kick off the initial sync in a background job
        from app.scheduler import trigger_initial_sync

        trigger_initial_sync(current_app._get_current_object(), sync.id)

        return redirect(url_for("main.sync_detail", sync_id=sync.id))

    return render_template("syncs/new.html", user=user, form_data={})


@main_bp.route("/syncs/<sync_id>")
@login_required
def sync_detail(sync_id):
    user = get_current_user()
    sync = Sync.query.filter_by(id=sync_id, user_id=user.id).first_or_404()
    last_polled_ts = (
        int(sync.last_polled_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        if sync.last_polled_at else None
    )
    created_ts = int(sync.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return render_template("syncs/detail.html", user=user, sync=sync,
                           last_polled_ts=last_polled_ts, created_ts=created_ts)


@main_bp.route("/syncs/<sync_id>/delete", methods=["POST"])
@login_required
def delete_sync(sync_id):
    user = get_current_user()
    sync = Sync.query.filter_by(id=sync_id, user_id=user.id).first_or_404()
    name = sync.name
    db.session.delete(sync)
    db.session.commit()
    flash(f'Sync "{name}" has been deleted.', "success")
    return redirect(url_for("main.dashboard"))


# ---------------------------------------------------------------------------
# JSON API — polled by the sync detail page during initial sync
# ---------------------------------------------------------------------------


@main_bp.route("/api/syncs/<sync_id>/progress")
@login_required
def sync_progress(sync_id):
    user = get_current_user()
    sync = Sync.query.filter_by(id=sync_id, user_id=user.id).first_or_404()
    return jsonify(
        {
            "status": sync.status,
            "total_records": sync.total_records or 0,
            "synced_records": sync.synced_records or 0,
            "error_message": sync.error_message,
            "last_polled_at": (
                sync.last_polled_at.strftime("%b %d, %Y at %I:%M %p UTC")
                if sync.last_polled_at
                else None
            ),
            # Unix ms timestamp for JS local-time formatting + poll completion detection.
            # .replace(tzinfo=utc) is required because datetime.utcnow() produces a
            # naive datetime and Python would otherwise assume it's local time.
            "last_polled_at_ts": (
                int(sync.last_polled_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
                if sync.last_polled_at else None
            ),
        }
    )


@main_bp.route("/api/syncs/<sync_id>/poll", methods=["POST"])
@login_required
def trigger_poll_now(sync_id):
    user = get_current_user()
    sync = Sync.query.filter_by(id=sync_id, user_id=user.id).first_or_404()

    if sync.status != "active":
        return jsonify(
            {"error": f"Cannot poll — sync is not active (status: {sync.status})."}
        ), 409

    from app.scheduler import trigger_poll

    trigger_poll(current_app._get_current_object(), sync.id)
    return jsonify({"ok": True})
