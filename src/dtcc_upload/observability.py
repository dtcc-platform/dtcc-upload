from __future__ import annotations

import json
import logging
from typing import Any


logger = logging.getLogger("dtcc_upload")


def log_event(action: str, **fields: Any) -> None:
    payload = {"action": action, **{key: value for key, value in fields.items() if value is not None}}
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def collect_metrics(catalog) -> dict[str, object]:
    return catalog.get_metrics()
