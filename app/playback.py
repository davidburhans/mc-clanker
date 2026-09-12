import os
import threading
import time

import numpy as np

from app.framework.framework_state import state

# REL-32a: byte budget per broadcast chunk (matches the pre-conversion loop).
CHUNK_BYTES = 4096


def _wav_chunk_to_float(data: bytes, sampwidth: int) -> np.ndarray:
    """Decode one interleaved int-PCM frame block to float32 in [-1, 1].

    sampwidth 1 (unsigned 8-bit), 3 (24-bit, sign-extended unpack), and
    4 (int32) are handled; sampwidth 2 never reaches here (identity fast
    path in ``wav_chunk_to_s16le``).

    Raises:
        ValueError: if sampwidth is outside 1-4 (message names the value).
    """
    if sampwidth == 1:  # WAV 8-bit PCM is UNSIGNED with a 128 bias
        u8 = np.frombuffer(data, dtype=np.uint8)
        return (u8.astype(np.float32) - 128.0) / 128.0
    if sampwidth == 3:  # 24-bit: pad to int32 with sign extension
        u8 = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        pad = np.where(u8[:, 2] >= 0x80, np.uint8(0xFF), np.uint8(0x00))
        i32 = np.concatenate([u8, pad[:, None]], axis=1).view("<i4").ravel()
        return i32.astype(np.float32) / 8388608.0
    if sampwidth == 4:
        i32 = np.frombuffer(data, dtype="<i4")
        return i32.astype(np.float32) / 2147483648.0
    raise ValueError(f"Unsupported WAV sample width {sampwidth} (expected 1-4)")


def _float_block_to_s16le(block: np.ndarray) -> bytes:
    """Mixer-convention float -> s16le bytes (stems.py AUDIO-1 math).

    REL-21 NaN-safety is carried over: corrupt decoded samples become
    silence instead of wrap garbage.
    """
    sane = np.nan_to_num(block.astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
    return (np.clip(sane, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def wav_chunk_to_s16le(data: bytes, sampwidth: int) -> bytes:
    """REL-32a: normalize any int-PCM WAV chunk to the s16le the broadcast
    chain assumes (youtube_relay ``-f s16le``, stream fan-out, WAV sinks).

    sampwidth 2 — this app's recording format — passes through unchanged
    (byte-identical identity fast path).

    Raises:
        ValueError: if sampwidth is outside 1-4 (message names the value).
    """
    if sampwidth == 2:
        return data
    return _float_block_to_s16le(_wav_chunk_to_float(data, sampwidth))


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
        """Internal loop that reads the audio file and queues it for streaming.

        REL-32a: the broadcast chain assumes s16le, so every chunk is
        normalized via ``wav_chunk_to_s16le``. stdlib ``wave`` rejects
        non-PCM inputs (float32, format 3) at open time; those fall back
        to the scipy decoded streamer.
        """
        import wave

        try:
            # Audit 4.2 Fix: Use a with block to ensure file is closed even if loop fails
            with wave.open(self.audio_file_path, "rb") as wav_file:
                self._stream_wave(wav_file)
        except wave.Error:
            # REL-32a: stdlib wave rejects non-PCM (e.g. float32, format 3).
            # scipy.io.wavfile decodes them; whole-file in RAM is accepted for
            # these user-supplied inputs (this app records s16le only).
            self._stream_decoded_array()
        except Exception as e:
            print(f"Failed to open audio file for playback: {e}")
        finally:
            self.is_playing = False
            with state.sync_lock:
                state.is_playback_active = False
                state.currently_playing_show_id = None

    def _stream_wave(self, wav_file) -> None:
        """Stream one int-PCM WAV file to the s16le broadcast chain.

        Chunk math + rewind-on-EOF preserved from the pre-REL-32a loop.
        """
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sampwidth = wav_file.getsampwidth()
        frames_per_chunk = CHUNK_BYTES // (sampwidth * channels)

        while self.is_playing:
            try:
                data = self._next_wave_chunk(wav_file, frames_per_chunk)
                if data is None:
                    break
                # REL-32a: raw readframes bytes are NOT always s16le — a 24-bit
                # file piped raw is wrap-around garbage in the s16le chain.
                pcm = wav_chunk_to_s16le(data, sampwidth)
                self._broadcast_and_pace(pcm, frames_per_chunk, sample_rate)
            except Exception as e:
                print(f"Playback error: {e}")
                break

    def _next_wave_chunk(self, wav_file, frames_per_chunk: int) -> bytes | None:
        """readframes with rewind-on-EOF; None ends the stream."""
        data = wav_file.readframes(frames_per_chunk)
        if not data:
            # End of file — restart from beginning
            if self.is_playing:
                wav_file.rewind()  # Audit 4.2 Fix: use rewind instead of close/reopen
                data = wav_file.readframes(frames_per_chunk)
            else:
                return None
        return data

    def _broadcast_and_pace(self, pcm: bytes, frames_per_chunk: int, sample_rate: int) -> None:
        """Broadcast one converted chunk, then sleep out its realtime duration.

        Audit 4.3/5.4 Fix: skip local queue, broadcast directly to mixer;
        a broadcast failure must not kill playback.
        """
        try:
            state.broadcast_audio(pcm)
        except Exception:
            pass
        # Simulate realtime playback
        time.sleep(frames_per_chunk / sample_rate)

    def _stream_decoded_array(self) -> None:
        """Fallback streamer for non-PCM WAVs (float32 format 3) via scipy."""
        from scipy.io import wavfile as scipy_wavfile  # lazy: keeps module import light

        sample_rate, audio = scipy_wavfile.read(self.audio_file_path)
        self._stream_audio_array(np.asarray(audio), int(sample_rate))

    def _stream_audio_array(self, audio: np.ndarray, sample_rate: int) -> None:
        """Chunk + broadcast an in-memory decoded array, rewind on EOF."""
        if audio.ndim == 1:
            audio = audio[:, np.newaxis]
        frames_per_chunk = max(1, CHUNK_BYTES // (audio.itemsize * audio.shape[1]))
        index = 0
        while self.is_playing:
            chunk = audio[index : index + frames_per_chunk]
            if chunk.size == 0:
                if not self.is_playing:
                    break
                index = 0  # rewind-on-EOF (Audit 4.2 analogue)
                continue
            self._broadcast_and_pace(_float_block_to_s16le(chunk), frames_per_chunk, sample_rate)
            index += frames_per_chunk


# ReMixInterface removed (Audit 3.1)
