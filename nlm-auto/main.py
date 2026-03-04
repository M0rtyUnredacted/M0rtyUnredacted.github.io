"""TikTok Automation App — entry point."""

import json
import logging
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)

    errors = []
    gd = cfg.get("google_drive", {})
    notif = cfg.get("notifications", {})

    if "FILL_IN" in gd.get("tiktok_manual_folder_id", "FILL_IN"):
        errors.append("google_drive.tiktok_manual_folder_id")
    if "xxxx" in notif.get("gmail_app_password", "xxxx"):
        errors.append("notifications.gmail_app_password")

    if errors:
        sys.exit(
            "ERROR: config.json is incomplete. Please fill in:\n"
            + "\n".join(f"  • {k}" for k in errors)
            + f"\n\nFile: {CONFIG_PATH}"
        )
    return cfg


def make_tiktok_job(config: dict):
    def job():
        ui_module.ui_log("TikTok Scheduler: running ...")
        try:
            tiktok_scheduler.run(config, ui_module.ui_log)
        except Exception as exc:
            log.exception("TikTok Scheduler error")
            ui_module.ui_log(f"TikTok ERROR: {exc}")
            mailer.send_failure(config, "TikTok Scheduler", exc)
    return job


def main():
    log.info("TikTok Automation App starting ...")

    config = load_config()
    log.info("Config loaded.")

    ui_module.launch()
    time.sleep(2)

    poll_minutes = config.get("tiktok", {}).get("poll_interval_minutes", 10)
    tiktok_job = make_tiktok_job(config)

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
