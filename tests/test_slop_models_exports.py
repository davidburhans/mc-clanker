# tests/test_slop_models_exports.py
"""Regression guard for the `slop_harness.models` `ALL_`-prefix export contract.

Background
----------
`slop_harness/quality_validator.py:23` imports these exact names at module
scope::

    from slop_harness.models import ALL_BPMS, ALL_INSTRUMENTS, ALL_KEYS

Historically `models.py` exported the names with the *wrong* suffix
(`BPMS_ALL` / `KEYS_ALL` / `BARS_ALL`) and had **no** instruments constant at
all, which made `quality_validator` unimportable and caused its 42-test suite
to be silently skipped.

This guard pins the exact import contract so a future suffix drift fails fast
and loudly instead of re-silencing the validator.

Example::

    $ .venv/bin/python -m pytest tests/test_slop_models_exports.py -q
"""
from collections.abc import Sequence

from slop_harness.models import (
    ALL_BARS,
    ALL_BPMS,
    ALL_INSTRUMENTS,
    ALL_KEYS,
)


def test_all_bpms_is_nonempty_sequence_of_int():
    assert isinstance(ALL_BPMS, Sequence)
    assert len(ALL_BPMS) > 0
    assert all(isinstance(v, int) for v in ALL_BPMS)


def test_all_keys_is_nonempty_sequence_of_str():
    assert isinstance(ALL_KEYS, Sequence)
    assert len(ALL_KEYS) > 0
    assert all(isinstance(v, str) for v in ALL_KEYS)


def test_all_bars_is_nonempty_sequence_of_int():
    assert isinstance(ALL_BARS, Sequence)
    assert len(ALL_BARS) > 0
    assert all(isinstance(v, int) for v in ALL_BARS)


def test_all_instruments_is_nonempty_sequence_of_str():
    assert isinstance(ALL_INSTRUMENTS, Sequence)
    assert len(ALL_INSTRUMENTS) > 0
    assert all(isinstance(v, str) for v in ALL_INSTRUMENTS)
