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
    print("FASTAPI LIFESPAN: Initializing database...")
    from db import DatabaseManager
    db_manager = DatabaseManager.get_instance()
    db_manager.create_tables()

    print("FASTAPI LIFESPAN: Starting framework loop...")
    framework_thread = threading.Thread(target=run_framework_loop, daemon=True)
    framework_thread.start()
    yield
    # Shutdown logic
    print("FASTAPI LIFESPAN: Shutting down resources...")
    state.trigger_shutdown()

def cleanup():
    print("Application exiting, cleaning up...")
    state.is_running = False

atexit.register(cleanup)

from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request, Response
import base64
import re


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Import here to avoid circular imports
        from auth import decode_token
        from db import DatabaseManager
        from models import Show

        auth_header = request.headers.get("Authorization")
        current_user = None

        # Try JWT Bearer token first
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            payload = decode_token(token)
            if payload and "sub" in payload:
                user_id = int(payload["sub"])
                db_manager = DatabaseManager.get_instance()
                with db_manager.session() as session:
                    user = session.query(type("User", (), {"id": int, "username": str, "email": str, "is_active": bool, "to_dict": lambda s: {"id": s.id, "username": s.username, "email": s.email}}) ).filter_by(id=user_id).first()
                    if user and user.is_active:
                        # Attach user to request state
                        request.state.user = user
                        current_user = user

        # If not JWT, try HTTP Basic auth with env vars (backwards compatibility)
        dj_pass = getattr(state, "dj_password", "")
        aud_pass = getattr(state, "audience_password", "")

        if current_user is None:
            if not dj_pass and not aud_pass:
                # No auth configured
                return await call_next(request)

            provided_pass = None
            if auth_header and auth_header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                    if ":" in decoded:
                        _, provided_pass = decoded.split(":", 1)
                except:
                    pass

            if provided_pass is None:
                # Check if this route requires auth
                is_dj_route = path.startswith("/dj") or \
                              (path.startswith("/api/") and request.method == "POST") or \
                              path.startswith("/api/llm-config") or \
                              path.startswith("/api/stems")
                is_audience_route = path == "/" or \
                                    path == "/index.html" or \
                                    path == "/styles.css" or \
                                    path == "/app.js" or \
                                    path.startswith("/stream.mp3") or \
                                    (path.startswith("/api/") and request.method == "GET")

                # If auth is required for this route but no credentials provided, reject
                if (is_dj_route and dj_pass) or (is_audience_route and aud_pass):
                    return Response(
                        "Unauthorized",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Restricted"'},
                    )

                # Create a pseudo-user for backwards compat when env vars are set
                class CompatUser:
                    id = 0
                    username = "djCompat"
                    email = "compat@local"
                    is_active = True
                    def to_dict(self):
                        return {"id": 0, "username": "djCompat", "email": "compat@local"}

                request.state.user = CompatUser()
                current_user = CompatUser()

        def needs_auth(realm="Restricted"):
            return Response(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{realm}"'},
            )

        # Check for per-show audience password in path
        # Pattern: /api/shows/{id}/playback/* or /api/shows/{id}/audio
        show_password_match = re.match(r"^/api/shows/(\d+)/(playback|audio)(\/.*)?$", path)
        if show_password_match:
            show_id = int(show_password_match.group(1))
            db_manager = DatabaseManager.get_instance()
            with db_manager.session() as session:
                show = session.query(Show).filter(Show.id == show_id).first()
                if show and show.audience_password_hash:
                    # Extract password from Basic auth
                    provided_pass = None
                    if auth_header and auth_header.startswith("Basic "):
                        try:
                            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                            if ":" in decoded:
                                _, provided_pass = decoded.split(":", 1)
                        except:
                            pass

                    if provided_pass:
                        from auth import verify_password
                        if not verify_password(provided_pass, show.audience_password_hash):
                            return needs_auth(f"Show {show_id}")
                    else:
                        return needs_auth(f"Show {show_id}")
                elif show:
                    # Show exists but no password - allow access
                    pass
                else:
                    # Show not found - let the route handle 404
                    pass

        # Check for DJ routes (require auth if env var is set)
        dj_pass = getattr(state, "dj_password", "")
        is_dj_route = path.startswith("/dj") or \
                      (path.startswith("/api/") and request.method == "POST") or \
                      path.startswith("/api/llm-config") or \
                      path.startswith("/api/stems")

        is_audience_route = path == "/" or \
                            path == "/index.html" or \
                            path == "/styles.css" or \
                            path == "/app.js" or \
                            path.startswith("/stream.mp3") or \
                            (path.startswith("/api/") and request.method == "GET")

        # Legacy env-var password check (only if no JWT user)
        if current_user and current_user.id == 0:  # Compat user from env vars
            if is_dj_route and dj_pass:
                # Already verified above if provided_pass == dj_pass
                pass
            elif is_audience_route and aud_pass:
                # Already verified above
                pass

        return await call_next(request)

# 3. Build FastAPI App
app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)

# Register API routes for DJ UI
app.include_router(api_router)

# Mount static files for DJ UI
static_dir = os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "static", "mc-clanker"
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

    # Wait for actual audio data before starting FFmpeg
    # This prevents the browser from timing out waiting for MP3 frames
    # when is_generating=false (only silence being produced)
    try:
        first_chunk = client_q.get(timeout=300.0)
        if first_chunk is None:
            print("DEBUG: audio_stream_generator received poison pill before first chunk")
            state.remove_audio_client(client_q)
            return
    except queue.Empty:
        print("DEBUG: audio_stream_generator timed out waiting for first audio chunk")
        state.remove_audio_client(client_q)
        return

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

    # Pre-feed the first chunk we already received
    try:
        process.stdin.write(first_chunk)
        process.stdin.flush()
        print("DEBUG: Pre-fed first audio chunk to ffmpeg")
    except Exception as e:
        print(f"Warning: Could not pre-feed first chunk to ffmpeg: {e}")
        state.remove_audio_client(client_q)
        return

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
            "Accept-Ranges": "bytes",
        },
    )



# Mount static files for Audience UI at root
audience_dir = os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "static", "audience"
)
if os.path.exists(audience_dir):
    app.mount("/", StaticFiles(directory=audience_dir, html=True), name="audience_ui")


if __name__ == "__main__":
    class CustomServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame) -> None:
            print(f"CustomServer: Caught signal {sig}. Triggering state shutdown.")
            state.trigger_shutdown()
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host="0.0.0.0", port=7860)
    server = CustomServer(config)
    server.run()
