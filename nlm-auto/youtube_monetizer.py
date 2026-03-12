"""YouTube Monetizer — bulk-enable monetization across YouTube Studio videos.

Reuses the same Chrome CDP session (localhost:9222) as TikTok Scheduler.
Navigates to YouTube Studio, finds all videos, and enables monetization on eligible ones.
"""

import logging
import time

import chrome_client

log = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9222"
YOUTUBE_STUDIO_URL = "https://studio.youtube.com/channel/UCL6oELUoWo66pID2QwJwQ1g/videos"


def run_monetizer(test_mode: bool = False, ui_log=None) -> None:
    """
    Main entry point for YouTube Monetizer.

    Args:
        test_mode: If True, process exactly 1 video then stop.
                  If False, process all videos in the content list.
        ui_log: Optional UI logging callback (same signature as tiktok_scheduler).
    """
    if ui_log is None:
        ui_log = lambda msg: log.info(msg)

    monetized_count = 0
    skipped_count = 0
    already_on_count = 0

    try:
        page = chrome_client.new_page(CDP_URL)
        ui_log("[MONETIZER] Connecting to YouTube Studio...")

        # Navigate to YouTube Studio videos page
        page.goto(YOUTUBE_STUDIO_URL, wait_until="networkidle")
        ui_log("[MONETIZER] Navigated to YouTube Studio.")

        # Wait for content table to load (wait for actual video row elements)
        try:
            page.wait_for_selector("ytcp-video-row", timeout=15000)
            ui_log("[MONETIZER] Content table loaded.")
        except Exception:
            ui_log("[MONETIZER] Timeout waiting for video rows — page may have no videos.")
            return

        # Process videos with pagination support
        processed_first = False
        while True:
            video_rows = _get_all_video_rows(page)

            if not video_rows:
                ui_log("[MONETIZER] No more video rows found.")
                break

            ui_log(f"[MONETIZER] Found {len(video_rows)} visible video rows.")

            for row_idx in range(len(video_rows)):
                # Always re-fetch rows to avoid stale element refs
                video_rows = _get_all_video_rows(page)
                if row_idx >= len(video_rows):
                    # Row count decreased (e.g., due to filtering/reload) — stop
                    ui_log(f"[MONETIZER] Row list changed size; stopping at row {row_idx}.")
                    break

                row = video_rows[row_idx]

                try:
                    # Extract video title from the row
                    title = _extract_title_from_row(row)
                    ui_log(f"[MONETIZER] Processing video {row_idx + 1}: '{title}'")

                    # Click 3-dot menu and Edit
                    _click_edit_menu(row, page)
                    time.sleep(1)

                    # Check monetization toggle state
                    toggle_state = _check_toggle_state(page, title, ui_log)

                    if toggle_state == "already_on":
                        already_on_count += 1
                    elif toggle_state == "disabled_or_review":
                        skipped_count += 1
                    elif toggle_state == "can_enable":
                        _enable_monetization(page, title, ui_log)
                        monetized_count += 1
                    else:
                        skipped_count += 1

                    # Close the edit panel
                    _close_edit_panel(page)
                    time.sleep(2)

                    if test_mode:
                        ui_log(f"[MONETIZER] Test mode: stopping after first video.")
                        processed_first = True
                        break

                except Exception as exc:
                    ui_log(f"[MONETIZER] Error processing row {row_idx}: {exc}")
                    try:
                        _close_edit_panel(page)
                    except Exception:
                        pass
                    skipped_count += 1
                    if test_mode:
                        processed_first = True
                        break

            if processed_first:
                break

            # Scroll to bottom to load more rows
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

            new_rows = _get_all_video_rows(page)
            if len(new_rows) <= len(video_rows):
                # No new rows loaded — reached the end
                ui_log("[MONETIZER] No new rows after scroll; pagination complete.")
                break

    except Exception as exc:
        log.exception("YouTube Monetizer fatal error")
        ui_log(f"[MONETIZER] FATAL: {exc}")
        return

    # Final summary
    total = monetized_count + skipped_count + already_on_count
    ui_log(f"[MONETIZER] Done — {monetized_count} monetized, {skipped_count} skipped, {already_on_count} already on (total: {total} processed).")


def _get_all_video_rows(page):
    """Get all currently visible video rows. Always re-query to avoid stale refs."""
    try:
        rows = page.locator("ytcp-video-row").all()
        return rows
    except Exception:
        return []


def _extract_title_from_row(row):
    """Extract video title from row element."""
    try:
        # Title is typically in a link or text element within the row
        title_locator = row.locator("a[href*='/videos/'][title]").first
        if title_locator.is_visible():
            return title_locator.get_attribute("title") or "Unknown Title"
    except Exception:
        pass

    try:
        # Fallback: look for any text in the row
        text = row.text_content()
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return lines[0] if lines else "Unknown Title"
    except Exception:
        return "Unknown Title"


