"""
APScheduler setup.

- One recurring job runs every hour to poll all active syncs.
- One-off initial-sync jobs are added dynamically when a Sync is created.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    job_defaults={"coalesce": True, "max_instances": 1},
    timezone="UTC",
)


def init_scheduler(app):
    from app.sync_engine import run_all_polls

    def hourly_poll():
        logger.info("Hourly poll triggered")
        run_all_polls(app)

    scheduler.add_job(
        hourly_poll,
        trigger="interval",
        hours=1,
        id="hourly_poll",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started")


def trigger_initial_sync(app, sync_id: str):
    """Queue a one-off initial sync job for the given sync_id."""
    from app.sync_engine import run_initial_sync

    job_id = f"initial_sync_{sync_id}"
    scheduler.add_job(
        run_initial_sync,
        args=[app, sync_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("Queued initial sync job: %s", job_id)


def trigger_poll(app, sync_id: str):
    """Queue a one-off manual poll for the given sync_id."""
    from app.sync_engine import run_poll

    job_id = f"manual_poll_{sync_id}"
    scheduler.add_job(
        run_poll,
        args=[app, sync_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60,
    )
    logger.info("Queued manual poll job: %s", job_id)
