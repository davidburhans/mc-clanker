"""Round-3 lane fix_a_mixer — regression tests for A1/A2/A3.

A1 (framework_mixer.Mixer._callback): the playhead increment must happen while
   ``self.lock`` is held, otherwise a ``clear()``/``prime_loop()`` landing in the
   release→increment window is clobbered by the stale ``+= frames``.
A2 (framework_mixer.Mixer._extend_tracks_for_loop + domain_audio.tile_to_loop):
   a track straddling the loop boundary used to keep its tail AND gain an offset
   tiled copy of its head after the boundary (same stem twice → +6 dB phasing);
   cached audio longer than the loop must be truncated to one loop.
A3 (framework_mixer.Mixer._callback): the loop>1 transition path must coerce
   channel shape like ``prime_loop`` does, else a (N, 1) stem plays left-only.
"""

import threading

import numpy as np
import pytest

from app.framework.domain_audio import make_cache_key, tile_to_loop
from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state

SAMPLE_RATE = 44100


@pytest.fixture(autouse=True)
def reset_state():
    state.reset()
    state.is_generating = True
    yield
    state.is_generating = False


class ReleaseHookLock:
    """threading.Lock stand-in that fires a one-shot callback right AFTER release.

    Deterministic stand-in for "another thread grabbed the lock in this exact
    window": the hook runs in-line, so a fix that keeps the playhead increment
    inside the critical section is provably safe and the unfixed ordering is
    provably broken — no thread-sleep flakiness.
    """

    def __init__(self):
        self._inner = threading.Lock()
        self.on_release = None
        self.releases = 0

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        result = self._inner.__exit__(exc_type, exc, tb)
        self.releases += 1
        hook, self.on_release = self.on_release, None
        if hook is not None:
            hook()
        return result


def _covering_tracks(mixer, sample):
    return [t for t in mixer.tracks if t.start_sample <= sample < (t.start_sample + t.length)]


# ===========================================================================
# A1 — playhead increment must be inside self.lock
# ===========================================================================


def test_generating_callback_reset_after_lock_release_is_not_clobbered():
    """clear() landing right after the callback drops the lock must survive (A1).

    Unfixed: the trailing ``self.current_sample += frames`` runs outside the lock,
    so the reset is resurrected as ``frames`` (2048 for the default blocksize).
    """
    mixer = Mixer(channels=1)
    mixer.add_track(np.ones((10_000, 1), dtype=np.float32) * 0.5, 0, stem_index=0)
    hook = ReleaseHookLock()
    mixer.lock = hook
    hook.on_release = mixer.clear

    mixer._callback(np.zeros((2048, 1), dtype=np.float32), 2048, None, None)

    assert hook.releases >= 1, "release hook never fired — lock is not a ReleaseHookLock?"
    assert mixer.tracks == []
    assert mixer.current_sample == 0, "clear() was clobbered by a stale out-of-lock increment"


def test_idle_callback_reset_after_lock_release_is_not_clobbered():
    """Same invariant on the not-generating early-return path (framework_mixer:214)."""
    mixer = Mixer(channels=1)
    state.is_generating = False
    hook = ReleaseHookLock()
    mixer.lock = hook
    hook.on_release = mixer.clear
    mixer.current_sample = 5000

    mixer._callback(np.zeros((64, 1), dtype=np.float32), 64, None, None)

    # >= 1: the idle path takes the lock around the update, clear() takes it again.
    assert hook.releases >= 1, "idle path never took self.lock around the playhead update"
    assert mixer.current_sample == 0, "clear() was clobbered by a stale out-of-lock increment"


def test_callback_advances_playhead_exactly_once_per_tick():
    """Refactor guard: the increment moved into the lock, it did not duplicate/vanish."""
    mixer = Mixer(channels=1)
    mixer.add_track(np.ones((100, 1), dtype=np.float32), 0, stem_index=0)
    for expected in (100, 200, 300):
        mixer._callback(np.zeros((100, 1), dtype=np.float32), 100, None, None)
        assert mixer.current_sample == expected

    state.is_generating = False
    mixer._callback(np.zeros((100, 1), dtype=np.float32), 100, None, None)
    assert mixer.current_sample == 400


# ===========================================================================
# A2 — straddling track must not double up past the loop boundary
# ===========================================================================


