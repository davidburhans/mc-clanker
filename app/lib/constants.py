"""Shared schema constants — single source of truth for mc-clanker LLM schema enums.

This module provides:
- Static enums: VALID_BPMS, VALID_KEYS, VALID_BARS, VALID_MODEL_IDS, etc.
- Dynamic family extension: add_custom_major_family() + get_all_major_families()
- Unified RESPONSE_FORMAT schema builder: get_response_format_schema()

Used by:
- LLM response_format in framework_conductor_async.py and slop_harness/llm_client.py
- API Pydantic validators in routes/schemas.py
- Frontend alignment tests in tests/test_frontend_constants.py
- Training pipeline validation in training/dpo_pipeline.py
"""

import threading

# =============================================================================
# Static Enums
# =============================================================================

VALID_BPMS: list[int] = [100, 110, 120, 128, 130, 140, 150]

VALID_KEYS: list[str] = [
    "C major",
    "C minor",
    "C# major",
    "C# minor",
    "D major",
    "D minor",
    "D# major",
    "D# minor",
    "E major",
    "E minor",
    "F major",
    "F minor",
    "F# major",
    "F# minor",
    "G major",
    "G minor",
    "G# major",
    "G# minor",
    "A major",
    "A minor",
    "A# major",
    "A# minor",
    "B major",
    "B minor",
]

VALID_BARS: list[int] = [4, 8]

VALID_ACTION_TYPES: list[str] = ["retain", "add", "remove"]

VALID_MODEL_IDS: list[str] = ["foundation-1", "infinite-pianos", "vocal-textures"]

# All major families across all models (union)
VALID_MAJOR_FAMILIES: list[str] = [
    "Drums",
    "Percussion",
    "Synth",
    "Keys",
    "Bass",
    "Bowed Strings",
    "Mallet",
    "Wind",
    "Guitar",
    "Brass",
    "Plucked Strings",
    "Piano",
    "Vocal",
    "Choir",
    "Pad",
    "Atmosphere",
]

# All sub_families across all models (~60 values)
VALID_SUB_FAMILIES: list[str] = [
    # Foundation-1
    "Synth Lead",
    "Synth Bass",
    "FM Synth",
    "Wavetable Synth",
    "Analog Synth",
    "Supersaw",
    "Digital Organ",
    "Hammond Organ",
    "Church Organ",
    "Grand Piano",
    "Digital Piano",
    "Rhodes Piano",
    "Wurlitzer Piano",
    "CP Piano",
    "Clavinet",
    "Celesta",
    "Harpsichord",
    "Pad",
    "Atmosphere",
    "Texture",
    "Bell",
    "Church Bell",
    "Tubular Bells",
    "Marimba",
    "Vibraphone",
    "Glockenspiel",
    "Xylophone",
    "Steel Drums",
    "Kalimba",
    "Ocarina",
    "Pluck",
    "Music Box",
    "Tack Piano",
    "Violin",
    "Viola",
    "Cello",
    "Digital Strings",
    "Harp",
    "Celtic Harp",
    "Concert Harp",
    "Koto",
    "Sitar",
    "Fiddle",
    "Acoustic Guitar",
    "Nylon Guitar",
    "Electric Guitar",
    "Trumpet",
    "French Horn",
    "Flugelhorn",
    "Bass Trombone",
    "Tenor Trombone",
    "Tuba",
    "Flute",
    "Piccolo",
    "Clarinet",
    "Oboe",
    "Bassoon",
    "Irish Flute",
    "World Winds",
    "Saxophone",
    "Choir",
    "Synthetic Choir",
    "Synthetic Vox",
    "Sub Bass",
    "Reese Bass",
    "Analog Bass",
    "Wavetable Bass",
    "Picked Bass",
    "Digital Bass",
    "FM Bass",
    "Pan Flute",
    # Infinite Pianos
    "Soft E. Piano",
    "Medium E. Piano",
    # Vocal Textures
    "Male Vocal Texture",
    "Female Vocal Texture",
    "Ensemble Vocal Texture",
]

