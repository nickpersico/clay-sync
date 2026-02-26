"""
Clay workbook webhook client.

Clay accepts arbitrary JSON key-value pairs via POST.
Every payload we send includes `_close_id` so Clay can use it as a
deduplication / lookup key when configuring the workbook.

Tombstoning (soft-delete): instead of a hard delete we set
`_removed_from_sync: true` so users can set up a secondary Clay automation
to remove those rows if they wish.
"""

import logging

import requests

logger = logging.getLogger(__name__)


class ClayWebhookError(Exception):
    pass


class ClayClient:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send_record(self, record_data: dict) -> bool:
        """POST a single record to the Clay webhook."""
        try:
            resp = requests.post(
                self.webhook_url,
                json=record_data,
                timeout=30,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            raise ClayWebhookError(
                f"Clay webhook POST failed: {exc}"
            ) from exc

    def tombstone_record(self, record_data: dict) -> bool:
        """
        Mark a record as removed by sending the existing data back with
        `_removed_from_sync` set to True.
        """
        payload = {**record_data, "_removed_from_sync": True}
        return self.send_record(payload)
