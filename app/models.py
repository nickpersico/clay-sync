import uuid
from datetime import datetime
from app import db


def _uuid():
    return str(uuid.uuid4())


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    close_user_id = db.Column(db.String(100), unique=True, nullable=False)
    close_org_id = db.Column(db.String(100))
    name = db.Column(db.String(200))
    email = db.Column(db.String(200))
    # Tokens are stored in plaintext for local dev.
    # Use encryption (e.g. cryptography.fernet) before deploying publicly.
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    token_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    syncs = db.relationship("Sync", back_populates="user", lazy="dynamic")

    def __repr__(self):
        return f"<User {self.email}>"


class Sync(db.Model):
    __tablename__ = "syncs"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    # 'lead' or 'contact' — extensible to other Close object types later
    entity_type = db.Column(db.String(20), nullable=False)
    close_query = db.Column(db.JSON, nullable=False)
    clay_webhook_url = db.Column(db.String(500), nullable=False)
    # pending | initial_syncing | active | error
    status = db.Column(db.String(20), default="pending", nullable=False)
    error_message = db.Column(db.Text)
    last_polled_at = db.Column(db.DateTime)
    # Progress counters for the initial sync UI
    total_records = db.Column(db.Integer, default=0)
    synced_records = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship("User", back_populates="syncs")
    records = db.relationship(
        "SyncRecord",
        back_populates="sync",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Sync {self.name!r} [{self.status}]>"


class SyncRecord(db.Model):
    __tablename__ = "sync_records"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    sync_id = db.Column(db.String(36), db.ForeignKey("syncs.id"), nullable=False)
    close_record_id = db.Column(db.String(100), nullable=False)
    # Full snapshot of the record as returned by Close.
    # Used for change-detection on hourly polls.
    record_data = db.Column(db.JSON)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sync = db.relationship("Sync", back_populates="records")

    __table_args__ = (
        db.UniqueConstraint("sync_id", "close_record_id", name="uq_sync_close_record"),
        db.Index("ix_sync_records_sync_id", "sync_id"),
        db.Index("ix_sync_records_close_record_id", "close_record_id"),
    )

    def __repr__(self):
        return f"<SyncRecord {self.close_record_id}>"
