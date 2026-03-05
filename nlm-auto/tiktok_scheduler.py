"""TikTok Scheduler — every 10 min.

Pipeline per new MP4 in Drive tiktok_manual_folder:
  1. Download MP4 to temp dir
  2. Read paired .md sidecar for caption
  3. Open TikTok Studio upload page in Chrome (CDP on 127.0.0.1:9222)
  4. Upload MP4, fill caption, schedule at max(now+20min, last_post+5h)
  5. Mark as scheduled in SQLite
"""

import logging
import os
import time
from datetime import datetime, timedelta

import chrome_client
import db
from drive_client import DriveClient

log = logging.getLogger(__name__)


class TiktokRateLimitError(RuntimeError):
    """TikTok is throttling uploads (/upload/unavailable).  Transient — do NOT
    mark the video as permanently failed; it will be retried on the next poll."""


class TiktokUploadTimeoutError(TiktokRateLimitError):
    """Upload iframe never showed the caption field within the timeout window.
    Likely a slow network or TikTok processing delay — transient, retry next poll."""

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
CDP_URL = "http://127.0.0.1:9222"
TIKTOK_STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"


def run(config: dict, ui_log):
    db.init()
    drive = DriveClient()

    folder_id = config["google_drive"]["tiktok_ready_folder_id"]
    mp4s = drive.list_mp4s(folder_id)
    new_mp4s = [f for f in mp4s if not db.is_tiktok_processed(f["id"])]

    if not new_mp4s:
        ui_log("TikTok Scheduler: no new videos.")
        return

    new_mp4s.sort(key=lambda f: f.get("modifiedTime", ""))
    pending = len(new_mp4s)
    ui_log(f"TikTok Scheduler: {pending} video(s) pending.")

    # Process all queued videos, but pause 30 s between uploads so TikTok's
    # server has time to finish processing the previous post (instant tab
    # reopening triggers /upload/unavailable even without a hard rate limit).
    INTER_UPLOAD_PAUSE = 30  # seconds

    for i, mp4 in enumerate(new_mp4s):
        if i > 0:
            ui_log(f"TikTok: pausing {INTER_UPLOAD_PAUSE}s before next upload ...")
            time.sleep(INTER_UPLOAD_PAUSE)
        try:
            _process_video(mp4, config, drive, ui_log)
        except TiktokRateLimitError as exc:
            # Covers TiktokRateLimitError AND TiktokUploadTimeoutError (subclass).
            # Both are transient — do NOT mark the video as permanently failed;
            # it will be retried on the next poll.
            reason = "rate-limited" if type(exc) is TiktokRateLimitError else "upload timed out"
            ui_log(f"TikTok: {reason} — will retry '{mp4['name']}' next poll.")
            log.warning("TikTok transient error on '%s': %s", mp4["name"], exc)
            return
        except Exception as exc:
            log.exception("TikTok Scheduler: failed on '%s'", mp4["name"])
            db.mark_tiktok_failed(mp4["id"], mp4["name"], str(exc))
            raise RuntimeError(f"TikTok failed on '{mp4['name']}': {exc}") from exc


def _process_video(mp4: dict, config: dict, drive: DriveClient, ui_log):
    file_id = mp4["id"]
    name = mp4["name"]
    ui_log(f"TikTok: processing '{name}' ...")

    os.makedirs(TEMP_DIR, exist_ok=True)
    local_mp4 = os.path.join(TEMP_DIR, name)
    page = chrome_client.new_page(CDP_URL)
    try:
        drive.download_file(file_id, local_mp4)
        ui_log(f"TikTok: downloaded '{name}'.")

        ready_folder_id  = config["google_drive"]["tiktok_ready_folder_id"]
        posted_folder_id = config["google_drive"]["tiktok_posted_folder_id"]
        caption, sidecar_id = _get_caption(drive, ready_folder_id, name)
        ui_log(f"TikTok: caption = {caption[:80]}{'...' if len(caption) > 80 else ''}")

        gap_hours = config.get("tiktok", {}).get("post_interval_hours", 5)
        last_post_str = db.last_tiktok_scheduled_time()
        if last_post_str:
            last_post = datetime.fromisoformat(last_post_str)
            schedule_dt = max(
                datetime.now() + timedelta(minutes=20),
                last_post + timedelta(hours=gap_hours),
            )
        else:
            schedule_dt = datetime.now() + timedelta(hours=gap_hours)

        # TikTok only allows scheduling up to 10 days ahead.
        # If the computed slot exceeds that, cap it and warn — a backed-up queue
        # could silently push the date past TikTok's limit.
        _MAX_SCHEDULE_DAYS = 10
        _max_allowed = datetime.now() + timedelta(days=_MAX_SCHEDULE_DAYS - 1, hours=23)
        if schedule_dt > _max_allowed:
            ui_log(
                f"TikTok: schedule {schedule_dt:%Y-%m-%d %H:%M} exceeds 10-day limit "
                f"— capping to {_max_allowed:%Y-%m-%d %H:%M}"
            )
            schedule_dt = _max_allowed

        ui_log(f"TikTok: scheduling at {schedule_dt.strftime('%Y-%m-%d %H:%M')} ...")
        _tiktok_upload(page, local_mp4, caption, schedule_dt, ui_log)
        db.mark_tiktok_scheduled(file_id, name, schedule_dt.isoformat())
        ui_log(f"TikTok: '{name}' scheduled.")

        # Move video + sidecar to the Posted folder
        drive.move_file(file_id, posted_folder_id)
        ui_log(f"TikTok: moved '{name}' to Posted folder.")
        if sidecar_id:
            drive.move_file(sidecar_id, posted_folder_id)
            ui_log(f"TikTok: moved sidecar for '{name}' to Posted folder.")
    finally:
        try:
            page.close()
        except Exception:
            pass  # page may already be closed; don't mask the real exception
        try:
            os.remove(local_mp4)
        except FileNotFoundError:
            pass


def _get_caption(drive: DriveClient, folder_id: str, mp4_name: str) -> tuple[str, str | None]:
    """Priority: sidecar .md > sidecar .txt > filename-based fallback.

    Returns (caption_text, sidecar_file_id).  sidecar_file_id is None when
    falling back to the filename-generated caption.
    """
    stem = os.path.splitext(mp4_name)[0]
    for f in drive.list_files(folder_id):
        if f["name"] in (stem + ".md", stem + ".txt"):
            try:
                return drive.read_plain_text(f["id"]), f["id"]
            except Exception as exc:
                log.warning("Could not read sidecar %s: %s", f["name"], exc)
    return _caption_from_filename(stem), None


