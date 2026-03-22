import gradio as gr
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
import threading
import queue
import time
import struct
import uvicorn
import os
import subprocess

from framework_state import state
from framework_main import run_framework_loop
from api_routes import router as api_router
import atexit
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    yield
    # Shutdown logic
    print("FASTAPI LIFESPAN: Shutting down resources...")
    state.trigger_shutdown()

# 1. Start the generator loop in a background thread
framework_thread = threading.Thread(target=run_framework_loop, daemon=True)
framework_thread.start()

def cleanup():
    print("Application exiting, cleaning up...")
    state.is_running = False

atexit.register(cleanup)

# Initial reasoning state to reflect loading
state.llm_reasoning = "⚙️ Initializing Engine... (Loading Foundation-1 Model, ~20s)"

# 2. Build the Gradio UI
with gr.Blocks(title="LLM Composer - Soundtrack for Life") as demo:
    gr.Markdown("# 🎵 Soundtrack for Life - LLM Composer Dashboard")

    with gr.Row():
        with gr.Column(scale=2):
            gr.Markdown("### 📻 Live Radio")
            # Embed HTML5 Audio player pointing to our FastAPI stream, with timestamp to bust cache
            gr.HTML(
                f'<audio id="radio_player" src="/stream.mp3?t={time.time()}" controls autoplay style="width: 100%; border-radius: 8px;"></audio>'
            )

            gr.Markdown("### 🧠 Conductor's Reasoning")
            reasoning_box = gr.Textbox(
                label="Strategy & Thoughts", interactive=False, lines=4
            )

            gr.Markdown("### 🎛️ Stem Timeline")
            with gr.Row():
                prev_stems_box = gr.JSON(label="Previous Stems")
                stems_box = gr.JSON(label="Currently Playing")
                next_stems_box = gr.JSON(label="Up Next (Generating...)")

        with gr.Column(scale=1):
            gr.Markdown("### 🎚️ DJ Controls")
            with gr.Row():
                bpm_box = gr.Number(label="Current Master BPM", interactive=False)
                key_box = gr.Textbox(label="Current Master Key", interactive=False)

            gr.Markdown("---")
            with gr.Accordion("⚙️ LLM Settings", open=False):
                llm_url_input = gr.Textbox(
                    label="API Base URL", value=state.llm_base_url
                )
                llm_key_input = gr.Textbox(
                    label="API Key", value=state.llm_api_key, type="password"
                )
                llm_model_input = gr.Textbox(label="Model Name", value=state.llm_model)
                save_settings_btn = gr.Button("Apply Settings")

                def save_llm_settings(url, key, model):
                    with state.lock:
                        state.llm_base_url = url
                        state.llm_api_key = key
                        state.llm_model = model
                    return gr.Info(
                        "LLM Settings Applied! Will take effect on next loop."
                    )

                save_settings_btn.click(
                    fn=save_llm_settings,
                    inputs=[llm_url_input, llm_key_input, llm_model_input],
                )

            gr.Markdown("#### Global Overrides")
            vibe_override = gr.Textbox(
                label="Vibe / Context Prompt",
                placeholder="e.g. 'Make it aggressive', 'Cyberpunk'",
            )

            with gr.Row():
                bpm_override = gr.Dropdown(
                    choices=[100, 110, 120, 128, 130, 140, 150],
                    label="Force Target BPM",
                )
                key_override = gr.Dropdown(
                    choices=[
                        "C major",
                        "C minor",
                        "D major",
                        "D minor",
                        "E major",
                        "E minor",
                        "F major",
                        "F minor",
                        "G major",
                        "G minor",
                        "A major",
                        "A minor",
                        "B major",
                        "B minor",
                    ],
                    label="Force Target Key",
                )

            submit_btn = gr.Button("Submit Override", variant="primary")

            start_btn = gr.Button("▶️ Start Engine", variant="primary")
            reset_btn = gr.Button("⚠️ Reset Engine", variant="stop")

            def start_engine():
                with state.lock:
                    state.is_generating = True
                gr.Info("Engine Started!")
                return gr.update(interactive=False)

            def reset_system():
                state.reset()
                return (
                    gr.update(interactive=True),
                    gr.update(value=""),
                    gr.update(value=None),
                    gr.update(value=None),
                )

            start_btn.click(fn=start_engine, outputs=[start_btn])
            reset_btn.click(
                fn=reset_system,
                outputs=[start_btn, vibe_override, bpm_override, key_override],
            )

            gr.Markdown("#### Instrument Rack")

            category_checkboxes = []
            for category, items in state.categorized_instruments.items():
                with gr.Accordion(
                    category,
                    open=(category == "Electronic & Dance" or category == "Rock & Pop"),
                ):
                    cb = gr.CheckboxGroup(choices=items, value=items, label=category)
                    category_checkboxes.append(cb)

            gr.Markdown("#### Add Custom Instrument")
            with gr.Row():
                custom_inst_input = gr.Textbox(
                    show_label=False, placeholder="e.g. Kazoo, 8-bit Synth, Didgeridoo"
                )
                add_inst_btn = gr.Button("Add")

            def add_custom(new_inst):
                if new_inst:
                    updated_cats = state.add_custom_instrument(new_inst.strip())
                    return gr.update(
                        choices=updated_cats["Custom"], value=updated_cats["Custom"]
                    ), ""
                return gr.update(), ""

            add_inst_btn.click(
                fn=add_custom,
                inputs=[custom_inst_input],
                outputs=[category_checkboxes[-1], custom_inst_input],
            )

            def submit_overrides(vibe, bpm, key, *categories):
                all_selected = []
                for cat_list in categories:
                    all_selected.extend(cat_list)

                with state.lock:
                    if vibe:
                        state.user_override = vibe
                    if bpm:
                        state.target_bpm_override = int(bpm)
                        if not state.is_generating:
                            state.current_bpm = int(bpm)
                    if key:
                        state.target_key_override = key
                        if not state.is_generating:
                            state.current_key = key
                    state.available_instruments = all_selected
                return gr.update(value=""), gr.update(value=None), gr.update(value=None)

            submit_btn.click(
                fn=submit_overrides,
                inputs=[vibe_override, bpm_override, key_override]
                + category_checkboxes,
                outputs=[vibe_override, bpm_override, key_override],
            )

    def update_ui():
        with state.lock:
            return (
                state.llm_reasoning,
                state.previous_stems,
                state.active_stems,
                state.next_stems,
                state.current_bpm,
                state.current_key,
                gr.update(interactive=not state.is_generating),
            )

    timer = gr.Timer(value=1.0)
    timer.tick(
        fn=update_ui,
        outputs=[
            reasoning_box,
            prev_stems_box,
            stems_box,
            next_stems_box,
            bpm_box,
            key_box,
            start_btn,
        ],
    )

