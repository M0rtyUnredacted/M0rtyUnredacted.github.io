"""TikTok Automation App — entry point."""

import json
import logging
import logging.handlers
import os
import signal
import sys
import time

import schedule

import chrome_client
import mailer
import tiktok_scheduler
import ui as ui_module

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")

_fmt = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler
_console = logging.StreamHandler()
_console.setFormatter(_fmt)

# File handler — rotates at 1 MB, keeps 3 backups
_file = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_file.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
log = logging.getLogger("main")


def _log_unhandled(exc_type, exc_value, exc_tb):
    log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

sys.excepthook = _log_unhandled

CONFIG_PATH   = os.path.join(os.path.dirname(__file__), "config.json")
RECENT_LOG    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recent.log")
_RECENT_LINES = 80   # how many tail lines to copy into recent.log


def _write_recent_log():
    """Copy the last _RECENT_LINES lines of app.log → recent.log.
    recent.log always shows the most relevant context without scrolling."""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tail = lines[-_RECENT_LINES:]
        with open(RECENT_LOG, "w", encoding="utf-8") as fh:
            fh.write(f"=== recent.log — last {len(tail)} lines of app.log ===\n")
            fh.writelines(tail)
    except Exception:
        pass


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)

    errors = []
    gd = cfg.get("google_drive", {})

    if "FILL_IN" in gd.get("tiktok_manual_folder_id", "FILL_IN"):
        errors.append("google_drive.tiktok_manual_folder_id")

    if errors:
        sys.exit(
            "ERROR: config.json is incomplete. Please fill in:\n"
            + "\n".join(f"  • {k}" for k in errors)
            + f"\n\nFile: {CONFIG_PATH}"
        )
    return cfg


def make_tiktok_job():
    """Return a job function that reloads config.json on every run.

    Reloading on each invocation means changing tiktok_manual_folder_id
    (or any other config value) in config.json takes effect on the very
    next poll — no restart required.
    """
    def job():
        ui_module.ui_log("TikTok Scheduler: running ...")
        try:
            config = load_config()
        except SystemExit as exc:
            ui_module.ui_log(f"TikTok ERROR: config problem — {exc}")
            log.error("Config reload failed: %s", exc)
            _write_recent_log()
            return
        try:
            tiktok_scheduler.run(config, ui_module.ui_log)
        except Exception as exc:
            log.exception("TikTok Scheduler error")
            ui_module.ui_log(f"TikTok ERROR: {exc}")
            mailer.send_failure(config, "TikTok Scheduler", exc)
        finally:
            _write_recent_log()   # always update recent.log after each run
    return job


def main():
    log.info("TikTok Automation App starting ...")
    log.info("Log file: %s", LOG_PATH)

    config = load_config()
    log.info("Config loaded.")

    tiktok_job = make_tiktok_job()
    ui_module.launch(run_now_fn=tiktok_job)
    time.sleep(2)

    poll_minutes = config.get("tiktok", {}).get("poll_interval_minutes", 10)
    schedule.every(poll_minutes).minutes.do(tiktok_job)

    ui_module.ui_log(f"Scheduler started — TikTok check every {poll_minutes} min.")

    ui_module.ui_log("Running initial check ...")
    tiktok_job()

    def _shutdown(sig, frame):
        ui_module.ui_log("Shutting down ...")
        chrome_client.close_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Running. Gradio UI → http://localhost:7860  (Ctrl+C to stop)")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