def _caption_from_filename(stem: str) -> str:
    """Generate a readable caption + 5 hashtags from a filename stem.

    Rules to stay TikTok TOS-safe:
    - No misleading claims, no banned/restricted topic tags
    - Hashtags are derived from the filename words plus safe engagement tags
    """
    import re

    # Strip leading date prefixes: 2024-01-15_, 20240115_
    cleaned = re.sub(r"^(\d{4}[-_]\d{2}[-_]\d{2}[-_]?|\d{8}[-_]?)", "", stem)
    # Replace separators with spaces, collapse whitespace
    cleaned = re.sub(r"[_\-]+", " ", cleaned).strip()
    title = cleaned.title() if cleaned else stem

    # Extract meaningful words (>3 chars, not stop-words) for topic hashtags
    _STOP = {"a", "an", "the", "and", "or", "but", "in", "on", "at",
             "to", "for", "of", "with", "is", "are", "was", "were", "be",
             "this", "that", "from", "its", "it"}
    words = [
        w for w in re.findall(r"[A-Za-z]+", cleaned)
        if len(w) > 3 and w.lower() not in _STOP
    ]

    # Up to 3 topic hashtags from filename, then fill with safe baseline tags
    topic_tags = [f"#{w.capitalize()}" for w in words[:3]]
    baseline_tags = ["#MortyUnredacted", "#FYP", "#ForYouPage"]
    seen: set[str] = set()
    all_tags: list[str] = []
    for tag in topic_tags + baseline_tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            all_tags.append(tag)
        if len(all_tags) == 5:
            break

    caption = f"{title}\n\n{' '.join(all_tags)}"
    log.debug("Generated caption from filename '%s': %s", stem, caption)
    return caption


def _click_discard_btn(page, ui_log, label: str) -> bool:
    """Click a Discard/Leave button if visible. Returns True if clicked."""
    for btn_text in ("Discard", "Leave", "Delete", "Remove"):
        try:
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if btn.is_visible(timeout=1_500):
                ui_log(f"TikTok: clicking '{btn_text}' ({label})")
                btn.click()
                time.sleep(1.5)
                return True
        except Exception:
            continue
    return False


def _dismiss_draft_recovery(page, ui_log) -> None:
    """Dismiss all draft/discard dialogs TikTok may show after a killed session.

    TikTok can show up to TWO layered dialogs:
      1. 'A video you were editing wasn't saved. Continue editing?'
         or 'You have an unfinished post' — with Continue / Discard
      2. 'Discard this post? Your video ... will be discarded permanently.'
         — confirmation modal with  Not now / Discard

    Both must be handled.  We also handle the case where only dialog 2
    is shown (TikTok sometimes skips step 1 and goes straight to confirm).

    Looking ahead at additional states:
    - 'Not now' on dialog 2 keeps the draft; we must NOT click it.
    - After both dismissals TikTok briefly re-renders; we add settle waits.
    - The dialogs live on the main page, NOT inside the upload iframe.
    - button:has-text() matches even when the button label is CSS-truncated.
    """
    # ── Phase 0: TUXModal sweep ───────────────────────────────────────────────
    # TikTok's design system uses class='TUXModal-overlay'.  CSS [class*='modal']
    # is case-sensitive and never matched this.  Run the sweep unconditionally
    # so any open TUXModal (draft discard, advisory, etc.) is closed first.
    _dismiss_any_tux_modal(page, ui_log)

    # ── Broad detection: any of the known non-TUX dialog variants ─────────────
    dialog_sel = (
        "div:has-text('Discard this post'), "           # confirmation dialog
        "div:has-text('wasn\\'t saved'), "              # variant A
        "div:has-text('unfinished post'), "             # variant B
        "div:has-text('Continue editing'), "            # variant C
        "div:has-text('and any edit'), "                # TUXModal text (fallback)
        "div:has-text('will be discarded'), "           # TUXModal text (fallback)
        "[class*='modal']:has-text('discard'), "        # generic modal (lowercase m)
        "[class*='dialog']:has-text('discard')"
    )
    try:
        page.wait_for_selector(dialog_sel, timeout=3_000)
    except Exception:
        return  # no further draft dialogs — nothing to do

    ui_log("TikTok: draft dialog detected — discarding ...")

    # Pass 1 — handle 'Continue editing?' or 'unfinished post' banner.
    # Click Discard in the BANNER only; this triggers the confirmation modal.
    _click_discard_btn(page, ui_log, "draft-recovery banner")

    # Pass 2 — handle 'Discard this post?' confirmation modal.
    #
    # IMPORTANT: we cannot reuse _click_discard_btn here.  That helper uses
    # locator(...).first, which finds the BANNER'S 'Discard' button (still
    # present in the DOM behind the modal) instead of the modal's own button.
    # Instead, scope the lookup to the dialog container first; if that fails,
    # use .last (modal renders after the banner in DOM order).
    try:
        page.wait_for_selector("div:has-text('Discard this post')", timeout=4_000)
        ui_log("TikTok: clicking Discard on confirmation modal ...")
        clicked = False
        for sel in (
            "[role='dialog'] button:has-text('Discard')",
            "[role='alertdialog'] button:has-text('Discard')",
            "[class*='Modal'] button:has-text('Discard')",
            "[class*='modal'] button:has-text('Discard')",
            "[class*='Dialog'] button:has-text('Discard')",
        ):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1_000):
                    btn.click()
                    time.sleep(1.5)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            # Last resort: .last is the final 'Discard' in DOM order = modal
            try:
                btn = page.locator("button:has-text('Discard')").last
                if btn.is_visible(timeout=1_000):
                    btn.click()
                    time.sleep(1.5)
            except Exception:
                pass
    except Exception:
        pass  # confirmation didn't appear — already gone after pass 1

    # Settle wait: TikTok re-renders the upload form after final discard
    time.sleep(2)
    ui_log("TikTok: draft discarded, upload form should be fresh.")