# 3. Build FastAPI App and Mount Gradio
app = FastAPI(lifespan=lifespan)

# Register API routes for DJ UI
app.include_router(api_router)

# Mount static files for DJ UI
static_dir = os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "static", "slop_jockey"
)
if os.path.exists(static_dir):
    app.mount("/dj", StaticFiles(directory=static_dir, html=True), name="dj_ui")


@app.get("/dj")
def redirect_to_dj_slash():
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/dj/")


def audio_stream_generator():
    """Generator that yields infinite MP3 stream using ffmpeg for transcoding"""
    print("DEBUG: audio_stream_generator() called")
    client_q = queue.Queue(maxsize=100)
    state.add_audio_client(client_q)

    ffmpeg_exe = "/usr/bin/ffmpeg"
    if not os.path.exists(ffmpeg_exe):
        ffmpeg_exe = "ffmpeg"

    # Check if ffmpeg exists and has libmp3lame
    try:
        check = subprocess.run(
            [ffmpeg_exe, "-codecs"], capture_output=True, text=True, timeout=5
        )
        if "libmp3lame" not in check.stdout:
            print(
                "WARNING: ffmpeg does not have libmp3lame encoder. MP3 streaming may not work."
            )
    except Exception as e:
        print(f"WARNING: Could not verify ffmpeg capabilities: {e}")

    # ffmpeg tuned for low-latency streaming
    ffmpeg_cmd = [
        ffmpeg_exe,
        "-y",
        "-f",
        "s16le",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-i",
        "pipe:0",
        "-f",
        "mp3",
        "-acodec",
        "libmp3lame",
        "-b:a",
        "192k",
        "pipe:1",
    ]

    print(f"Starting audio stream with ffmpeg: {' '.join(ffmpeg_cmd)}")
    process = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Pre-feed some silence to ffmpeg so it can start encoding immediately
    # This prevents the browser from timing out waiting for first MP3 frame
    # 2048 samples * 2 channels * 2 bytes = 8192 bytes of silence
    silence_chunk = bytes(8192)
    try:
        process.stdin.write(silence_chunk)
        process.stdin.flush()
        print("DEBUG: Pre-fed silence to ffmpeg")
    except Exception as e:
        print(f"Warning: Could not pre-feed silence to ffmpeg: {e}")

    def feeder():
        try:
            while state.is_running:
                try:
                    chunk = client_q.get(timeout=1.0)
                    if chunk is None: # Poison pill
                        print("DEBUG: Feeder received poison pill")
                        break
                    if process.poll() is not None:
                        stderr = (
                            process.stderr.read().decode() if process.stderr else ""
                        )
                        print(f"FFmpeg process died. stderr: {stderr}")
                        break
                    process.stdin.write(chunk)
                    process.stdin.flush()
                except (queue.Empty, BrokenPipeError):
                    if not state.is_running:
                        break
                    continue
                except Exception as e:
                    print(f"Feeder error: {e}")
                    break
        finally:
            try:
                process.stdin.close()
            except:
                pass
            stderr = process.stderr.read().decode() if process.stderr else ""
            if stderr:
                print(f"FFmpeg stderr: {stderr}")

    # Register for cleanup
    state.register_subprocess(process)
    
    threading.Thread(target=feeder, daemon=True).start()

    try:
        bytes_yielded = 0
        initial_read = False
        while state.is_running:
            data = process.stdout.read(4096)
            if not data:
                stderr = process.stderr.read().decode() if process.stderr else ""
                print(f"FFmpeg stdout ended. stderr: {stderr}")
                break
            if not initial_read:
                print(f"DEBUG: First MP3 data received: {len(data)} bytes")
                initial_read = True
            bytes_yielded += len(data)
            yield data
        print(f"Audio stream ended. Total bytes yielded: {bytes_yielded}")
    finally:
        state.remove_audio_client(client_q)
        state.unregister_subprocess(process)
        try:
            process.kill()
            process.wait(timeout=2)
        except:
            pass
        print("Audio stream generator closed")


@app.get("/stream.mp3")
def stream_mp3():
    return StreamingResponse(
        audio_stream_generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    class CustomServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame) -> None:
            print(f"CustomServer: Caught signal {sig}. Triggering state shutdown.")
            state.trigger_shutdown()
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host="0.0.0.0", port=7860)
    server = CustomServer(config)
    server.run()