def _click_edit_menu(row, page):
    """Click the 3-dot menu on a video row and then click 'Edit'."""
    # Find and click the 3-dot menu button in the row
    menu_button = row.locator("button[aria-label*='options'], button[aria-label*='menu']").first

    if menu_button.is_visible():
        menu_button.click()
        time.sleep(0.5)

    # Wait for and click the Edit option
    try:
        edit_option = page.locator("yt-formatted-string:has-text('Edit'), tp-yt-paper-item:has-text('Edit')").first
        edit_option.click()
    except Exception:
        # Try alternative selector for Edit menu item
        page.click("text='Edit'")

    time.sleep(1)


def _check_toggle_state(page, title, ui_log):
    """
    Check the monetization toggle state in the edit panel.

    Returns:
        'already_on' — monetization is enabled
        'can_enable' — toggle exists and is disabled (aria-checked=false)
        'disabled_or_review' — toggle is disabled or "Under review" badge is present
        'unknown' — couldn't determine state
    """
    try:
        # Wait for the Monetization tab to be visible
        page.wait_for_selector("[role='tab']", timeout=5000)

        # Try to find and click the Monetization tab
        monetization_tabs = page.locator("[role='tab']:has-text('Monetization'), [role='tab']:has-text('monetization')").all()
        if monetization_tabs:
            monetization_tabs[0].click()
            time.sleep(1)
    except Exception:
        pass

    # Look for the toggle switch
    try:
        toggle = page.locator("yt-formatted-string[aria-label*='Monetization'] ~ [role='switch'], [role='switch'][aria-label*='monetization']").first
        if not toggle.is_visible():
            # Try another selector pattern
            toggle = page.locator("tp-yt-switch-renderer").first
    except Exception:
        toggle = None

    if not toggle or not toggle.is_visible():
        ui_log(f"[MONETIZER] {title} — skipped (no toggle found)")
        return "disabled_or_review"

    # Check for "Under review" badge
    try:
        under_review = page.locator("text='Under review'").is_visible()
        if under_review:
            ui_log(f"[MONETIZER] {title} — skipped (under review)")
            return "disabled_or_review"
    except Exception:
        pass

    # Check toggle aria-checked state
    try:
        is_checked = toggle.get_attribute("aria-checked")

        if is_checked == "true":
            ui_log(f"[MONETIZER] {title} — already monetized, skip")
            return "already_on"
        elif is_checked == "false":
            # Check if toggle is disabled (aria-disabled)
            is_disabled = toggle.get_attribute("aria-disabled") == "true"
            if is_disabled:
                ui_log(f"[MONETIZER] {title} — skipped (toggle disabled)")
                return "disabled_or_review"
            else:
                ui_log(f"[MONETIZER] {title} — toggle ready to enable")
                return "can_enable"
    except Exception:
        pass

    ui_log(f"[MONETIZER] {title} — skipped (unknown state)")
    return "unknown"


def _enable_monetization(page, title, ui_log):
    """Enable the monetization toggle and wait for save confirmation."""
    try:
        # Find and click the toggle
        toggle = page.locator("[role='switch'][aria-checked='false']").first
        if toggle.is_visible():
            toggle.click()
            ui_log(f"[MONETIZER] {title} — clicked toggle")
            time.sleep(1)

        # Click the Save button
        _click_save_and_wait(page, title, ui_log)

    except Exception as exc:
        ui_log(f"[MONETIZER] {title} — error enabling: {exc}")


def _click_save_and_wait(page, title, ui_log):
    """Click Save button and wait for success confirmation (toast or panel close)."""
    try:
        # Find and click Save button
        save_button = page.locator("button:has-text('Save'), yt-button-renderer:has-text('Save')").first
        if save_button.is_visible():
            save_button.click()
            ui_log(f"[MONETIZER] {title} — clicked Save")
            time.sleep(1)

        # Wait for success indicator (toast message or panel close)
        try:
            # Wait for "Saved" or success toast
            page.wait_for_selector("text='Saved', text='Changes saved'", timeout=5000)
            ui_log(f"[MONETIZER] {title} — monetized ✓")
        except Exception:
            # If no toast appears, assume save succeeded and panel will close
            ui_log(f"[MONETIZER] {title} — monetized (save triggered)")

    except Exception as exc:
        ui_log(f"[MONETIZER] {title} — save error: {exc}")


def _close_edit_panel(page):
    """Close the edit panel by clicking the back/close button or pressing Escape."""
    try:
        # Try clicking a back button
        back_button = page.locator("button[aria-label*='back'], button[aria-label*='close']").first
        if back_button.is_visible():
            back_button.click()
            time.sleep(1)
        else:
            # Try pressing Escape to close the panel
            page.press("Escape")
            time.sleep(1)
    except Exception:
        # If all else fails, just press Escape
        try:
            page.press("Escape")
        except Exception:
            pass
