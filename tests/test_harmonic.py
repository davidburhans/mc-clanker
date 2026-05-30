import pytest
from app.lib.constants import VALID_KEYS
from app.lib.harmonic import HarmonicHelper


def test_valid_keys_mapping():
    """Verify that all 24 keys in VALID_KEYS map to unique Camelot codes."""
    all_codes = set()
    for key in VALID_KEYS:
        code = HarmonicHelper.get_camelot_code(key)
        assert code is not None
        assert isinstance(code, str)
        assert len(code) in (2, 3)
        assert code[-1] in ('A', 'B')
        assert 1 <= int(code[:-1]) <= 12
        all_codes.add(code)
    
    assert len(all_codes) == 24


@pytest.mark.parametrize(
    "key,expected_code",
    [
        ("C major", "8B"),
        ("C minor", "5A"),
        ("G# minor", "1A"),
        ("B major", "1B"),
        ("E major", "12B"),
        ("C# minor", "12A"),
    ]
)
def test_specific_camelot_codes(key, expected_code):
    """Verify specific known correct Camelot code mappings."""
    assert HarmonicHelper.get_camelot_code(key) == expected_code


def test_harmonic_neighbors_count_and_validity():
    """Verify get_harmonic_neighbors returns exactly 3 valid keys in VALID_KEYS."""
    for key in VALID_KEYS:
        neighbors = HarmonicHelper.get_harmonic_neighbors(key)
        assert len(neighbors) == 3
        for neighbor in neighbors:
            assert neighbor in VALID_KEYS
            assert neighbor != key


def test_harmonic_neighbors_wrapping_boundary():
    """Verify correct subdominant and dominant wrapping boundaries (1 <-> 12)."""
    # G# minor is 1A
    # Relative: 1B (B major)
    # Subdominant: 12A (C# minor)
    # Dominant: 2A (D# minor)
    gsharp_minor_neighbors = HarmonicHelper.get_harmonic_neighbors("G# minor")
    assert "B major" in gsharp_minor_neighbors
    assert "C# minor" in gsharp_minor_neighbors
    assert "D# minor" in gsharp_minor_neighbors

    # C# minor is 12A
    # Relative: 12B (E major)
    # Subdominant: 11A (F# minor)
    # Dominant: 1A (G# minor)
    csharp_minor_neighbors = HarmonicHelper.get_harmonic_neighbors("C# minor")
    assert "E major" in csharp_minor_neighbors
    assert "F# minor" in csharp_minor_neighbors
    assert "G# minor" in csharp_minor_neighbors


def test_invalid_key_raises_error():
    """Verify that passing an invalid key string raises a ValueError."""
    with pytest.raises(ValueError):
        HarmonicHelper.get_camelot_code("H major")
    
    with pytest.raises(ValueError):
        HarmonicHelper.get_camelot_code("C flat")

    with pytest.raises(ValueError):
        HarmonicHelper.get_harmonic_neighbors("Unknown key")


def test_get_harmonic_map():
    """Verify that get_harmonic_map generates a full dictionary for all 24 keys."""
    h_map = HarmonicHelper.get_harmonic_map()
    assert len(h_map) == 24
    for key, data in h_map.items():
        assert key in VALID_KEYS
        assert "camelot" in data
        assert "neighbors" in data
        assert len(data["neighbors"]) == 3