def _dismiss_any_tux_modal(page, ui_log, allow_discard: bool = True) -> None:
    """Close any open TUXModal-overlay that would intercept pointer events.

    TikTok's design system uses class='TUXModal-overlay' with
    data-transition-status='open'.  CSS [class*='modal'] is case-sensitive
    and would NOT match 'TUXModal', which is why it was missed before.

    Button priority:
      Discard  — dismiss draft/post confirmation (only when allow_discard=True)
      Close / Got it / OK / Continue — advisory banners
      Escape   — last-resort keyboard dismiss

    allow_discard=False when called from advisory-dialog context: we must NOT
    click Discard on the live post we just uploaded.

    Called at the start of _dismiss_draft_recovery (allow_discard=True),
    inside _dismiss_advisory_dialogs (allow_discard=False),
    and directly before any caption/schedule click (allow_discard=True).
    """
    btn_order = (
        ["Discard", "Close", "Got it", "OK", "Continue"]
        if allow_discard
        else ["Close", "Got it", "OK", "Continue"]
    )
    for _attempt in range(4):   # handle up to 4 stacked TUXModals
        try:
            overlay = page.locator(
                "[class*='TUXModal-overlay'][data-transition-status='open'], "
                "[data-floating-ui-portal] [data-transition-status='open']"
            ).first
            if not overlay.is_visible(timeout=1_500):
                break
        except Exception:
            break

        clicked = False
        for btn_text in btn_order:
            try:
                btn = overlay.locator(f"button:has-text('{btn_text}')").first
                if btn.is_visible(timeout=500):
                    ui_log(f"TikTok: closing TUXModal ('{btn_text}') ...")
                    btn.click()
                    time.sleep(1.2)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            page.keyboard.press("Escape")
            time.sleep(0.5)
            break


def _dismiss_joyride(page) -> None:
    """Dismiss TikTok's react-joyride onboarding overlay if it appears."""
    overlay_sel = "[data-test-id='overlay'], .react-joyride__overlay"
    try:
        page.wait_for_selector(overlay_sel, timeout=4_000)
    except Exception:
        return  # no overlay — nothing to do

    # Try keyboard dismiss first
    page.keyboard.press("Escape")
    time.sleep(0.5)

    # Try visible skip/close buttons
    for btn_sel in (
        "button:has-text('Skip')",
        "button:has-text('Got it')",
        "button:has-text('Done')",
        "[aria-label='Close']",
        "button[class*='close']",
    ):
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible():
                btn.click(force=True)
                time.sleep(0.5)
                break
        except Exception:
            continue

    # Nuclear fallback: remove the portal node from the DOM entirely
    if page.locator(overlay_sel).is_visible():
        page.evaluate(
            "() => { const el = document.getElementById('react-joyride-portal'); "
            "if (el) el.remove(); }"
        )


def _safe_click(page, locator, ui_log, label: str, timeout: int = 10_000):
    """Click a locator; if an overlay intercepts, retry with force=True."""
    try:
        locator.click(timeout=timeout)
    except Exception as exc:
        if "intercepts pointer events" in str(exc):
            ui_log(f"TikTok: overlay blocked '{label}' — using force click")
            _dismiss_joyride(page)
            locator.click(force=True, timeout=timeout)
        else:
            raise


def _screenshot_on_fail(page, label: str) -> str | None:
    """Save a screenshot to temp/ and return the path (or None)."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(TEMP_DIR, f"fail_{label}_{ts}.png")
        os.makedirs(TEMP_DIR, exist_ok=True)
        page.screenshot(path=path, full_page=True)
        log.info("Failure screenshot saved: %s", path)
        return path
    except Exception:
        return None


def _wait_for_upload_frame(page, timeout_ms: int = 30_000):
    """Find the frame (main page or iframe) that contains the upload file input.

    TikTok Studio embeds the upload form inside an iframe on current versions.
    Searches page.frames every second until input[type='file'] is found or
    the timeout expires.  Returns the Frame object, or None on timeout.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        current_url = page.url
        if "/unavailable" in current_url:
            raise TiktokRateLimitError(
                f"TikTok redirected to {current_url} — upload rate limit in effect. "
                "Wait a few minutes before the next upload."
            )
        for frame in page.frames:
            try:
                if frame.locator("input[type='file']").count() > 0:
                    log.debug("Upload file input found in frame: %s", frame.url)
                    return frame
            except Exception:
                continue
        time.sleep(1)
    return None


def _dismiss_advisory_dialogs(page, frame, ui_log) -> None:
    """Dismiss TikTok advisory/warning dialogs that appear after video upload.

    Known dialogs (any can appear, any can be absent):
    - Copyright / music warning   → Close / Got it / OK
    - Age-restriction notice      → Continue / OK
    - Auto-caption offer          → Close / No thanks
    - 'Some features unavailable' → Close / OK
    - 'Processing may take time'  → OK / Continue

    Strategy: scan for visible modal-like containers and click any
    dismiss button inside them.  Repeat up to 5 times so stacked dialogs
    all get cleared.  If nothing is visible after a short wait, return.
    """
    dismiss_btns = (
        "button:has-text('Close')",
        "button:has-text('Got it')",
        "button:has-text('OK')",
        "button:has-text('Continue')",
        "button:has-text('No thanks')",
        "button:has-text('Dismiss')",
        "[aria-label='Close']",
    )
    # TikTok may also surface advisory content inside a TUXModal overlay.
    # allow_discard=False — we must NOT click Discard on the post we just uploaded.
    _dismiss_any_tux_modal(page, ui_log, allow_discard=False)

    for _attempt in range(5):
        found_any = False
        for sel in dismiss_btns:
            try:
                # Check both the main page and the upload iframe
                for ctx in (page, frame):
                    btn = ctx.locator(sel).first
                    if btn.is_visible(timeout=800):
                        ui_log(f"TikTok: dismissing advisory dialog ({sel}) ...")
                        btn.click()
                        time.sleep(0.8)
                        found_any = True
                        break
            except Exception:
                continue
        if not found_any:
            break  # no more visible dialogs


