"""
Core sync logic: initial sync and hourly change-detection poll.

Both functions are designed to run inside a background thread with their
own Flask application context (provided by the caller / scheduler).

Two-step fetch strategy
-----------------------
Close's /data/search/ endpoint returns only id + __object_type by default.
We use it only to collect lightweight stubs (id + date_updated) and then
fetch the full record via GET /lead/{id}/ or /contact/{id}/.
"""

import logging
from datetime import datetime

from app import db
from app.models import Sync, SyncEvent, SyncRecord
from app.close_client import CloseClient, CloseAPIError
from app.clay_client import ClayClient, ClayWebhookError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _close_client_for_sync(sync, app_config):
    return CloseClient(
        access_token=sync.user.access_token,
        refresh_token=sync.user.refresh_token,
        token_expires_at=sync.user.token_expires_at,
        client_id=app_config["CLOSE_CLIENT_ID"],
        client_secret=app_config["CLOSE_CLIENT_SECRET"],
        user_id=sync.user.id,
    )


def _log_event(sync_id, event_type, message=None,
               records_added=0, records_updated=0, records_removed=0):
    """Write a SyncEvent row. Safe to call inside an existing DB session."""
    try:
        db.session.add(SyncEvent(
            sync_id=sync_id,
            event_type=event_type,
            message=message,
            records_added=records_added,
            records_updated=records_updated,
            records_removed=records_removed,
        ))
        db.session.flush()  # include in the next commit without a separate one
    except Exception as exc:
        logger.warning("Failed to write SyncEvent (%s): %s", event_type, exc)


def _set_error(sync, message):
    sync.status = "error"
    sync.error_message = message
    _log_event(sync.id, "error", message=message)
    db.session.commit()
    logger.error("Sync %s error: %s", sync.id, message)


# ---------------------------------------------------------------------------
# Initial sync
# ---------------------------------------------------------------------------


def run_initial_sync(app, sync_id):
    """
    Fetch all matching records from Close and push them to Clay.
    Runs once, in the background, immediately after a Sync is created.

    Step 1 — collect lightweight stubs (id + date_updated) via /data/search/
    Step 2 — fetch each full record via GET /{entity_type}/{id}/
    Step 3 — POST full record to Clay and save snapshot locally
    """
    with app.app_context():
        sync = Sync.query.get(sync_id)
        if not sync:
            logger.error("run_initial_sync: sync %s not found", sync_id)
            return

        logger.info("Starting initial sync for '%s' (%s)", sync.name, sync_id)
        sync.status = "initial_syncing"
        sync.synced_records = 0
        sync.total_records = 0
        sync.error_message = None
        _log_event(sync_id, "initial_sync_started", message="Initial sync started.")
        db.session.commit()

        close = _close_client_for_sync(sync, app.config)
        clay = ClayClient(sync.clay_webhook_url)

        # --- Step 1: Collect stubs (id + date_updated) -------------------
        try:
            stubs = close.get_all_stubs(
                sync.close_query,
                sync.entity_type,
                max_records=app.config["MAX_SYNC_RECORDS"],
            )
        except ValueError as exc:
            _set_error(sync, str(exc))
            return
        except CloseAPIError as exc:
            msg = str(exc)
            if exc.status_code == 404:
                msg = "The filter query returned a 404 — it may reference a deleted resource."
            _set_error(sync, msg)
            return
        except Exception as exc:
            _set_error(sync, f"Unexpected error fetching records: {exc}")
            logger.exception("Unexpected error in run_initial_sync fetch phase")
            return

        sync.total_records = len(stubs)
        db.session.commit()
        logger.info(
            "Initial sync '%s': collected %d stubs, fetching full records…",
            sync.name,
            sync.total_records,
        )

        # --- Steps 2 + 3: Fetch full record then push to Clay ------------
        for stub in stubs:
            record_id = stub.get("id")
            if not record_id:
                continue

            # Fetch the full record from Close
            try:
                record = close.get_record(sync.entity_type, record_id)
            except CloseAPIError as exc:
                logger.warning(
                    "Could not fetch full record %s (%s): %s — skipping",
                    record_id,
                    sync.entity_type,
                    exc,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "Unexpected error fetching record %s: %s — skipping",
                    record_id,
                    exc,
                )
                continue

            payload = {"_close_id": record_id, **record}

            try:
                clay.send_record(payload)
            except ClayWebhookError as exc:
                _set_error(sync, f"Clay webhook error: {exc}")
                return

            # Upsert the local snapshot
            existing = SyncRecord.query.filter_by(
                sync_id=sync_id, close_record_id=record_id
            ).first()
            if existing:
                existing.record_data = record
                existing.is_deleted = False
                existing.synced_at = datetime.utcnow()
            else:
                db.session.add(
                    SyncRecord(
                        sync_id=sync_id,
                        close_record_id=record_id,
                        record_data=record,
                    )
                )

            sync.synced_records += 1
            # Flush progress to DB frequently so the UI stays live
            if sync.synced_records % 25 == 0:
                db.session.commit()

        sync.status = "active"
        sync.last_polled_at = datetime.utcnow()
        _log_event(
            sync_id, "initial_sync_complete",
            message=f"Initial sync complete. {sync.synced_records:,} records pushed to Clay.",
            records_added=sync.synced_records,
        )
        db.session.commit()
        logger.info(
            "Initial sync complete for '%s': %d records", sync.name, sync.synced_records
        )


