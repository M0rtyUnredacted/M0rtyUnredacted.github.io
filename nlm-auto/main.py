"""NLM Automation App — entry point.

Starts:
  - Gradio UI  (http://localhost:7860)
  - NLM Watcher        (every nlm_poll_minutes, default 15)
  - TikTok Scheduler   (every tiktok_poll_minutes, default 10)
"""

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
    # Basic validation
    required = ["query_docs_folder_id", "notebook_url", "gmail_app_password"]
    missing = [k for k in required if "PASTE" in str(cfg.get(k, "PASTE")) or not cfg.get(k)]
    if missing:
        sys.exit(
            f"ERROR: config.json is incomplete. Please fill in: {', '.join(missing)}\n"
            f"  File: {CONFIG_PATH}"
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

    # Schedule jobs
    nlm_minutes = config.get("nlm_poll_minutes", 15)
    tiktok_minutes = config.get("tiktok_poll_minutes", 10)

    nlm_job = make_nlm_job(config)
    tiktok_job = make_tiktok_job(config)

    schedule.every(nlm_minutes).minutes.do(nlm_job)
    schedule.every(tiktok_minutes).minutes.do(tiktok_job)

    log.info("NLM Watcher scheduled every %d min.", nlm_minutes)
    log.info("TikTok Scheduler scheduled every %d min.", tiktok_minutes)
    ui_module.ui_log(f"Scheduler started. NLM every {nlm_minutes} min, TikTok every {tiktok_minutes} min.")

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

    # Main loop
    log.info("App running. Gradio UI → http://localhost:7860  (Ctrl+C to stop)")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