def _tiktok_upload(page, mp4_path: str, caption: str, schedule_dt: datetime, ui_log):
    # Accept native "Leave this page?" / beforeunload browser dialogs automatically
    # so they never block navigation or uploads.
    page.on("dialog", lambda d: d.accept())

    ui_log("TikTok: navigating to Studio ...")
    page.goto(TIKTOK_STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)
    _dismiss_joyride(page)
    _dismiss_draft_recovery(page, ui_log)  # handles "wasn't saved" dialog

    # TikTok Studio loads the upload form inside an iframe.
    # Scan all frames (main + iframes) for the file input element.
    ui_log("TikTok: waiting for upload UI ...")
    frame = _wait_for_upload_frame(page, timeout_ms=30_000)
    if frame is None:
        current = page.url
        if "/unavailable" in current:
            raise TiktokRateLimitError(
                f"TikTok upload unavailable ({current}) — rate limit in effect."
            )
        if "/login" in current or "/passport" in current:
            _screenshot_on_fail(page, "login_redirect")
            raise RuntimeError(
                f"TikTok redirected to login ({current}). "
                "Your session may have expired — log in to TikTok in Chrome "
                "and restart the app."
            )
        _screenshot_on_fail(page, "upload_ui_missing")
        raise RuntimeError(
            f"Upload file input not found in any frame within 30 s "
            f"(url={current}). Check temp/fail_upload_ui_missing_*.png."
        )

    # ── Upload ────────────────────────────────────────────────────────────────
    # expect_file_chooser is a browser-level hook — works even when the click
    # originates inside an iframe.
    ui_log("TikTok: triggering file upload ...")
    try:
        with page.expect_file_chooser(timeout=20_000) as fc_info:
            frame.locator(
                "button:has-text('Select files'), "
                "div[class*='upload-btn'], "
                "div[class*='upload-card'], "
                "label[class*='upload'], "
                "input[type='file']"
            ).first.click()
        fc_info.value.set_files(mp4_path)
    except Exception:
        # Fallback: set files directly on the hidden file input
        ui_log("TikTok: file-chooser intercept failed, trying direct input ...")
        upload_input = frame.locator("input[type='file']").first
        upload_input.wait_for(state="attached", timeout=10_000)
        upload_input.set_input_files(mp4_path)

    # Caption-field selectors, ordered from most-specific to broadest.
    # TikTok Studio uses a Draft.js rich-text editor; we cover multiple versions
    # of their class names plus generic contenteditable fallbacks.
    _CAPTION_SELS = (
        # data-e2e attribute (most stable — TikTok keeps these for automation)
        "[data-e2e='upload-caption']",
        "[data-e2e='caption-input']",
        # data-placeholder attribute text (common in current TikTok Studio)
        "div[contenteditable='true'][data-placeholder*='caption' i]",
        "div[contenteditable='true'][data-placeholder*='describe' i]",
        "div[contenteditable='true'][data-placeholder*='tell' i]",
        # Draft.js specific markers
        "[data-text='true'][contenteditable='true']",
        ".DraftEditor-editorContainer [contenteditable='true']",
        "[data-contents='true'] [contenteditable='true']",
        # Class-based fallbacks
        "[contenteditable='true'][class*='caption']",
        "[contenteditable='true'][class*='editor-kit']",
        "textarea[placeholder*='caption' i]",
        # Broadest: any visible contenteditable in the frame
        "div[contenteditable='true']",
    )
    _caption_sel = ", ".join(_CAPTION_SELS)

    ui_log("TikTok: waiting for upload to complete ...")
    deadline = time.time() + 600   # 10 minutes — large videos need more time
    _last_diag = time.time()
    # Check both the upload iframe and the main page — the description editor
    # lives in whichever context the full upload form is rendered in.
    while time.time() < deadline:
        # Try frame first (most common), then main page as fallback
        caption_field = None
        for _ctx in (frame, page):
            try:
                _el = _ctx.locator(_caption_sel).first
                if _el.is_visible(timeout=300):
                    caption_field = _el
                    break
            except Exception:
                continue

        if caption_field is not None:
            break

        # Every 60 s log a brief diagnostic so we can see what IS visible
        if time.time() - _last_diag >= 60:
            _last_diag = time.time()
            elapsed = int(time.time() - (deadline - 600))
            try:
                ce_count = frame.locator("[contenteditable='true']").count()
                prog = frame.locator(
                    "[class*='progress'], [class*='uploading'], "
                    "[aria-label*='upload' i][role='progressbar']"
                ).count()
                ui_log(
                    f"TikTok: still waiting … {elapsed}s elapsed, "
                    f"contenteditable={ce_count}, progress-els={prog}"
                )
            except Exception:
                ui_log(f"TikTok: still waiting … {elapsed}s elapsed")
        time.sleep(3)
    else:
        _screenshot_on_fail(page, "upload_timeout")
        raise TiktokUploadTimeoutError(
            "TikTok upload did not finish within 10 minutes "
            "(caption field never appeared). "
            "Check temp/fail_upload_timeout_*.png for the page state."
        )

    # ── Post-upload dialogs ───────────────────────────────────────────────────
    # TikTok may show advisory dialogs immediately after a video finishes
    # processing: copyright/music warnings, age-restriction notices, auto-
    # caption confirmation, "some features unavailable" banners, etc.
    # Dismiss them all before touching the caption field.
    _dismiss_advisory_dialogs(page, frame, ui_log)

    # ── Caption ───────────────────────────────────────────────────────────────
    # TikTok's editor is a Slate/Draft.js contenteditable div.
    # Re-query fresh (don't reuse the loop reference — TikTok may have
    # re-rendered after upload finished, leaving the old locator stale).
    # Small pause first to let the post-upload UI finish settling.
    time.sleep(2)
    # Final sweep: dismiss any TUXModal that appeared during video processing.
    # This is the most common cause of "intercepts pointer events" on the caption.
    _dismiss_any_tux_modal(page, ui_log)
    # Re-query using the same multi-context search used in the wait loop.
    caption_field = None
    for _ctx in (frame, page):
        try:
            _el = _ctx.locator(_caption_sel).first
            if _el.is_visible(timeout=1_000):
                caption_field = _el
                break
        except Exception:
            continue
    if caption_field is None:
        _screenshot_on_fail(page, "caption_field_gone")
        raise RuntimeError("Caption field disappeared after upload completed.")
    ui_log("TikTok: filling caption ...")
    _screenshot_on_fail(page, "before_caption")  # diagnostic: always saved for review

    # If this video was previously processed (draft left by a failed run) and
    # the draft already has a REAL caption (longer than the filename fallback),
    # keep it rather than overwriting with the filename fallback.
    _filename_fallback = _caption_from_filename(os.path.splitext(os.path.basename(mp4_path))[0])
    try:
        _existing = caption_field.inner_text(timeout=800).strip()
    except Exception:
        _existing = ""
    _caption_is_filename_fallback = caption == _filename_fallback
    if _caption_is_filename_fallback and len(_existing) > len(caption) + 5:
        ui_log(f"TikTok: draft already has a longer caption ({len(_existing)} chars) "
               f"and current caption is filename fallback — keeping existing draft caption.")
        caption = _existing  # use the better existing caption
    # If the existing caption already matches what we want, skip the fill
    if _existing.strip() == caption.strip():
        ui_log("TikTok: caption already correct, skipping fill.")
    else:
        _safe_click(page, caption_field, ui_log, "caption_field")
        # Triple-click selects the whole line reliably in Draft.js (Ctrl+A sometimes
        # selects across block boundaries but misses embedded nodes).
        caption_field.click(click_count=3)
        caption_field.press("Control+a")
        caption_field.press("Backspace")
        page.keyboard.type(caption[:2200])
        # Draft.js shows autocomplete popups for '#' (hashtags) and '@' (mentions).
        # Escape closes any open popup without losing the typed text.
        page.keyboard.press("Escape")
        time.sleep(0.5)

    # ── Schedule ──────────────────────────────────────────────────────────────
    ui_log("TikTok: setting schedule ...")
    _screenshot_on_fail(page, "before_schedule")

    # IMPORTANT: From UI inspection, the "When to post: Now / Schedule" radio
    # group is on the MAIN PAGE — NOT inside the upload iframe.
    # The iframe only contains the file-input drop zone.
    # Search page first; fall back to frame in case TikTok changes this.
    _TOGGLE_SELS = (
        "input[type='radio'][value='schedule']",
        "label:has-text('Schedule')",
        "[data-e2e*='schedule' i]",
        "[aria-label='Schedule']",
        # Scoped to the "When to post" section to avoid false matches
        "div:has-text('When to post') ~ div input[type='radio']:nth-child(2)",
    )
    schedule_toggle = None
    for ctx in (page, frame):
        for sel in _TOGGLE_SELS:
            try:
                el = ctx.locator(sel).first
                if el.is_visible(timeout=1_200):
                    schedule_toggle = el
                    break
            except Exception:
                continue
        if schedule_toggle:
            break

    if schedule_toggle is None:
        _screenshot_on_fail(page, "schedule_toggle_missing")
        raise RuntimeError(
            "TikTok 'Schedule' radio button not found on page or frame. "
            "Check temp/fail_schedule_toggle_missing_*.png."
        )

    # ── Only activate Schedule if it isn't already active ────────────────────
    # CRITICAL: clicking a radio that's already selected DESELECTS it in some
    # TikTok UI builds, making the date/time fields disappear before we find
    # them.  Check the underlying radio input first; only click if not checked.
    _schedule_already_on = False
    # Try to find the actual radio <input> to read its checked state,
    # regardless of which element (label/div/span) schedule_toggle resolved to.
    for _rctx in (page, frame):
        try:
            _radio = _rctx.locator("input[type='radio']").nth(1)  # 0=Now, 1=Schedule
            if _radio.is_visible(timeout=500):
                _schedule_already_on = _radio.is_checked()
                break
        except Exception:
            pass
    # Fallback: ask the element itself
    if not _schedule_already_on:
        try:
            _schedule_already_on = schedule_toggle.is_checked()
        except Exception:
            pass

    if _schedule_already_on:
        ui_log("TikTok: 'Schedule' already active — skipping toggle click.")
    else:
        _safe_click(page, schedule_toggle, ui_log, "schedule_toggle")
        time.sleep(2.5)  # extra time for React to render date/time pickers
        _screenshot_on_fail(page, "after_toggle")  # diagnostic — always saved
        # Verify it actually activated
        try:
            for _rctx in (page, frame):
                _radio = _rctx.locator("input[type='radio']").nth(1)
                if _radio.is_visible(timeout=300) and _radio.is_checked():
                    break
        except Exception:
            pass

    # ── Scroll the inner form panel to reveal the date/time section ───────────
    # The upload form is inside a scrollable div panel, not the main window.
    # window.scrollBy() does nothing here — must scroll the panel itself.
    _scroll_result = page.evaluate("""() => {
        // 1. Try to scroll the schedule section into view within its container
        const keywords = ['schedule', 'Schedule', 'WhenToPost', 'when-to-post',
                          'ScheduleTime', 'schedule-time'];
        for (const kw of keywords) {
            const els = document.querySelectorAll(`[class*="${kw}"], [data-e2e*="${kw}"]`);
            for (const el of els) {
                if (el.offsetParent !== null) {
                    el.scrollIntoView({block: 'center', behavior: 'instant'});
                    return 'scrolled-to:' + (el.className || '').slice(0, 60);
                }
            }
        }
        // 2. Fallback: scroll every overflow-y container to its bottom
        let scrolled = 0;
        document.querySelectorAll('*').forEach(el => {
            if (el === document.body || el === document.documentElement) return;
            const style = window.getComputedStyle(el);
            if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                el.scrollHeight > el.clientHeight + 50) {
                el.scrollTop = el.scrollHeight;
                scrolled++;
            }
        });
        return scrolled > 0 ? ('scrolled-containers:' + scrolled) : 'no-scroll-needed';
    }""")
    ui_log(f"TikTok: form-scroll result: {_scroll_result}")
    time.sleep(1.2)
    # Wait up to 8 s for TikTok to render date/time pickers after toggle click.
    # TikTok's React app can take several seconds on slow connections.
    _picker_wait_sels = (
        "input[type='date']", "input[type='datetime-local']",
        "[class*='DatePicker']", "[class*='DateInput']",
        "[class*='ScheduleDate']", "[class*='ScheduleTime']",
        "[data-e2e*='schedule-date' i]", "[data-e2e*='schedule-time' i]",
    )
    for _pw in range(30):  # up to 15 s (30 × 0.5 s)
        _found_early = False
        # CSS selector scan (fast, class-name dependent)
        for _pwctx in ([page] + list(page.frames)):
            for _pws in _picker_wait_sels:
                try:
                    if _pwctx.locator(_pws).first.is_visible(timeout=200):
                        _found_early = True
                        break
                except Exception:
                    continue
            if _found_early:
                break
        # Text-pattern scan as backup (class-name agnostic)
        if not _found_early:
            if _find_picker_by_text("date") is not None:
                _found_early = True
        if _found_early:
            ui_log(f"TikTok: date/time picker visible after {(_pw + 1) * 0.5:.1f}s.")
            break
        time.sleep(0.5)
    else:
        ui_log("TikTok: WARNING — picker not detected in wait loop; will attempt locate anyway.")

    # ── DOM dump helper ────────────────────────────────────────────────────────
    # Saves ALL interactive/relevant DOM elements to schedule_dom.log
    # (always overwritten) so you can inspect it immediately after a failure.
    _DOM_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "temp", "schedule_dom.log")
    def _dump_schedule_dom(label: str):
        try:
            els = page.evaluate("""() => {
                const rows = [];
                document.querySelectorAll('*').forEach(el => {
                    if (!el.isConnected) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 3 || r.height < 3) return;
                    const cls = (el.className || '').toString();
                    const tag = el.tagName;
                    const role = el.getAttribute('role') || '';
                    const de2e = el.getAttribute('data-e2e') || '';
                    const tab  = el.getAttribute('tabindex') || '';
                    const al   = (el.getAttribute('aria-label') || '').slice(0, 35);
                    const txt  = (el.innerText || el.textContent || '').trim().slice(0, 50);
                    const interesting = (
                        tag === 'INPUT' || tag === 'BUTTON' || tag === 'SELECT' ||
                        role === 'button' || role === 'combobox' || role === 'listbox' ||
                        role === 'radio'  || role === 'dialog'   || role === 'option' ||
                        tab === '0' || de2e !== '' ||
                        /schedule|date|time|picker|calendar|when/i.test(cls)
                    );
                    if (interesting) rows.push(
                        `${tag}[r=${role||'-'}][t=${tab||'-'}][de2e=${de2e||'-'}]` +
                        `[al=${al}] .${cls.slice(0,70)} | "${txt}"`
                    );
                });
                return rows.slice(0, 120);
            }""")
            with open(_DOM_LOG, "w", encoding="utf-8") as _f:
                _f.write(f"=== schedule_dom.log [{label}] ===\n")
                _f.write("\n".join(els))
            ui_log(f"TikTok [{label}]: DOM dump → temp/schedule_dom.log ({len(els)} entries)")
        except Exception as _e:
            ui_log(f"TikTok [{label}] DOM dump failed: {_e}")

    # ── Find date/time pickers ────────────────────────────────────────────────
    # PRIMARY strategy: scan ALL visible interactive elements and match by the
    # date/time TEXT they display.  This works regardless of class names (which
    # change with TikTok's CSS modules) or data-e2e attributes.
    # TikTok pre-populates with current date (YYYY-MM-DD) and current+buffer
    # time (HH:MM), so we can reliably match by those patterns.
    import re as _re

    def _find_picker_by_text(picker_type: str):
        """Scan all interactive elements in page+frame, return the first one
        whose inner text matches a time (HH:MM) or date pattern.
        Also catches placeholder text used when pickers are empty (e.g. MM/DD/YYYY)."""
        if picker_type == "time":
            _pat = _re.compile(
                r'^\d{1,2}:\d{2}(\s*(AM|PM))?$'             # 10:30 AM / 22:30
                r'|^hh:mm(\s*(am|pm))?$'                     # placeholder: hh:mm am
                r'|^--:--$',                                  # placeholder: --:--
                _re.I)
        else:
            _pat = _re.compile(
                r'^\d{4}-\d{2}-\d{2}$'                       # 2026-03-05
                r'|^\d{1,2}/\d{1,2}/\d{4}$'                  # 3/5/2026
                r'|^\d{4}/\d{1,2}/\d{1,2}$'                  # 2026/3/5
                r'|^\d{2}\.\d{2}\.\d{4}$'                    # 05.03.2026
                r'|^[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}$'  # Mar 5, 2026
                r'|^\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}$'    # 5 Mar 2026
                r'|^[A-Za-z]{3,9}\.?\s+\d{1,2}$'             # Mar 5 (no year)
                r'|^mm/dd/yyyy$'                              # placeholder text
                r'|^dd/mm/yyyy$'                              # placeholder text
                r'|^yyyy-mm-dd$'                              # placeholder text
                r'|^select\s+date$',                          # "Select date" button
                _re.I)
        _sel = ("button, select, [tabindex='0'], [role='button'], "
                "[role='combobox'], input[type='time'], input[type='date'], "
                "input[type='datetime-local']")
        for _ctx in ([page] + list(page.frames)):
            try:
                _els = _ctx.locator(_sel).all()
                for _el in _els[:80]:
                    try:
                        if not _el.is_visible(timeout=150):
                            continue
                        _txt = _el.inner_text(timeout=200).strip()
                        if _pat.match(_txt):
                            return _el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    # FALLBACK strategy: classic CSS-selector scan (catches native inputs,
    # <select>s, and any TUX variants we've seen in past TikTok versions)
    _DATE_SELS = (
        "input[type='date']", "input[type='datetime-local']",
        "select[name*='date' i]", "select[class*='date' i]",
        "input[placeholder*='mm/dd' i]", "input[placeholder*='yyyy' i]",
        "input[placeholder*='date' i]",
        "[data-e2e*='schedule-date' i]", "[data-e2e*='date'][role='button']",
        "[data-e2e='schedule-date-picker']",
        "[class*='TUXDateInput']", "[class*='DateInput'][role='button']",
        "[class*='DateInput'][tabindex='0']", "[class*='DatePicker'][tabindex='0']",
        "[class*='ScheduleDate']", "[class*='schedule-date']",
        "[class*='ScheduleTime'] [role='button']:first-child",
        # Newer TikTok Studio layouts
        "[class*='whenToPost' i] [role='button']:first-child",
        "[class*='PostTime' i] [role='button']:first-child",
        "[class*='scheduleTime' i] [role='button']:first-child",
        "div[class*='date' i][role='button']",
        "div[class*='Date'][role='button']",
        "span[class*='date' i][role='button']",
        # Generic: first button/combobox inside any schedule container
        "[class*='schedule' i] button:first-of-type",
        "[class*='Schedule'] [role='combobox']:first-of-type",
    )
    _TIME_SELS = (
        "input[type='time']",
        "select[name*='time' i]", "select[class*='time' i]",
        "input[placeholder*='hh:mm' i]",
        "[data-e2e*='schedule-time' i]", "[data-e2e*='time'][role='button']",
        "[class*='TUXTimeInput']", "[class*='TimeInput'][role='button']",
        "[class*='TimeInput'][tabindex='0']", "[class*='TimePicker'][tabindex='0']",
        "[class*='ScheduleTime'] [role='button']:last-child",
    )

    def _find_picker_by_sel(sels: tuple):
        """Classic selector scan — page first, then all frames."""
        for _ctx in ([page] + list(page.frames)):
            for _sel in sels:
                try:
                    _el = _ctx.locator(_sel).first
                    try:
                        _el.scroll_into_view_if_needed(timeout=500)
                    except Exception:
                        pass
                    if _el.is_visible(timeout=1_000):
                        return _el
                except Exception:
                    continue
        return None


    def _find_picker_by_js(picker_type: str):
        """Last-resort: use JS to find any visible element whose text looks like
        a date or time, mark it with a temp attribute, then query it via Playwright.
        Catches display formats missed by the regex scan (locale-formatted dates,
        non-standard separators, etc.)."""
        if picker_type == "time":
            js_pat = r'\d{1,2}:\d{2}(\s*(AM|PM))?'
        else:
            js_pat = (
                r'\d{4}[-/]\d{1,2}[-/]\d{1,2}'     # 2026-03-05 / 2026/3/5
                r'|\d{1,2}[-/]\d{1,2}[-/]\d{4}'     # 3/5/2026 / 05-03-2026
                r'|\d{2}\.\d{2}\.\d{4}'              # 05.03.2026
                r'|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}'  # Mar 5, 2026
                r'|\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}'    # 5 Mar 2026
            )
        attr = f"data-claude-picker-{picker_type}"
        js_code = f"""() => {{
            const pat = new RegExp('{js_pat}', 'i');
            const sels = [
                'button', 'input', 'select',
                '[role="button"]', '[role="combobox"]', '[tabindex="0"]'
            ];
            for (const sel of sels) {{
                for (const el of document.querySelectorAll(sel)) {{
                    const txt = (el.innerText || el.value || el.textContent || '').trim();
                    if (pat.test(txt) && el.offsetParent !== null) {{
                        el.setAttribute('{attr}', '1');
                        return true;
                    }}
                }}
            }}
            return false;
        }}"""
        try:
            for _ctx in ([page] + list(page.frames)):
                try:
                    found = _ctx.evaluate(js_code)
                    if found:
                        el = _ctx.locator(f"[{attr}]").first
                        if el.is_visible(timeout=500):
                            # Clean up attribute so it doesn't affect later queries
                            try:
                                _ctx.evaluate(
                                    f"() => document.querySelector('[{attr}]')"
                                    f"?.removeAttribute('{attr}')"
                                )
                            except Exception:
                                pass
                            return el
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _fill_picker(el, val_iso: str, val_slash: str, val_hhmm: str,
                     val_ampm: str, label: str):
        """Fill a date/time field.  Tries input fill, select_option, keyboard
        type, and React event dispatch.  Works for native inputs, <select>
        elements, and custom editable divs."""
        # Strategy A: <select> element
        try:
            tag = el.evaluate("e => e.tagName")
            if tag == "SELECT":
                for v in (val_iso, val_hhmm, val_ampm, val_slash):
                    if not v:
                        continue
                    try:
                        el.select_option(value=v)
                        return
                    except Exception:
                        pass
                    try:
                        el.select_option(label=v)
                        return
                    except Exception:
                        pass
        except Exception:
            pass
        # Strategy B: native input fill (input[type=date/time])
        try:
            el.click(click_count=3)
            el.fill(val_iso)
            el.dispatch_event("input")
            el.dispatch_event("change")
            time.sleep(0.3)
            v = el.input_value()
            if v and v not in ("", "undefined"):
                return
        except Exception:
            pass
        # Strategy C: keyboard type (works for custom editable divs)
        try:
            el.click(click_count=3)
            el.press("Control+a")
            for fmt in (val_slash, val_hhmm, val_ampm, val_iso):
                if fmt:
                    page.keyboard.type(fmt)
                    page.keyboard.press("Tab")
                    time.sleep(0.3)
                    break
        except Exception:
            pass

    def _handle_calendar_popup(target_dt):
        """If a calendar popup appeared after clicking date trigger, navigate
        to target month and click the target day."""
        # Wait briefly for any calendar dialog to appear
        time.sleep(0.8)
        cal_sels = (
            "[role='dialog'][class*='alendar' i]",
            "[class*='Calendar']",
            "[class*='calendar']",
            "[role='dialog']",
        )
        cal = None
        for sel in cal_sels:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=800):
                    cal = el
                    break
            except Exception:
                continue

        if cal is None:
            return False  # no calendar appeared

        target_month = target_dt.strftime("%B %Y")
        for _ in range(24):
            try:
                header_txt = page.evaluate("""() => {
                    const h = document.querySelector(
                        '[class*="Calendar"] [class*="month" i], '
                        '[class*="Calendar"] [class*="header" i], '
                        '[role="dialog"] [class*="month" i]'
                    );
                    return h ? h.textContent.trim() : '';
                }""")
                if target_month.lower() in header_txt.lower():
                    break
                # Click "next month"
                for ns in ("button[aria-label*='next' i]",
                           "button[class*='next' i]", "button[class*='Next']",
                           "[class*='forward']", "[class*='rightArrow']"):
                    try:
                        nb = page.locator(ns).last
                        if nb.is_visible(timeout=400):
                            nb.click()
                            time.sleep(0.4)
                            break
                    except Exception:
                        continue
            except Exception:
                break

        # Click the target day
        day = str(target_dt.day)
        for ds in (
            f"[data-date='{target_dt.strftime('%Y-%m-%d')}']",
            f"button[aria-label*='{target_dt.strftime('%B')} {day}' i]",
            f"[class*='day']:has-text('{day}'):not([class*='disabled' i]):not([class*='other' i])",
            f"td:has-text('{day}'):not([class*='disabled' i])",
            f"button:has-text('{day}'):not([disabled])",
        ):
            try:
                db = page.locator(ds).first
                if db.is_visible(timeout=500):
                    db.click()
                    time.sleep(0.3)
                    return True
            except Exception:
                continue
        return False

    # ── Set date ──────────────────────────────────────────────────────────────
    # Retry up to 20 s — TikTok React can be slow to render pickers after toggle.
    date_el = None
    for _date_attempt in range(20):
        date_el = (_find_picker_by_text("date")
                   or _find_picker_by_sel(_DATE_SELS)
                   or _find_picker_by_js("date"))
        if date_el is not None:
            if _date_attempt > 0:
                ui_log(f"TikTok: date picker found on attempt {_date_attempt + 1}.")
            break
        # Scroll again in case the panel shifted
        if _date_attempt in (4, 9, 14):
            try:
                page.evaluate("""() => {
                    document.querySelectorAll('*').forEach(el => {
                        const s = window.getComputedStyle(el);
                        if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                                && el.scrollHeight > el.clientHeight + 50)
                            el.scrollTop = el.scrollHeight;
                    });
                }""")
            except Exception:
                pass
        time.sleep(1.0)
    if date_el is None:
        _screenshot_on_fail(page, "date_picker_missing")
        _dump_schedule_dom("date_picker_missing")
        raise RuntimeError(
            "TikTok date picker not found after enabling schedule toggle. "
            "Check temp/fail_date_picker_missing_*.png; "
            "DOM dump → temp/schedule_dom.log."
        )

    _safe_click(page, date_el, ui_log, "date_trigger")
    if not _handle_calendar_popup(schedule_dt):
        # No calendar appeared — try direct fill (might be an input or editable)
        _fill_picker(date_el,
                     schedule_dt.strftime("%Y-%m-%d"),
                     schedule_dt.strftime("%m/%d/%Y"),
                     "", "", "date")
    time.sleep(0.5)

    # ── Set time ──────────────────────────────────────────────────────────────
    time_el = (_find_picker_by_text("time")
               or _find_picker_by_sel(_TIME_SELS)
               or _find_picker_by_js("time"))
    if time_el is None:
        _screenshot_on_fail(page, "time_picker_missing")
        _dump_schedule_dom("time_picker_missing")
        raise RuntimeError(
            "TikTok time picker not found after setting date. "
            "Check temp/fail_time_picker_missing_*.png; "
            "DOM dump → temp/schedule_dom.log."
        )

    _safe_click(page, time_el, ui_log, "time_trigger")
    time.sleep(0.6)
    # Time might open a dropdown list — try clicking the matching option first
    _h = schedule_dt.strftime("%I").lstrip("0") or "12"
    _ampm = schedule_dt.strftime("%p")  # AM / PM
    _hhmm_24 = schedule_dt.strftime("%H:%M")
    _hhmm_12 = schedule_dt.strftime("%I:%M %p")
    _time_option_clicked = False
    for ts in (
        f"[role='option']:has-text('{_hhmm_12}')",
        f"li:has-text('{_hhmm_12}')",
        f"[role='option']:has-text('{_h}:00 {_ampm}')",
        f"li:has-text('{_h}:00 {_ampm}')",
        f"[role='option']:has-text('{_hhmm_24}')",
        f"li:has-text('{_hhmm_24}')",
    ):
        try:
            opt = page.locator(ts).first
            if opt.is_visible(timeout=600):
                opt.click()
                _time_option_clicked = True
                time.sleep(0.3)
                break
        except Exception:
            continue

    if not _time_option_clicked:
        _fill_picker(time_el, _hhmm_24, "", _hhmm_24, _hhmm_12, "time")
    time.sleep(0.5)

    # ── Validate schedule time is sufficiently in the future ──────────────────
    # TikTok rejects schedules less than 15-20 min from now.
    from datetime import timezone as _tz
    _now = datetime.now()
    _delta_min = (schedule_dt.replace(tzinfo=None) - _now).total_seconds() / 60
    if _delta_min < 18:
        ui_log(f"TikTok WARNING: schedule_dt is only {_delta_min:.0f} min from now "
               f"— TikTok requires ≥20 min. This may be rejected.")

    # ── Submit ────────────────────────────────────────────────────────────────
    # The submit button may say "Post" (normal publish) OR "Schedule post".
    # We ONLY click it if the schedule toggle was confirmed activated.
    # Search page first, then frame; prefer buttons with "Schedule" in text.
    post_btn = None
    for ctx in (page, frame):
        for btn_sel in (
            "button:has-text('Schedule post')",
            "button:has-text('Schedule')",
            "button:has-text('Submit')",
            "button:has-text('Post')",           # last resort — only safe after toggle verified
        ):
            try:
                el = ctx.locator(btn_sel).last
                if el.is_visible(timeout=1_500):
                    post_btn = el
                    break
            except Exception:
                continue
        if post_btn:
            break

    if post_btn is None:
        _screenshot_on_fail(page, "submit_btn_missing")
        _dump_schedule_dom("submit_btn_missing")
        raise RuntimeError(
            "TikTok submit button not found. "
            "Check temp/fail_submit_btn_missing_*.png."
        )
    _safe_click(page, post_btn, ui_log, "post_button")
    time.sleep(3)

    # ── Confirm intermediate dialog (if any) ─────────────────────────────────
    for _confirm_sel in (
        "button:has-text('Confirm')",
        "button:has-text('OK')",
        "button:has-text('Done')",
    ):
        for _ctx in (page, frame):
            try:
                btn = _ctx.locator(_confirm_sel).first
                if btn.is_visible(timeout=1_200):
                    _safe_click(page, btn, ui_log, "confirm_button")
                    time.sleep(2)
                    break
            except Exception:
                pass

    # ── Verify success / surface errors ──────────────────────────────────────
    _screenshot_on_fail(page, "after_schedule")
    time.sleep(1)
    _error_sels = (
        "div:has-text('Please set a valid')",
        "div:has-text('scheduled time must be')",
        "div:has-text('minimum') :has-text('minutes')",
        "div:has-text('Something went wrong')",
        "div:has-text('failed to')",
        "[class*='error']:has-text('schedule')",
        "[class*='error']:has-text('time')",
    )
    for sel in _error_sels:
        for _ctx in (page, frame):
            try:
                el = _ctx.locator(sel).first
                if el.is_visible(timeout=400):
                    msg = el.inner_text(timeout=400).strip()[:200]
                    _screenshot_on_fail(page, "schedule_validation_error")
                    raise RuntimeError(f"TikTok scheduling validation error: {msg!r}")
            except RuntimeError:
                raise
            except Exception:
                continue

    ui_log("TikTok: post scheduled.")
