"""Gradio live-status UI — http://localhost:7860"""

import os
import queue
import threading
import time
from typing import Callable
import gradio as gr
import db

_run_now_fn: Callable | None = None
_youtube_job_fn: Callable | None = None
_youtube_full_run: bool = False

_log_queue: queue.Queue = queue.Queue()
_log_lines: list[str] = []
_MAX_LINES = 500
_LOG_FILE = os.path.join(os.path.dirname(__file__), "app.log")


_ERROR_KEYWORDS = ("error", "failed", "warning", "warn", "rate-limit", "unavailable")


def ui_log(message: str) -> None:
    """Called from watcher threads to append a timestamped log line."""
    ts = time.strftime("%H:%M:%S")
    prefix = ">>> " if any(k in message.lower() for k in _ERROR_KEYWORDS) else "    "
    line = f"[{ts}] {prefix}{message}"
    _log_queue.put(line)


def _drain_queue():
    while not _log_queue.empty():
        _log_lines.append(_log_queue.get_nowait())
    # Keep only the last _MAX_LINES entries
    if len(_log_lines) > _MAX_LINES:
        del _log_lines[: len(_log_lines) - _MAX_LINES]


def _get_latest_line() -> str:
    _drain_queue()
    return _log_lines[-1] if _log_lines else "(waiting...)"


def _get_log_text() -> str:
    _drain_queue()  # keep in-memory list current for _get_latest_line
    # Read the actual log file so ALL entries (not just ui_log calls) are shown,
    # then reverse so newest lines appear at the top of the textbox.
    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tail = lines[-_MAX_LINES:]  # cap at 500 lines
        return "".join(reversed(tail)).rstrip() or "(log file empty)"
    except FileNotFoundError:
        pass
    # Fallback to in-memory if file not yet created
    if not _log_lines:
        return "(waiting for activity...)"
    return "\n".join(reversed(_log_lines))


def _run_now() -> str:
    if _run_now_fn is None:
        return "Run Now callback not registered."
    t = threading.Thread(target=_run_now_fn, daemon=True)
    t.start()
    return "Job triggered — check log for progress."


def _reset_failed_videos() -> str:
    count = db.reset_tiktok_failed()
    msg = f"Reset {count} failed video(s) — they will be retried on the next poll."
    ui_log(msg)
    return msg


def _reset_all_videos() -> str:
    count = db.reset_tiktok_all()
    msg = f"Reset ALL {count} video record(s) — every Drive video will be re-processed on the next poll."
    ui_log(msg)
    return msg


def _monetize_youtube(full_run: bool) -> str:
    if _youtube_job_fn is None:
        return "YouTube Monetizer callback not registered."
    # Pass full_run flag to the job function
    t = threading.Thread(target=lambda: _youtube_job_fn(full_run), daemon=True)
    t.start()
    mode = "full run" if full_run else "test mode (1 video)"
    return f"YouTube Monetizer triggered ({mode}) — check log for progress."


def _toggle_youtube_full_run(value: bool) -> bool:
    global _youtube_full_run
    _youtube_full_run = value
    return value


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="TikTok Auto — M0rty Unredacted", theme=gr.themes.Soft()) as demo:
        gr.Markdown("## TikTok Automation App — M0rty Unredacted\nDrive → TikTok Studio scheduler")

        # Single-line "latest" display — always visible regardless of scroll
        gr.Textbox(
            label="Latest",
            value=_get_latest_line,
            lines=1,
            max_lines=1,
            interactive=False,
            every=5,
        )

        log_box = gr.Textbox(
            label="Full Log  (newest first — scroll up for recent; >>> = error/warning)",
            value=_get_log_text,
            lines=28,
            max_lines=28,
            interactive=False,
            every=5,  # refresh every 5 seconds
        )

        with gr.Row():
            run_now_btn  = gr.Button("▶ Run Now",            variant="primary")
            reset_btn    = gr.Button("Reset Failed Videos",  variant="secondary")
            reset_all_btn = gr.Button("Reset ALL Videos",    variant="stop")
            action_out   = gr.Textbox(label="", interactive=False, scale=3)
        run_now_btn.click(_run_now,              outputs=action_out)
        reset_btn.click(_reset_failed_videos,    outputs=action_out)
        reset_all_btn.click(_reset_all_videos,   outputs=action_out)

        gr.Markdown(
            "_TikTok Scheduler polls Drive every 10 min and auto-schedules new videos._  \n"
            "_**▶ Run Now** triggers an immediate poll (reloads config.json — no restart needed after changing folder ID)._  \n"
            "_**Reset Failed Videos** unblocks videos stuck as 'failed' so they retry on the next poll._"
        )

        gr.Markdown("---")
        gr.Markdown("### YouTube Monetizer — bulk-enable monetization")

        with gr.Row():
            youtube_full_run_toggle = gr.Checkbox(
                label="Full run (all videos)",
                value=_youtube_full_run,
                interactive=True,
            )
            monetize_btn = gr.Button("💰 Monetize All", variant="secondary")
            youtube_action_out = gr.Textbox(label="", interactive=False, scale=3)

        youtube_full_run_toggle.change(_toggle_youtube_full_run, inputs=youtube_full_run_toggle, outputs=youtube_full_run_toggle)
        monetize_btn.click(
            lambda: _monetize_youtube(_youtube_full_run),
            outputs=youtube_action_out
        )

        gr.Markdown(
            "_**💰 Monetize All** (test mode) processes 1 video to test._  \n"
            "_Enable **Full run** to process all videos in your channel._  \n"
            "_Reuses the same Chrome session as TikTok Scheduler._"
        )

    return demo


def launch(share: bool = False, run_now_fn: Callable | None = None, youtube_job_fn: Callable | None = None) -> None:
    """Launch Gradio in a daemon thread so it doesn't block the main loop."""
    global _run_now_fn, _youtube_job_fn
    _run_now_fn = run_now_fn
    _youtube_job_fn = youtube_job_fn
    demo = build_ui()

    def _run():
        demo.launch(
            server_name="127.0.0.1",
            server_port=7860,
            share=share,
            quiet=True,
            prevent_thread_lock=True,
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ui_log("Gradio UI started at http://localhost:7860")
