"""NLM Automation App — entry point."""

import json
import logging
import os
import signal
import sys
import time

import schedule

import chrome_client
import mailer
import nlm_watcher
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
    nlm = cfg.get("notebooklm", {})
    notif = cfg.get("notifications", {})

    if "FILL_IN" in gd.get("query_docs_folder_id", "FILL_IN"):
        errors.append("google_drive.query_docs_folder_id")
    if "FILL_IN" in gd.get("tiktok_manual_folder_id", "FILL_IN"):
        errors.append("google_drive.tiktok_manual_folder_id")
    if "FILL_IN" in nlm.get("notebook_url", "FILL_IN"):
        errors.append("notebooklm.notebook_url")
    if "xxxx" in notif.get("gmail_app_password", "xxxx"):
        errors.append("notifications.gmail_app_password")

    if errors:
        sys.exit(
            "ERROR: config.json is incomplete. Please fill in:\n"
            + "\n".join(f"  • {k}" for k in errors)
            + f"\n\nFile: {CONFIG_PATH}"
        )
    return cfg


def make_nlm_job(config: dict):
    def job():
        ui_module.ui_log("NLM Watcher: running ...")
        try:
            nlm_watcher.run(config, ui_module.ui_log)
        except Exception as exc:
            log.exception("NLM Watcher error")
            ui_module.ui_log(f"NLM ERROR: {exc}")
            mailer.send_failure(config, "NLM Watcher", exc)
    return job


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
    log.info("NLM Automation App starting ...")

    config = load_config()
    log.info("Config loaded.")

    # Start Gradio UI
    ui_module.launch()
    time.sleep(2)

    nlm_minutes = config.get("google_drive", {}).get("poll_interval_minutes", 15)
    tiktok_minutes = 10  # TikTok always checks every 10 min

    nlm_job = make_nlm_job(config)
    tiktok_job = make_tiktok_job(config)

    schedule.every(nlm_minutes).minutes.do(nlm_job)
    schedule.every(tiktok_minutes).minutes.do(tiktok_job)

    ui_module.ui_log(
        f"Scheduler started — NLM every {nlm_minutes} min, TikTok every {tiktok_minutes} min."
    )

    # Run both jobs immediately on startup so we don't wait for the first interval
    ui_module.ui_log("Running initial checks ...")
    nlm_job()
    tiktok_job()

    # Graceful shutdown
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
