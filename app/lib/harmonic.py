from typing import Any
from app.lib.constants import VALID_KEYS


class HarmonicHelper:
    """
    Helper class for standard Circle of Fifths / Camelot Wheel harmonic mixing.
    
    Provides bi-directional mappings between the 24 VALID_KEYS and Camelot positions,
    and calculates harmonically compatible transition keys.
    """

    # 1A to 12A (Minor) and 1B to 12B (Major)
    KEY_TO_CAMELOT: dict[str, str] = {
        "G# minor": "1A", "B major": "1B",
        "D# minor": "2A", "F# major": "2B",
        "A# minor": "3A", "C# major": "3B",
        "F minor": "4A",  "G# major": "4B",
        "C minor": "5A",  "D# major": "5B",
        "G minor": "6A",  "A# major": "6B",
        "D minor": "7A",  "F major": "7B",
        "A minor": "8A",  "C major": "8B",
        "E minor": "9A",  "G major": "9B",
        "B minor": "10A", "D major": "10B",
        "F# minor": "11A", "A major": "11B",
        "C# minor": "12A", "E major": "12B",
    }

    CAMELOT_TO_KEY: dict[str, str] = {v: k for k, v in KEY_TO_CAMELOT.items()}

    @classmethod
    def get_camelot_code(cls, key: str) -> str:
        """
        Get the Camelot Wheel code (e.g. '5A') for a given key string.
        
        Raises ValueError if key is not valid.
        """
        if key not in cls.KEY_TO_CAMELOT:
            raise ValueError(f"Invalid key '{key}'. Must be one of: {VALID_KEYS}")
        return cls.KEY_TO_CAMELOT[key]

    @classmethod
    def get_harmonic_neighbors(cls, key: str) -> list[str]:
        """
        Get the 3 harmonically compatible transition keys for the given key:
        1. Relative major/minor (same Camelot number, opposite letter)
        2. Subdominant fourth (number - 1, same letter)
        3. Dominant fifth (number + 1, same letter)
        
        Raises ValueError if key is not valid.
        """
        code = cls.get_camelot_code(key)
        num = int(code[:-1])
        letter = code[-1]

        # 1. Relative key (opposite letter)
        other_letter = "B" if letter == "A" else "A"
        relative_code = f"{num}{other_letter}"

        # 2. Subdominant (N-1) with wrapping
        subdominant_num = (num - 2) % 12 + 1
        subdominant_code = f"{subdominant_num}{letter}"

        # 3. Dominant (N+1) with wrapping
        dominant_num = num % 12 + 1
        dominant_code = f"{dominant_num}{letter}"

        neighbor_codes = [relative_code, subdominant_code, dominant_code]
        
        # Convert codes back to keys
        return [cls.CAMELOT_TO_KEY[c] for c in neighbor_codes]

    @classmethod
    def get_harmonic_map(cls) -> dict[str, dict[str, Any]]:
        """
        Generate a complete dictionary of all keys mapped to their Camelot codes and neighbors.
        Excellent for pre-caching on the frontend.
        """
        return {
            key: {
                "camelot": cls.get_camelot_code(key),
                "neighbors": cls.get_harmonic_neighbors(key)
            }
            for key in VALID_KEYS
        }
