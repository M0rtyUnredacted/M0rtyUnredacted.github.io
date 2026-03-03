"""NLM Watcher — every poll_interval_minutes (default 15).

Pipeline per new query doc:
  1. Export doc text from Drive
  2. Optionally prepend style_doc text for tone/format guidance
  3. Open NotebookLM notebook in Chrome (CDP on 127.0.0.1:9222)
  4. Add the doc text as a Copied Text source
  5. Trigger Video Overview generation
  6. Wait up to 15 min for the Download button to appear
  7. Download MP4 to temp dir
  8. Upload MP4 to Drive tiktok_manual_folder
  9. Mark doc done in SQLite
"""

import logging
import os
import time

import chrome_client
import db
from drive_client import DriveClient

log = logging.getLogger(__name__)

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
CDP_URL = "http://127.0.0.1:9222"


def run(config: dict, ui_log):
    db.init()
    drive = DriveClient()

    quota_max = config.get("daily_quota", {}).get("max_nlm_videos_per_day", 6)
    quota_used = db.quota_used_today()
    if quota_used >= quota_max:
        ui_log(f"NLM Watcher: daily quota reached ({quota_used}/{quota_max}). Skipping.")
        return

    query_folder = config["google_drive"]["query_docs_folder_id"]
    docs = drive.list_docs(query_folder)
    new_docs = [d for d in docs if not db.is_doc_processed(d["id"])]

    if not new_docs:
        ui_log("NLM Watcher: no new query docs.")
        return

    remaining = quota_max - quota_used
    new_docs = new_docs[:remaining]
    ui_log(f"NLM Watcher: {len(new_docs)} new doc(s) to process (quota {quota_used}/{quota_max}).")

    style_text = _load_style_doc(config, drive, ui_log)

    for doc in new_docs:
        db.mark_doc_in_progress(doc["id"], doc["name"], doc.get("modifiedTime", ""))
        try:
            _process_doc(doc, config, drive, style_text, ui_log)
        except Exception as exc:
            log.exception("NLM Watcher: failed on '%s'", doc["name"])
            db.mark_doc_failed(doc["id"], str(exc))
            raise RuntimeError(f"NLM failed on '{doc['name']}': {exc}") from exc


def _load_style_doc(config: dict, drive: DriveClient, ui_log) -> str:
    style_id = config.get("notebooklm", {}).get("style_doc_id", "")
    if not style_id or "FILL_IN" in style_id:
        return ""
    try:
        text = drive.export_doc_as_text(style_id)
        ui_log(f"NLM: loaded style doc ({len(text)} chars).")
        return text
    except Exception as exc:
        ui_log(f"NLM: WARNING - could not load style doc: {exc}")
        return ""


def _process_doc(doc: dict, config: dict, drive: DriveClient, style_text: str, ui_log):
    doc_id = doc["id"]
    doc_name = doc["name"]
    ui_log(f"NLM: processing '{doc_name}' ...")

    os.makedirs(TEMP_DIR, exist_ok=True)

    text = drive.export_doc_as_text(doc_id)
    ui_log(f"NLM: exported {len(text)} chars from '{doc_name}'.")

    notebook_url = config["notebooklm"]["notebook_url"]
    page = chrome_client.new_page(CDP_URL)
    try:
        mp4_path = _nlm_automate(page, notebook_url, text, style_text, doc_name, ui_log)
    finally:
        page.close()

    tiktok_folder = config["google_drive"]["tiktok_manual_folder_id"]
    if mp4_path and os.path.exists(mp4_path):
        ui_log("NLM: uploading MP4 to Drive TikTok folder ...")
        drive.upload_file(mp4_path, tiktok_folder)
        os.remove(mp4_path)
        db.mark_doc_done(doc_id, os.path.basename(mp4_path))
        db.increment_quota()
        ui_log(f"NLM: '{doc_name}' done.")
    else:
        raise RuntimeError(f"No MP4 found after processing '{doc_name}'.")


def _nlm_automate(page, notebook_url: str, doc_text: str, style_text: str,
                  doc_name: str, ui_log) -> str:

    ui_log("NLM: opening notebook ...")
    page.goto(notebook_url, wait_until="networkidle", timeout=60_000)
    time.sleep(2)

    # ── Add source (Copied Text) ──────────────────────────────────────────────
    ui_log("NLM: adding source ...")
    add_btn = page.locator(
        "button:has-text('Add source'), button:has-text('Add'), [aria-label*='Add source']"
    ).first
    add_btn.wait_for(timeout=30_000)
    add_btn.click()
    time.sleep(1)

    paste_opt = page.locator(
        "[role='menuitem']:has-text('Copied text'), [role='menuitem']:has-text('Paste text'), "
        "[role='option']:has-text('Paste'), button:has-text('Copied text'), "
        "[role='menuitem']:has-text('Text')"
    ).first
    paste_opt.wait_for(timeout=10_000)
    paste_opt.click()
    time.sleep(1)

    source_text = (style_text + "\n\n---\n\n" + doc_text if style_text else doc_text)[:50_000]

    text_area = page.locator("textarea, [contenteditable='true']").first
    text_area.wait_for(timeout=10_000)
    text_area.fill(source_text)
    time.sleep(0.5)

    insert_btn = page.locator(
        "button:has-text('Insert'), button:has-text('Add'), button:has-text('Save')"
    ).last
    insert_btn.click()
    time.sleep(3)
    ui_log("NLM: source added.")

    # ── Trigger Video Overview ────────────────────────────────────────────────
    ui_log("NLM: triggering Video Overview ...")
    video_btn = page.locator(
        "button:has-text('Video overview'), button:has-text('Video Overview'), "
        "[aria-label*='Video overview'], button:has-text('Generate video')"
    ).first
    video_btn.wait_for(timeout=20_000)
    video_btn.click()
    time.sleep(2)

    gen_confirm = page.locator(
        "button:has-text('Generate'), button:has-text('Create'), button:has-text('Start')"
    ).last
    if gen_confirm.is_visible():
        gen_confirm.click()
        time.sleep(2)

    # ── Wait up to 15 min ─────────────────────────────────────────────────────
    ui_log("NLM: waiting for Video Overview (up to 15 min) ...")
    deadline = time.time() + 900
    while time.time() < deadline:
        download_btn = page.locator(
            "button:has-text('Download'), a:has-text('Download'), [aria-label*='Download']"
        ).first
        if download_btn.is_visible():
            break
        time.sleep(5)
    else:
        raise TimeoutError("Video Overview did not finish within 15 minutes.")

    # ── Download MP4 ──────────────────────────────────────────────────────────
    ui_log("NLM: downloading MP4 ...")
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in doc_name)
    dest = os.path.join(TEMP_DIR, f"{safe_name}.mp4")

    with page.expect_download(timeout=120_000) as dl_info:
        download_btn.click()
    dl_info.value.save_as(dest)

    ui_log(f"NLM: saved → {dest}")
    return dest
