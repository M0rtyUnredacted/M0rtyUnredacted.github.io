"""Persistent state — tracks which Drive files have already been processed."""

import json
import logging
import os

log = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

_DEFAULT: dict = {
    "processed_nlm": [],
    "processed_tiktok": [],
    "last_tiktok_post": None,
}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state.json (%s) — starting fresh.", exc)
    return dict(_DEFAULT)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
