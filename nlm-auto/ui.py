"""Gradio live-status UI — http://localhost:7860"""

import queue
import threading
import time
import gradio as gr
import db

_log_queue: queue.Queue = queue.Queue()
_log_lines: list[str] = []
_MAX_LINES = 500


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


def _get_log_text() -> str:
    _drain_queue()
    if not _log_lines:
        return "(waiting for activity...)"
    return "\n".join(reversed(_log_lines))  # newest at top


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


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="TikTok Auto — M0rty Unredacted", theme=gr.themes.Soft()) as demo:
        gr.Markdown("## TikTok Automation App — M0rty Unredacted\nDrive → TikTok Studio scheduler")

        log_box = gr.Textbox(
            label="Live Status Log  (newest first — >>> = error/warning)",
            value=_get_log_text,
            lines=30,
            max_lines=30,
            interactive=False,
            every=5,  # refresh every 5 seconds
        )

        with gr.Row():
            reset_btn = gr.Button("Reset Failed Videos", variant="secondary")
            reset_all_btn = gr.Button("Reset ALL Videos", variant="stop")
            reset_out = gr.Textbox(label="", interactive=False, scale=3)
        reset_btn.click(_reset_failed_videos, outputs=reset_out)
        reset_all_btn.click(_reset_all_videos, outputs=reset_out)

        gr.Markdown(
            "_TikTok Scheduler polls Drive every 10 min and auto-schedules new videos._  \n"
            "_>>> prefix = error or warning.  Click **Reset Failed Videos** to unblock "
            "videos stuck as 'failed' so they retry on the next poll._"
        )

    return demo


def launch(share: bool = False) -> None:
    """Launch Gradio in a daemon thread so it doesn't block the main loop."""
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
