"""NLM Watcher — every poll_interval_minutes (default 15).

Pipeline per new query doc:
  1. Export doc text from Drive
  2. Optionally fetch style_doc for tone/format guidance
  3. Open NotebookLM notebook in Chrome
  4. Add the doc text as a new source
  5. Trigger Video Overview generation
  6. Wait, then download the MP4
  7. Upload MP4 to Drive tiktok_manual_folder (SortIt To-Do)
  8. Mark doc as processed in state
"""

import logging
import os
import time
import glob as _glob

import chrome_client
from drive_client import DriveClient
from state import load_state, save_state

log = logging.getLogger(__name__)


def run(config: dict, ui_log):
    drive = DriveClient()
    state = load_state()
    processed = set(state.get("processed_nlm", []))

    quota = config.get("daily_quota", {}).get("max_nlm_videos_per_day", 6)
    done_today = state.get("nlm_done_today", 0)
    if done_today >= quota:
        ui_log(f"NLM Watcher: daily quota reached ({done_today}/{quota}). Skipping.")
        return

    query_folder = config["google_drive"]["query_docs_folder_id"]
    docs = drive.list_docs(query_folder)
    new_docs = [d for d in docs if d["id"] not in processed]

    if not new_docs:
        ui_log("NLM Watcher: no new query docs.")
        return

    remaining = quota - done_today
    new_docs = new_docs[:remaining]
    ui_log(f"NLM Watcher: {len(new_docs)} new doc(s) to process (quota {done_today}/{quota}).")

    style_text = _load_style_doc(config, drive, ui_log)

    for doc in new_docs:
        try:
            _process_doc(doc, config, drive, state, style_text, ui_log)
            state["nlm_done_today"] = state.get("nlm_done_today", 0) + 1
            save_state(state)
        except Exception as exc:
            log.exception("NLM Watcher: failed on doc %s", doc["name"])
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
        ui_log(f"NLM: WARNING — could not load style doc: {exc}")
        return ""


def _process_doc(doc: dict, config: dict, drive: DriveClient, state: dict, style_text: str, ui_log):
    doc_id = doc["id"]
    doc_name = doc["name"]
    ui_log(f"NLM: processing '{doc_name}' ...")

    text = drive.export_doc_as_text(doc_id)
    ui_log(f"NLM: exported {len(text)} chars from '{doc_name}'.")

    downloads_dir = os.path.abspath(config.get("downloads_dir", "downloads"))
    os.makedirs(downloads_dir, exist_ok=True)

    notebook_url = config["notebooklm"]["notebook_url"]
    cdp_url = "http://localhost:9222"

    page = chrome_client.new_page(cdp_url)
    try:
        mp4_path = _nlm_automate(page, notebook_url, text, style_text, downloads_dir, doc_name, ui_log)
    finally:
        page.close()

    tiktok_folder = config["google_drive"]["tiktok_manual_folder_id"]
    if mp4_path and os.path.exists(mp4_path):
        ui_log("NLM: uploading MP4 to Drive TikTok folder ...")
        drive.upload_file(mp4_path, tiktok_folder)
        os.remove(mp4_path)
        ui_log(f"NLM: '{doc_name}' done — MP4 sent to TikTok folder.")
    else:
        ui_log(f"NLM: WARNING — no MP4 found after processing '{doc_name}'.")

    state.setdefault("processed_nlm", []).append(doc_id)
    save_state(state)


def _nlm_automate(page, notebook_url: str, doc_text: str, style_text: str,
                  downloads_dir: str, doc_name: str, ui_log) -> str | None:

    ui_log("NLM: opening notebook ...")
    page.goto(notebook_url, wait_until="networkidle", timeout=60_000)
    time.sleep(2)

    # ── Add source ───────────────────────────────────────────────────────────
    ui_log("NLM: adding source ...")
    add_btn = page.locator(
        "button:has-text('Add source'), button:has-text('Add'), [aria-label*='Add source']"
    ).first
    add_btn.wait_for(timeout=30_000)
    add_btn.click()
    time.sleep(1)

    paste_opt = page.locator(
        "[role='menuitem']:has-text('Paste text'), [role='option']:has-text('Paste'), "
        "button:has-text('Copied text'), [role='menuitem']:has-text('Text')"
    ).first
    paste_opt.wait_for(timeout=10_000)
    paste_opt.click()
    time.sleep(1)

    # Combine style guidance + content (style goes first if present)
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

    # ── Generate Video Overview ───────────────────────────────────────────────
    ui_log("NLM: triggering Video Overview ...")
    video_btn = page.locator(
        "button:has-text('Video Overview'), [aria-label*='Video overview'], "
        "button:has-text('Generate video'), button:has-text('Video')"
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

    # ── Wait for generation ───────────────────────────────────────────────────
    ui_log("NLM: waiting for Video Overview (up to 5 min) ...")
    deadline = time.time() + 300
    while time.time() < deadline:
        download_btn = page.locator(
            "button:has-text('Download'), a:has-text('Download'), [aria-label*='Download']"
        ).first
        if download_btn.is_visible():
            break
        time.sleep(5)
    else:
        raise TimeoutError("Video Overview did not finish generating within 5 minutes.")

    # ── Download MP4 ─────────────────────────────────────────────────────────
    ui_log("NLM: downloading MP4 ...")
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in doc_name)
    dest = os.path.join(downloads_dir, f"{safe_name}.mp4")

    with page.expect_download(timeout=120_000) as dl_info:
        download_btn.click()
    dl_info.value.save_as(dest)

    ui_log(f"NLM: MP4 saved → {dest}")
    return dest
