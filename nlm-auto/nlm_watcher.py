"""NLM Watcher — every 15 min.

Pipeline per new query doc:
  1. Export doc text from Drive
  2. Open NotebookLM notebook in Chrome
  3. Add the doc text as a new source (paste into "add source" dialog)
  4. Trigger Video Overview generation
  5. Wait for download and collect the MP4
  6. Move MP4 to Drive SortIt/To-Do folder
  7. Mark doc as processed in state
"""

import logging
import os
import time
import glob as _glob

import chrome_client
from drive_client import DriveClient
from state import load_state, save_state

log = logging.getLogger(__name__)

NLM_TIMEOUT = 300_000  # 5 min — video overview generation can be slow


def run(config: dict, ui_log):
    """Entry point called by the scheduler."""
    drive = DriveClient()
    state = load_state()
    processed = set(state.get("processed_nlm", []))

    docs = drive.list_docs(config["query_docs_folder_id"])
    new_docs = [d for d in docs if d["id"] not in processed]

    if not new_docs:
        ui_log("NLM Watcher: no new query docs.")
        return

    ui_log(f"NLM Watcher: {len(new_docs)} new doc(s) to process.")

    for doc in new_docs:
        try:
            _process_doc(doc, config, drive, state, ui_log)
        except Exception as exc:
            log.exception("NLM Watcher: failed on doc %s", doc["name"])
            raise RuntimeError(f"NLM failed on '{doc['name']}': {exc}") from exc


def _process_doc(doc: dict, config: dict, drive: DriveClient, state: dict, ui_log):
    doc_id = doc["id"]
    doc_name = doc["name"]
    ui_log(f"NLM: processing '{doc_name}' ...")

    # 1. Export doc text
    text = drive.export_doc_as_text(doc_id)
    ui_log(f"NLM: exported {len(text)} chars from '{doc_name}'.")

    # 2-6. Browser automation
    downloads_dir = os.path.abspath(config.get("downloads_dir", "downloads"))
    os.makedirs(downloads_dir, exist_ok=True)

    page = chrome_client.new_page(config["chrome_debug_url"])
    try:
        mp4_path = _nlm_automate(page, config["notebook_url"], text, downloads_dir, doc_name, ui_log)
    finally:
        page.close()

    # 7. Upload MP4 to SortIt Drive folder
    if mp4_path and os.path.exists(mp4_path):
        ui_log(f"NLM: uploading MP4 to Drive SortIt folder ...")
        drive.upload_file(mp4_path, config["sortit_folder_id"])
        os.remove(mp4_path)
        ui_log(f"NLM: '{doc_name}' done — MP4 sent to SortIt.")
    else:
        ui_log(f"NLM: WARNING — no MP4 found after processing '{doc_name}'.")

    # Mark processed
    state.setdefault("processed_nlm", []).append(doc_id)
    save_state(state)


def _nlm_automate(page, notebook_url: str, doc_text: str, downloads_dir: str, doc_name: str, ui_log) -> str | None:
    """Drive NotebookLM in Chrome and return path to downloaded MP4."""

    # Navigate to notebook
    ui_log("NLM: opening notebook ...")
    page.goto(notebook_url, wait_until="networkidle", timeout=60_000)
    time.sleep(2)

    # ── Add source ───────────────────────────────────────────────────────────
    ui_log("NLM: adding source ...")
    # Click the "+ Add source" / "Add source" button
    add_btn = page.locator(
        "button:has-text('Add source'), button:has-text('Add'), [aria-label*='Add source']"
    ).first
    add_btn.wait_for(timeout=30_000)
    add_btn.click()
    time.sleep(1)

    # Choose "Copied text" / "Paste text" option
    paste_opt = page.locator(
        "[role='menuitem']:has-text('Paste text'), [role='option']:has-text('Paste'), button:has-text('Copied text')"
    ).first
    paste_opt.wait_for(timeout=10_000)
    paste_opt.click()
    time.sleep(1)

    # Fill the text area
    text_area = page.locator("textarea, [contenteditable='true']").first
    text_area.wait_for(timeout=10_000)
    text_area.fill(doc_text[:50_000])  # NLM source cap
    time.sleep(0.5)

    # Confirm / Insert
    insert_btn = page.locator(
        "button:has-text('Insert'), button:has-text('Add'), button:has-text('Save')"
    ).last
    insert_btn.click()
    time.sleep(3)
    ui_log("NLM: source added.")

    # ── Generate Video Overview ───────────────────────────────────────────────
    ui_log("NLM: triggering Video Overview ...")
    # Look for the Video Overview section / button
    video_btn = page.locator(
        "button:has-text('Video Overview'), [aria-label*='Video overview'], button:has-text('Generate')"
    ).first
    video_btn.wait_for(timeout=20_000)
    video_btn.click()
    time.sleep(2)

    # If there's a confirmation / "Generate" dialog
    gen_confirm = page.locator("button:has-text('Generate'), button:has-text('Create')").last
    if gen_confirm.is_visible():
        gen_confirm.click()
        time.sleep(2)

    # ── Wait for generation ───────────────────────────────────────────────────
    ui_log("NLM: waiting for Video Overview generation (up to 5 min) ...")
    # Poll until a Download button appears or a progress indicator disappears
    deadline = time.time() + 300
    while time.time() < deadline:
        download_btn = page.locator(
            "button:has-text('Download'), a:has-text('Download'), [aria-label*='Download']"
        ).first
        if download_btn.is_visible():
            break
        progress = page.locator("[aria-label*='loading'], [class*='spinner'], [class*='progress']").first
        if not progress.is_visible():
            # Check again for download button
            if download_btn.is_visible():
                break
        time.sleep(5)
    else:
        raise TimeoutError("Video Overview did not finish generating within 5 minutes.")

    # ── Download MP4 ─────────────────────────────────────────────────────────
    ui_log("NLM: downloading MP4 ...")
    before = set(_glob.glob(os.path.join(downloads_dir, "*.mp4")))

    with page.expect_download(timeout=120_000) as dl_info:
        download_btn.click()
    download = dl_info.value

    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in doc_name)
    dest = os.path.join(downloads_dir, f"{safe_name}.mp4")
    download.save_as(dest)
    ui_log(f"NLM: MP4 saved → {dest}")
    return dest
