import os
import threading
import time

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

    def start(self):
        """Start playing the pre-recorded audio file."""
        if self.is_playing:
            return {"status": "already_playing", "show_id": self.show_id}

        if not os.path.exists(self.audio_file_path):
            return {"status": "error", "message": "Audio file not found"}

        self.is_playing = True

        # Set state to indicate playback is active
        with state.sync_lock:
            state.currently_playing_show_id = self.show_id
            state.is_playback_active = True

        # Start playback thread
        self.playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self.playback_thread.start()

        return {"status": "started", "show_id": self.show_id}

    def stop(self):
        """Stop playback."""
        self.is_playing = False

        with state.sync_lock:
            state.is_playback_active = False
            state.currently_playing_show_id = None

        if self.playback_thread:
            self.playback_thread.join(timeout=5)
            self.playback_thread = None

        return {"status": "stopped", "show_id": self.show_id}

    def get_progress(self) -> dict:
        """Get current playback progress."""
        with state.sync_lock:
            return {
                "show_id": self.show_id,
                "is_playing": self.is_playing,
                "currently_playing_show_id": state.currently_playing_show_id,
            }

    def _playback_loop(self):
        """Internal loop that reads the audio file and queues it for streaming."""
        import wave

        try:
            # Audit 4.2 Fix: Use a with block to ensure file is closed even if loop fails
            with wave.open(self.audio_file_path, "rb") as wav_file:
                sample_rate = wav_file.getframerate()
                channels = wav_file.getnchannels()
                sampwidth = wav_file.getsampwidth()

                chunk_size = 4096  # bytes per chunk
                frames_per_chunk = chunk_size // (sampwidth * channels)

                while self.is_playing:
                    try:
                        # Read audio data
                        data = wav_file.readframes(frames_per_chunk)
                        if not data:
                            # End of file — restart from beginning
                            if self.is_playing:
                                wav_file.rewind()  # Audit 4.2 Fix: use rewind instead of close/reopen
                                data = wav_file.readframes(frames_per_chunk)
                            else:
                                break

                        # Audit 4.3/5.4 Fix: Skip local queue, broadcast directly to mixer
                        try:
                            state.broadcast_audio(data)
                        except Exception:
                            pass

                        # Simulate realtime playback
                        time.sleep(frames_per_chunk / sample_rate)

                    except Exception as e:
                        print(f"Playback error: {e}")
                        break
        except Exception as e:
            print(f"Failed to open audio file for playback: {e}")
        finally:
            self.is_playing = False
            with state.sync_lock:
                state.is_playback_active = False
                state.currently_playing_show_id = None


# ReMixInterface removed (Audit 3.1)
