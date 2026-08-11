import os
import tempfile
import threading
import wave
from unittest.mock import patch

from app.playback import ShowPlayback


class TestAuditFixes:
    """Tests for resource leaks and bugs identified in the audit."""

    def test_show_playback_context_manager(self):
        """
        Verify that ShowPlayback properly scopes the wave.open call
        and doesn't leak file handles when reopening.
        """
        temp_path = tempfile.mktemp(suffix=".wav")
        with wave.open(temp_path, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(b"\x00" * 1000)

        playback = ShowPlayback(show_id=1, audio_file_path=temp_path)
        playback.is_playing = True

        try:
            # We want to trace wave.open calls to ensure they are properly managed
            # If the code closes the wave file manual inside a `with` block without
            # creating a new context manager, it's a bug.
            # We'll patch wave.open to track invocations.
            original_wave_open = wave.open
            opened_files = []

            def mock_wave_open(*args, **kwargs):
                f = original_wave_open(*args, **kwargs)
                opened_files.append(f)
                return f

            with patch("wave.open", side_effect=mock_wave_open):
                with patch("app.playback.state"):
                    # Run the loop briefly
                    def stop_soon():
                        import time

                        time.sleep(0.1)
                        playback.is_playing = False

                    threading.Thread(target=stop_soon, daemon=True).start()
                    playback._playback_loop()

            # The test should pass if we refactor it.
            # To assert it was fixed, we verify no open files are leaked.
            for f in opened_files:
                # wave.Wave_read objects have a closed property or close() method
                # If they are closed, `f.getfp()` might return None
                fp = f.getfp()
                if hasattr(fp, "closed"):
                    assert fp.closed, "File handle was leaked!"
                elif fp is None:
                    # Successfully closed
                    pass

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_show_playback_audio_queue_blocking(self):
        """
        Verify that ShowPlayback no longer uses a local audio_queue that could
        block when full. The queue was removed in the audit fix — audio is
        broadcast directly to the mixer.
        """
        temp_path = tempfile.mktemp(suffix=".wav")
        with wave.open(temp_path, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            # Create a larger file
            wav.writeframes(b"\x00" * 1000000)

        playback = ShowPlayback(show_id=1, audio_file_path=temp_path)
        playback.is_playing = True

        try:
            with patch("app.playback.state"):
                # Verify audio_queue no longer exists (it was the source of the bug)
                assert not hasattr(playback, "audio_queue"), "audio_queue should be removed — it blocked when full"

                def stop_soon():
                    import time

                    time.sleep(0.1)
                    playback.is_playing = False

                threading.Thread(target=stop_soon, daemon=True).start()
                playback._playback_loop()
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
