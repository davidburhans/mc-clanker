import os
import threading
import queue

from app.framework.framework_state import state


class ShowPlayback:
    """
    Handles playback of pre-recorded show audio files.
    Uses the existing audio streaming infrastructure.
    """

    def __init__(self, show_id: int, audio_file_path: str, db_session=None):
        self.show_id = show_id
        self.audio_file_path = audio_file_path
        self.db_session = db_session
        self.is_playing = False
        self.playback_thread = None
        self.audio_queue = queue.Queue(maxsize=100)

    def start(self):
        """Start playing the pre-recorded audio file."""
        if self.is_playing:
            return {"status": "already_playing", "show_id": self.show_id}

        if not os.path.exists(self.audio_file_path):
            return {"status": "error", "message": "Audio file not found"}

        self.is_playing = True

        # Set state to indicate playback is active
        with state.lock:
            state.currently_playing_show_id = self.show_id
            state.is_playback_active = True

        # Start playback thread
        self.playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self.playback_thread.start()

        return {"status": "started", "show_id": self.show_id}

    def stop(self):
        """Stop playback."""
        self.is_playing = False

        with state.lock:
            state.is_playback_active = False
            state.currently_playing_show_id = None

        if self.playback_thread:
            self.playback_thread.join(timeout=5)
            self.playback_thread = None

        return {"status": "stopped", "show_id": self.show_id}

    def get_progress(self) -> dict:
        """Get current playback progress."""
        with state.lock:
            return {
                "show_id": self.show_id,
                "is_playing": self.is_playing,
                "currently_playing_show_id": state.currently_playing_show_id,
            }

    def _playback_loop(self):
        """Internal loop that reads the audio file and queues it for streaming."""
        import wave
        import numpy as np

        try:
            wav_file = wave.open(self.audio_file_path, "rb")
            try:
                sample_rate = wav_file.getframerate()
                channels = wav_file.getnchannels()
                sampwidth = wav_file.getsampwidth()

                chunk_size = 4096  # bytes per chunk

                while self.is_playing:
                    try:
                        # Read audio data
                        data = wav_file.readframes(chunk_size // (sampwidth * channels))
                        if not data:
                            # End of file — restart from beginning
                            if self.is_playing:
                                wav_file.close()
                                wav_file = wave.open(self.audio_file_path, "rb")
                                data = wav_file.readframes(chunk_size // (sampwidth * channels))
                            else:
                                break

                        # Convert to PCM16 if needed
                        if sampwidth == 2:
                            pcm_data = data
                        else:
                            # Convert other formats to int16
                            audio_data = np.frombuffer(data, dtype=np.int16)
                            pcm_data = audio_data.tobytes()

                        # Add to queue for streaming
                        self.audio_queue.put_nowait(pcm_data)

                    except queue.Full:
                        # Skip if queue is full (client too slow)
                        continue
                    except Exception as e:
                        print(f"Playback error: {e}")
                        break
            finally:
                wav_file.close()

        except Exception as e:
            print(f"Failed to open audio file for playback: {e}")
        finally:
            self.is_playing = False
            with state.lock:
                state.is_playback_active = False
                state.currently_playing_show_id = None


class ReMixInterface:
    """
    Rich remix interface for regenerating/remixing recorded shows.
    Future phase implementation.
    """

    def __init__(self, show_id: int, db_session=None):
        self.show_id = show_id
        self.db_session = db_session

    def get_remix_context(self) -> dict:
        """Get the context needed for the remix interface."""
        return {
            "show_id": self.show_id,
            "message": "Remix interface - full implementation in future phase",
        }

    def regenerate_stem(self, loop_index: int, stem_index: int, params: dict) -> dict:
        """Regenerate a specific stem from a specific loop."""
        return {
            "status": "not_implemented",
            "message": "Remix interface - full implementation in future phase",
        }