def test_extend_for_loop_cuts_straddling_track_at_boundary():
    mixer = Mixer(channels=1)
    audio = np.ones((100, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 50

    mixer._extend_tracks_for_loop(80)

    original = mixer.tracks[0]
    assert original.start_sample + original.length == 80, "original tail still crosses the loop boundary"
    tiled = [t for t in mixer.tracks if t.start_sample == 80]
    assert len(tiled) == 1
    assert tiled[0].length == 40


def test_extend_for_loop_straddle_leaves_single_copy_after_boundary():
    """Total energy per sample past the boundary must stay 1x (A2 +6 dB phasing)."""
    mixer = Mixer(channels=1)
    mixer.add_track(np.ones((100, 1), dtype=np.float32) * 0.5, 0, stem_index=0)
    mixer.current_sample = 50

    mixer._extend_tracks_for_loop(80)

    for sample in (80, 85, 119):
        covering = _covering_tracks(mixer, sample)
        assert len(covering) == 1, f"sample {sample} carries {len(covering)} copies of stem 0"
    # Before the boundary nothing changed.
    assert len(_covering_tracks(mixer, 60)) == 1


def test_extend_for_loop_does_not_mutate_caller_array():
    """The boundary cut is a slice: the stem buffer handed to the Mixer stays intact."""
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer = Mixer(channels=1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 50

    mixer._extend_tracks_for_loop(80)

    assert audio.shape == (100, 1)
    assert np.array_equal(audio[:, 0], np.arange(100, dtype=np.float32))
    assert np.array_equal(mixer.tracks[0].audio_data[:, 0], np.arange(80, dtype=np.float32))


# --- tile_to_loop: never hand the mixer more than one loop of audio ---------

LONG_CACHE_BPM = 960  # 8 bars @ 960 BPM → 2.0 s → 88200 samples @ 44.1 kHz


def _tile_cached(cached: np.ndarray, *, bars: int = 8):
    stem = {"prompt": "Synth", "bars": bars, "model_id": "foundation-1"}
    cache_key = make_cache_key("foundation-1", "Synth", LONG_CACHE_BPM, "A minor", bars)
    prepared, loop_samples = tile_to_loop(
        next_stems=[stem],
        stem_cache={cache_key: {"audio_data": cached, "last_used": 0}},
        bpm=LONG_CACHE_BPM,
        key="A minor",
        sample_rate=SAMPLE_RATE,
        deduped_tracks=[stem],
    )
    return prepared, loop_samples


def test_tile_to_loop_truncates_audio_longer_than_the_loop():
    prepared, loop_samples = _tile_cached(np.ones((132300, 2), dtype=np.float32))

    assert loop_samples == 88200
    assert len(prepared[0][0]) == loop_samples, "tiled track exceeds loop_duration_samples"


def test_tile_to_loop_still_tiles_short_audio_to_exact_length():
    prepared, loop_samples = _tile_cached(np.ones((1000, 2), dtype=np.float32))

    assert len(prepared[0][0]) == loop_samples
    assert np.all(prepared[0][0] == 1.0)


# ===========================================================================
# A3 — loop>1 transition path must normalize channel shape
# ===========================================================================


def test_loop_transition_promotes_single_channel_track_to_stereo():
    mixer = Mixer(channels=2, sample_rate=SAMPLE_RATE)
    mixer.current_sample = 0
    mixer.current_loop_end_sample = 50
    mixer.set_next_loop([(np.full((500, 1), 0.25, dtype=np.float32), 0)], next_loop_duration_samples=500, loop_idx=2)

    outdata = np.zeros((200, 2), dtype=np.float32)
    mixer._callback(outdata, 200, None, None)

    added = [t for t in mixer.tracks if t.start_sample == 50]
    assert len(added) == 1
    assert added[0].audio_data.shape == (500, 2), "transition path left a (N,1) track in the mixer"


def test_loop_transition_plays_in_both_channels():
    """Unfixed: mix_channels = min(2, 1) → the right channel stays silent."""
    mixer = Mixer(channels=2, sample_rate=SAMPLE_RATE)
    mixer.current_sample = 0
    mixer.current_loop_end_sample = 50
    mixer.set_next_loop([(np.full((500, 1), 0.25, dtype=np.float32), 0)], next_loop_duration_samples=500, loop_idx=2)

    outdata = np.zeros((200, 2), dtype=np.float32)
    mixer._callback(outdata, 200, None, None)

    assert np.allclose(outdata[50:, 0], 0.25)
    assert np.allclose(outdata[50:, 1], 0.25), "new loop audible in the left channel only"


def test_loop_transition_keeps_true_stereo_untouched():
    stereo = np.array([[0.1, 0.2]] * 100, dtype=np.float32)
    mixer = Mixer(channels=2, sample_rate=SAMPLE_RATE)
    mixer.current_sample = 0
    mixer.current_loop_end_sample = 50
    mixer.set_next_loop([(stereo, 0)], next_loop_duration_samples=500, loop_idx=2)

    mixer._callback(np.zeros((200, 2), dtype=np.float32), 200, None, None)

    added = [t for t in mixer.tracks if t.start_sample == 50]
    assert added[0].audio_data.shape == (100, 2)
    assert np.allclose(added[0].audio_data[:5], [[0.1, 0.2]] * 5)
