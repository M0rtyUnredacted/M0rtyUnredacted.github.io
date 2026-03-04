"""Gradio live-status UI — http://localhost:7860"""

import queue
import threading
import time
import gradio as gr

_log_queue: queue.Queue = queue.Queue()
_log_lines: list[str] = []
_MAX_LINES = 500


def ui_log(message: str) -> None:
    """Called from watcher threads to append a timestamped log line."""
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    _log_queue.put(line)


def _drain_queue():
    while not _log_queue.empty():
        _log_lines.append(_log_queue.get_nowait())
    # Keep only the last _MAX_LINES entries
    if len(_log_lines) > _MAX_LINES:
        del _log_lines[: len(_log_lines) - _MAX_LINES]


def _get_log_text() -> str:
    _drain_queue()
    return "\n".join(_log_lines) if _log_lines else "(waiting for activity...)"


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="TikTok Auto — M0rty Unredacted", theme=gr.themes.Soft()) as demo:
        gr.Markdown("## TikTok Automation App — M0rty Unredacted\nDrive → TikTok Studio scheduler")

        log_box = gr.Textbox(
            label="Live Status Log",
            value=_get_log_text,
            lines=30,
            max_lines=30,
            interactive=False,
            every=5,  # refresh every 5 seconds
        )

        gr.Markdown(
            "_TikTok Scheduler polls Drive every 10 min and auto-schedules new videos_"
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