# ---------------------------------------------------------------------------
# Hourly poll (change detection)
# ---------------------------------------------------------------------------


def run_poll(app, sync_id):
    """
    Detect additions, removals, and updates since the last poll and
    reflect them in the Clay workbook.

    Step 1 — collect lightweight stubs (id + date_updated) via /data/search/
    Step 2 — for new/changed records only, fetch full data via GET /{entity_type}/{id}/
    Step 3 — send to Clay (new/updated) or tombstone (removed)
    """
    with app.app_context():
        sync = Sync.query.get(sync_id)
        if not sync or sync.status != "active":
            return

        logger.info("Polling sync '%s' (%s)", sync.name, sync_id)
        close = _close_client_for_sync(sync, app.config)
        clay = ClayClient(sync.clay_webhook_url)

        # --- Step 1: Get current stubs from Close ------------------------
        try:
            stubs = close.get_all_stubs(
                sync.close_query,
                sync.entity_type,
                max_records=app.config["MAX_SYNC_RECORDS"],
            )
        except CloseAPIError as exc:
            if exc.status_code == 404:
                _set_error(
                    sync,
                    "The filter query returned a 404 — the resource may have been "
                    "deleted in Close. This sync has been stopped.",
                )
            else:
                logger.error("Close API error during poll for '%s': %s", sync.name, exc)
            return
        except ValueError as exc:
            _set_error(sync, str(exc))
            return
        except Exception as exc:
            logger.exception("Unexpected error polling '%s'", sync.name)
            return

        # Stub map: id → date_updated string (or None)
        current_map = {s["id"]: s.get("date_updated") for s in stubs if s.get("id")}
        current_ids = set(current_map)

        existing_records = SyncRecord.query.filter_by(
            sync_id=sync_id, is_deleted=False
        ).all()
        existing_map = {r.close_record_id: r for r in existing_records}
        existing_ids = set(existing_map)

        added = current_ids - existing_ids
        removed = existing_ids - current_ids
        possibly_changed = current_ids & existing_ids
        changes = 0

        # --- New records: fetch full data then push to Clay --------------
        for record_id in added:
            try:
                record = close.get_record(sync.entity_type, record_id)
            except Exception as exc:
                logger.warning("Could not fetch new record %s: %s — skipping", record_id, exc)
                continue

            try:
                clay.send_record({"_close_id": record_id, **record})
                db.session.add(
                    SyncRecord(
                        sync_id=sync_id,
                        close_record_id=record_id,
                        record_data=record,
                    )
                )
                changes += 1
            except ClayWebhookError as exc:
                logger.error("Clay error adding %s: %s", record_id, exc)

        # --- Removed records → tombstone (use stored data) ---------------
        for record_id in removed:
            sr = existing_map[record_id]
            try:
                clay.tombstone_record({"_close_id": record_id, **(sr.record_data or {})})
                sr.is_deleted = True
                changes += 1
            except ClayWebhookError as exc:
                logger.error("Clay error tombstoning %s: %s", record_id, exc)

        # --- Changed records: compare date_updated, fetch full if stale --
        for record_id in possibly_changed:
            cur_updated = current_map[record_id]
            sr = existing_map[record_id]
            prev_updated = (sr.record_data or {}).get("date_updated")

            # Skip if date_updated hasn't changed
            if cur_updated and prev_updated and cur_updated == prev_updated:
                continue

            # Fetch full record and do a deep comparison as a safety net
            try:
                record = close.get_record(sync.entity_type, record_id)
            except Exception as exc:
                logger.warning(
                    "Could not fetch updated record %s: %s — skipping", record_id, exc
                )
                continue

            if record == (sr.record_data or {}):
                continue  # identical despite different date_updated (edge case)

            try:
                clay.send_record({"_close_id": record_id, **record})
                sr.record_data = record
                sr.synced_at = datetime.utcnow()
                changes += 1
            except ClayWebhookError as exc:
                logger.error("Clay error updating %s: %s", record_id, exc)

        sync.last_polled_at = datetime.utcnow()
        sync.total_records = len(current_ids)
        if changes > 0:
            parts = []
            if added:
                parts.append(f"{len(added)} added")
            if removed:
                parts.append(f"{len(removed)} removed")
            updated_count = changes - len(added) - len(removed)
            if updated_count > 0:
                parts.append(f"{updated_count} updated")
            _log_event(
                sync_id, "poll_complete",
                message=f"Poll complete: {', '.join(parts)}.",
                records_added=len(added),
                records_updated=max(updated_count, 0),
                records_removed=len(removed),
            )
        db.session.commit()
        logger.info(
            "Poll complete for '%s': %d change(s)", sync.name, changes
        )


# ---------------------------------------------------------------------------
# Batch runner (called by the hourly scheduler)
# ---------------------------------------------------------------------------


def run_all_polls(app):
    """Poll every active sync. Called once per hour by the scheduler."""
    with app.app_context():
        active_ids = [s.id for s in Sync.query.filter_by(status="active").all()]

    for sync_id in active_ids:
        try:
            run_poll(app, sync_id)
        except Exception as exc:
            logger.exception("Unhandled error polling sync %s: %s", sync_id, exc)
