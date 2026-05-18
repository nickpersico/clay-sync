"""
Close CRM API client.

Key notes from the Close advanced filtering docs:
  - Endpoint:   POST /api/v1/data/search/
  - Pagination: cursor-based (pass `cursor` from previous response)
  - Cursors expire after 30 s — paginate without pausing between pages
  - Hard limit:  10,000 objects per cursor session
  - Count:       pass `include_counts: true` to get total in first response
  - Fields:      /data/search/ returns only id + __object_type by default.
                 Use _fields to request extra lightweight fields (e.g. date_updated).
                 Full record data must be fetched via GET /lead/{id}/ or /contact/{id}/.

Bypassing the 10k limit (date-range windowing)
  - Sort by date_created ASC and use the last record's date_created as
    the lower bound (`"after"`) for the next request.
  - Each request is its own cursor session (up to 10k records).
  - Repeat until a batch returns fewer than 10k records.
  - Using strict `after` means no record can appear in two consecutive
    windows, so deduplication is only needed as a safety net.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

CLOSE_API_BASE = "https://api.close.com/api/v1"
CLOSE_TOKEN_URL = "https://api.close.com/oauth2/token"
SEARCH_ENDPOINT = "/data/search/"

# Rate-limit handling.
# Close returns 429 with a `RateLimit: limit=N, remaining=N, reset=N` header.
# The recommended strategy (per Close docs) is to sleep for `reset` seconds
# and retry. We fall back to `Retry-After`, then a small default.
MAX_429_RETRIES = 5
DEFAULT_429_BACKOFF_S = 5


def _parse_ratelimit_reset(header_value: str) -> int | None:
    """Parse `reset=N` out of a `RateLimit: limit=…, remaining=…, reset=N` header."""
    if not header_value:
        return None
    for part in header_value.split(","):
        part = part.strip()
        if part.startswith("reset="):
            try:
                return max(1, int(float(part.split("=", 1)[1])))
            except ValueError:
                return None
    return None


class CloseAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class CloseClient:
    def __init__(
        self,
        access_token,
        refresh_token=None,
        token_expires_at=None,
        client_id=None,
        client_secret=None,
        user_id=None,  # internal DB user id, used to persist refreshed tokens
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = token_expires_at
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _maybe_refresh(self):
        if not self.token_expires_at:
            return
        if datetime.utcnow() >= self.token_expires_at - timedelta(minutes=5):
            self._do_refresh()

    def _do_refresh(self):
        resp = requests.post(
            CLOSE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        if not resp.ok:
            raise CloseAPIError(
                f"Token refresh failed: {resp.text}", resp.status_code
            )
        data = resp.json()
        self.access_token = data["access_token"]
        self.token_expires_at = datetime.utcnow() + timedelta(
            seconds=data.get("expires_in", 3600)
        )
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]

        # Persist refreshed tokens to DB
        if self.user_id:
            try:
                from app.models import User
                from app import db

                user = User.query.get(self.user_id)
                if user:
                    user.access_token = self.access_token
                    user.token_expires_at = self.token_expires_at
                    user.refresh_token = self.refresh_token
                    db.session.commit()
            except Exception as exc:
                logger.warning("Failed to persist refreshed token: %s", exc)

    def _request_with_retry(self, method, url, **kwargs):
        """
        Issue a request, retrying on 429 according to Close's RateLimit header.
        After MAX_429_RETRIES, returns the last response so `_check_response`
        raises a normal CloseAPIError.
        """
        resp = None
        for attempt in range(MAX_429_RETRIES + 1):
            resp = requests.request(method, url, timeout=30, **kwargs)
            if resp.status_code != 429:
                return resp
            wait = _parse_ratelimit_reset(resp.headers.get("RateLimit", ""))
            if wait is None:
                try:
                    wait = int(resp.headers.get("Retry-After", DEFAULT_429_BACKOFF_S))
                except ValueError:
                    wait = DEFAULT_429_BACKOFF_S
            logger.warning(
                "Close API 429 on %s %s — sleeping %ds (attempt %d/%d)",
                method, url, wait, attempt + 1, MAX_429_RETRIES + 1,
            )
            time.sleep(wait)
        return resp

    def _post(self, path, json_body):
        self._maybe_refresh()
        url = f"{CLOSE_API_BASE}{path}"
        resp = self._request_with_retry(
            "POST", url, headers=self._headers, json=json_body
        )
        self._check_response(resp)
        return resp.json()

    def _get(self, path, params=None):
        self._maybe_refresh()
        url = f"{CLOSE_API_BASE}{path}"
        resp = self._request_with_retry(
            "GET", url, headers=self._headers, params=params
        )
        self._check_response(resp)
        return resp.json()

    @staticmethod
    def _check_response(resp):
        if resp.ok:
            return
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise CloseAPIError(
            f"Close API {resp.status_code}: {detail}", resp.status_code
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_me(self):
        """Return the authenticated user's profile."""
        return self._get("/me/")

    def get_record_count(self, query_body):
        """
        Return the total number of records matching `query_body` without
        fetching them all. Uses include_counts=true with _limit=1.
        Returns None if the API doesn't return a count.
        """
        body = {**query_body, "_limit": 1, "include_counts": True}
        result = self._post(SEARCH_ENDPOINT, body)
        count_info = result.get("count", {})
        return count_info.get("total")

    def search_page(self, query_body, limit=100, cursor=None):
        """
        Fetch a single page of search stubs (id + date_updated).
        Returns (records_list, next_cursor_or_None).
        """
        body = {**query_body, "_limit": limit}
        if cursor:
            body["cursor"] = cursor
        result = self._post(SEARCH_ENDPOINT, body)
        records = result.get("data", [])
        next_cursor = result.get("cursor")  # None signals last page
        return records, next_cursor

    def get_all_stubs(self, query_body, entity_type, max_records=500_000):
        """
        Return lightweight stubs for every record matching query_body.
        Each stub contains: id, date_created, date_updated.

        Works around Close's 10k-per-cursor-session limit by issuing multiple
        requests in sequential date_created windows:

          Window 1: full query, sorted date_created ASC → up to 10k records
          Window 2: same query AND date_created > last_window_end → up to 10k
          ...repeat until a window returns fewer than 10k records.

        max_records is a safety cap (default 500k) to prevent runaway fetches.
        """
        WINDOW_CAP = 10_000  # Close API hard limit per cursor session

        all_stubs: list = []
        date_cursor: str | None = None  # date_created lower-bound for next window

        while len(all_stubs) < max_records:
            body = self._build_windowed_query(query_body, entity_type, date_cursor)
            window = self._fetch_cursor_window(body)

            if not window:
                break

            all_stubs.extend(window)
            logger.info(
                "get_all_stubs: window of %d stubs fetched (running total: %d)",
                len(window),
                len(all_stubs),
            )

            # A window smaller than WINDOW_CAP means we've seen everything
            if len(window) < WINDOW_CAP:
                break

            # Full window — advance past the last record's date_created
            last_date = window[-1].get("date_created")
            if not last_date or last_date == date_cursor:
                logger.warning(
                    "get_all_stubs: cannot advance date cursor past %r — stopping",
                    last_date,
                )
                break
            date_cursor = last_date

        # Deduplicate by ID (safety net for the rare case where two records share
        # the exact date_created value that sits at a window boundary)
        seen: set = set()
        deduped: list = []
        for stub in all_stubs:
            rid = stub.get("id")
            if rid and rid not in seen:
                seen.add(rid)
                deduped.append(stub)

        if len(deduped) > max_records:
            raise ValueError(
                f"This filter matches more than {max_records:,} records, which exceeds "
                f"Clay's {max_records:,}-row workbook limit. Please refine your filter."
            )

        return deduped

    def _build_windowed_query(self, query_body: dict, entity_type: str,
                              date_after: str | None) -> dict:
        """
        Build a /data/search/ body for one date-range window.

        - Forces sort to date_created ASC (required for sequential windowing).
        - Requests only id + date_created + date_updated via _fields.
        - If date_after is provided, injects a `date_created > date_after`
          condition wrapped around the caller's original query.
        """
        body = dict(query_body)
        body["sort"] = [{"field": {"type": "regular_field", "object_type": entity_type, "field_name": "date_created"}, "direction": "asc"}]
        body["_fields"] = {entity_type: ["id", "date_created", "date_updated"]}

        if date_after:
            original_query = query_body.get("query") or {}
            date_condition = {
                "type": "field",
                "field": {"type": "regular_field", "field_name": "date_created"},
                "condition": {"type": "after", "value": date_after},
            }
            body["query"] = (
                {"type": "and", "queries": [original_query, date_condition]}
                if original_query
                else date_condition
            )

        return body

    def _fetch_cursor_window(self, body: dict) -> list:
        """
        Cursor-paginate a single /data/search/ query, collecting up to 10k
        stubs. All page requests must complete within Close's 30s cursor expiry.
        """
        stubs: list = []
        cursor: str | None = None

        while True:
            page, cursor = self.search_page(body, limit=100, cursor=cursor)
            stubs.extend(page)
            if not cursor:
                break  # last page of this window

        return stubs

    def get_record(self, entity_type, record_id):
        """
        Fetch the complete data for a single record via the standard REST endpoint.
        e.g. GET /api/v1/lead/{id}/ or GET /api/v1/contact/{id}/
        This always returns all fields including custom fields.
        """
        return self._get(f"/{entity_type}/{record_id}/")
