"""TDD-red suite for the P11-U2 MixerController public surface promotion.

Encodes the DoD for Phase 11 Unit 2 *before* the implementation exists, so these
tests FAIL (red) until the following land additively on ``Mixer`` /
``MixerController``:

- ``Mixer.prime_loop(tracks, *, duration_samples)`` -- byte-for-byte delegation
  of the P10 loop-1 batch (``loop_steps._step_commit_to_mixer``).
- ``Mixer.loop_position_seconds()`` -- byte-for-byte delegation of the P13
  ``current_ahead`` read (``loop_steps._step_await_pregen``).
- ``Mixer.ensure_stereo`` -- public alias of the existing ``_ensure_stereo``
  ``@staticmethod`` (NOT ``domain_audio.to_two_channel``).
- ``ports.MixerController`` -- extended with ``prime_loop`` + ``loop_position_seconds``.

Import discipline: this file deliberately avoids the ``framework_main_async`` /
``loop_steps`` import chain (it transitively pulls ``scipy`` via ``aac_encoder``);
all assertions are pure mixer-level.

Example::

    m = Mixer()
    m.prime_loop([(np.zeros((10, 2), np.float32), 0)], duration_samples=100)
    assert m.loop_position_seconds() >= 0.0
"""

import numpy as np
import pytest

from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state
from app.framework.ports import MixerController

# ---------------------------------------------------------------------------
# Fixture -- mirror the reset_state shape in test_mixer.py /
# test_mixer_extended.py so per-stem mixer reads are well-defined.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_state():
    state.reset()
    state.is_generating = True
    state.active_stems = [{"prompt": "stem0"}, {"prompt": "stem1"}]
    yield
    state.is_generating = False


# The stereo-coercion matrix shared by prime_loop and ensure_stereo. The
# (N,1) -> (N,2) case is the load-bearing divergence between _ensure_stereo
# (expands) and to_two_channel (passes through); it MUST stay pinned so the
# surface never silently swaps to to_two_channel.
STEREO_CASES = [
    (np.array([0.1, 0.2, 0.3], dtype=np.float32), (3, 2)),
    (np.array([[0.5], [0.6]], dtype=np.float32), (2, 2)),
    (np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32), (2, 2)),
    (np.zeros((10, 4), dtype=np.float32), (10, 4)),
]
STEREO_IDS = ["mono_1d", "mono_2d_single_channel", "already_stereo", "multichannel"]


def _drive_p10_loop1_batch(mixer, tracks, duration_samples):
    """Drive the verbatim P10 loop-1 batch from ``loop_steps.py`` directly.

    Reference implementation of ``_step_commit_to_mixer`` (the
    ``self._loop_idx == 1`` branch): read ``current_sample`` INSIDE
    ``mixer.lock``, add each track (mono-coerced via ``_ensure_stereo``) at that
    live position, then set the boundary and current-loop duration. ``prime_loop``
    must reproduce this byte-for-byte.
    """
    with mixer.lock:
        start_sample = mixer.current_sample
        for audio_data, stem_idx in tracks:
            mixer._add_track_internal(mixer._ensure_stereo(audio_data), start_sample, stem_idx)
        mixer.current_loop_end_sample = start_sample + duration_samples
        mixer._current_loop_duration = duration_samples


def _assert_track_bundles_identical(public_mixer, private_mixer):
    """Assert two mixers' track lists are identical element-wise.

    Compares audio_data, start_sample, and stem_index so the byte-for-byte
    delegation guarantee is fully covered.
    """
    assert len(public_mixer.tracks) == len(private_mixer.tracks)
    for public_track, private_track in zip(public_mixer.tracks, private_mixer.tracks):
        assert np.array_equal(public_track.audio_data, private_track.audio_data)
        assert public_track.start_sample == private_track.start_sample
        assert public_track.stem_index == private_track.stem_index


# ===========================================================================
# prime_loop -- P10 loop-1 batch delegation
# ===========================================================================


@pytest.mark.parametrize("audio,expected_shape", STEREO_CASES, ids=STEREO_IDS)
def test_prime_loop_ensures_stereo_per_track(audio, expected_shape):
    """prime_loop coerces each track to stereo exactly like _ensure_stereo."""
    m = Mixer()
    m.prime_loop([(audio, 0)], duration_samples=100)
    assert m.tracks[0].audio_data.shape == expected_shape


def test_prime_loop_matches_private_path_output():
    """prime_loop output == driving the verbatim P10 batch directly.

    The boundary is derived from the LIVE ``current_sample`` (here 7777), never a
    hardcoded 0, and ``_current_loop_duration`` must be set.
    """
    duration = 44100
    tracks = [
        (np.array([[0.5], [0.6]], dtype=np.float32), 0),
        (np.ones((4, 2), dtype=np.float32), 1),
    ]

    private = Mixer()
    private.current_sample = 7777  # LIVE position, NOT 0
    _drive_p10_loop1_batch(private, tracks, duration)

    public = Mixer()
    public.current_sample = 7777
    public.prime_loop(tracks, duration_samples=duration)

    _assert_track_bundles_identical(public, private)
    # boundary computed from LIVE current_sample (7777), not 0
    assert public.tracks[0].start_sample == 7777
    assert public.current_loop_end_sample == 7777 + duration
    assert public._current_loop_duration == duration


# ===========================================================================
# loop_position_seconds -- P13 boundary read delegation
# ===========================================================================


def test_loop_position_seconds_matches_p13_expression():
    """loop_position_seconds == verbatim P13 (live_end - live_pos) / sample_rate."""
    m = Mixer()
    m.current_sample = 44100
    m.current_loop_end_sample = 88200  # 1.0s of headroom at 44100 Hz
    assert m.loop_position_seconds() == pytest.approx(1.0)
    assert m.loop_position_seconds() == (88200 - 44100) / m.sample_rate


def test_loop_position_seconds_negative_when_behind():
    """Negative result when the playback head is past the loop boundary."""
    m = Mixer()
    m.current_sample = 88200
    m.current_loop_end_sample = 44100  # 1.0s behind schedule
    assert m.loop_position_seconds() == pytest.approx(-1.0)


# ===========================================================================
# ensure_stereo alias -- same staticmethod object as _ensure_stereo
# ===========================================================================


def test_ensure_stereo_alias_is_same_object():
    """The public alias must BE _ensure_stereo (same object), not a reimpl."""
    assert Mixer.ensure_stereo is Mixer._ensure_stereo


@pytest.mark.parametrize("audio,expected_shape", STEREO_CASES, ids=STEREO_IDS)
def test_ensure_stereo_alias_shapes(audio, expected_shape):
    """Alias shapes match _ensure_stereo, incl. the (N,1) -> (N,2) expansion."""
    assert Mixer.ensure_stereo(audio).shape == expected_shape


# ===========================================================================
# Protocol conformance -- Mixer satisfies the EXTENDED MixerController
# ===========================================================================


def test_mixer_satisfies_extended_mixer_controller_protocol():
    """Mixer carries the new surface and still satisfies MixerController.

    ``runtime_checkable`` only inspects method presence, so this guard catches a
    regression where the Protocol is extended with ``prime_loop`` /
    ``loop_position_seconds`` but the concrete Mixer is not (isinstance would
    flip False), and it pins the two new members onto the instance.
    """
    m = Mixer()
    assert hasattr(m, "prime_loop")
    assert hasattr(m, "loop_position_seconds")
    assert isinstance(m, MixerController)