# ~79 timbre tags from Foundation-1
TIMBRE_TAGS: list[str] = [
    "Upper Mids",
    "Mids",
    "Highs",
    "Low Mids",
    "Bright",
    "Dark",
    "Shiny",
    "Sparkly",
    "Warm",
    "Cold",
    "Silky",
    "Glass",
    "Glassy",
    "Clean",
    "Gritty",
    "Retro",
    "Analog",
    "Crisp",
    "Focused",
    "Metallic",
    "Chiptune",
    "Woody",
    "Rubbery",
    "Buzzy",
    "Vintage",
    "Wide",
    "Thick",
    "Thin",
    "Full",
    "Near",
    "Far",
    "Rich",
    "Tight",
    "Punchy",
    "Plucked",
    "Snappy",
    "Staccato",
    "Saw",
    "Square",
    "Triangle",
    "Pitch Bend",
    "Filter",
    "Bitcrush",
    "Bitcrushed",
    "Growl",
    "Biting",
    "Harsh",
    "Overdriven",
    "Acid",
    "Reese",
    "Siren",
    "Spacey",
    "Ambient",
    "Muffled",
    "Veiled",
    "Boomy",
    "Deep",
    "Rumble",
    "White Noise",
    "Laser",
    "FX",
    "Formant Vocal",
    "Synthetic Vox",
    "Round",
    "Nasal",
    "Noisy",
    "Dreamy",
    "Supersaw",
    "Airy",
    "Breathy",
    "Present",
    "303",
]

# 15 notation tags
NOTATION_TAGS: list[str] = [
    "chord progression",
    "melody",
    "top melody",
    "arp",
    "triplets",
    "simple",
    "complex",
    "rising",
    "falling",
    "strummed",
    "sustained",
    "catchy",
    "epic",
    "slow",
    "fast",
]

# 20 FX tags from Foundation-1
FX_TAGS: list[str] = [
    "Low Reverb",
    "Medium Reverb",
    "High Reverb",
    "Plate Reverb",
    "Low Delay",
    "Medium Delay",
    "High Delay",
    "Ping Pong Delay",
    "Stereo Delay",
    "Cross Delay",
    "Mono Delay",
    "Low Distortion",
    "Medium Distortion",
    "High Distortion",
    "Phaser",
    "Low Phaser",
    "Medium Phaser",
    "High Phaser",
    "Bitcrush",
    "High Bitcrush",
]


# =============================================================================
# Dynamic Major Family Extension
# =============================================================================

_custom_major_families: set = set()
_custom_major_families_lock = threading.Lock()


def get_all_major_families() -> list[str]:
    """Union of built-in VALID_MAJOR_FAMILIES + user-added custom families."""
    with _custom_major_families_lock:
        return list(VALID_MAJOR_FAMILIES) + sorted(_custom_major_families)


def add_custom_major_family(family: str) -> None:
    """Register a user-added family so it becomes valid in the LLM schema."""
    with _custom_major_families_lock:
        _custom_major_families.add(family)


def get_response_format_schema() -> dict:
    """Build the full RESPONSE_FORMAT schema dict with current (static + dynamic) families.

    Called at LLM call time so dynamic families are always current.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "dj_action_state",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "reasoning": {"type": "string"},
                    "master_bpm": {"type": "integer", "enum": VALID_BPMS},
                    "master_key": {"type": "string", "enum": VALID_KEYS},
                    "actions": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "action_type": {"type": "string", "const": "retain"},
                                        "stem_index": {"type": "integer"},
                                    },
                                    "required": ["action_type", "stem_index"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "action_type": {"type": "string", "const": "add"},
                                        "model_id": {"type": "string", "enum": VALID_MODEL_IDS},
                                        "major_family": {"type": "string", "enum": get_all_major_families()},
                                        "sub_family": {"type": "string", "enum": VALID_SUB_FAMILIES},
                                        "timbre_tags": {
                                            "type": "array",
                                            "items": {"type": "string", "enum": TIMBRE_TAGS},
                                            "maxItems": 3,
                                        },
                                        "notation_tag": {"type": "string", "enum": NOTATION_TAGS},
                                        "fx_tag": {"type": "string", "enum": FX_TAGS},
                                        "bars": {"type": "integer", "enum": VALID_BARS},
                                    },
                                    "required": [
                                        "action_type",
                                        "model_id",
                                        "major_family",
                                        "sub_family",
                                        "timbre_tags",
                                        "notation_tag",
                                        "fx_tag",
                                        "bars",
                                    ],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "action_type": {"type": "string", "const": "remove"},
                                        "stem_index": {"type": "integer"},
                                    },
                                    "required": ["action_type", "stem_index"],
                                    "additionalProperties": False,
                                },
                            ]
                        },
                    },
                    "name": {"type": "string"},
                },
                "required": ["reasoning", "master_bpm", "master_key", "actions", "name"],
                "additionalProperties": False,
            },
        },
    }
