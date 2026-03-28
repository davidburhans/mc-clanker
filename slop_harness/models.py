# slop_harness/models.py
"""
Model definitions for slop jockey fine-tuning dataset generation.

Contains definitions for Foundation-1, Infinite Pianos, and Vocal Textures models
with their respective parameters: major_families, sub_families, timbre_tags,
notation_tags, fx_tags, keys, bpms, and bars.
"""

# All 24 musical keys (all sharp notation)
KEYS_ALL = [
    "A# major", "A# minor", "B major", "B minor",
    "C major", "C minor", "C# major", "C# minor",
    "D major", "D minor", "D# major", "D# minor",
    "E major", "E minor",
    "F major", "F minor", "F# major", "F# minor",
    "G major", "G minor", "G# major", "G# minor",
    "A major", "A minor",
]

# Standard BPM values
BPMS_ALL = [100, 110, 120, 128, 130, 140, 150]

# Standard bar lengths
BARS_ALL = [4, 8]

# Foundation-1 model definition
FOUNDATION_1_MODEL = {
    "id": "foundation-1",
    "repo_id": "RoyalCities/Foundation-1",
    "description": "General purpose electronic sounds. Excellent for driving drums, thick bass, and sharp leads.",
    "major_families": [
        "Synth", "Keys", "Bass", "Bowed Strings", "Mallet", "Wind",
        "Guitar", "Brass", "Vocal", "Plucked Strings"
    ],
    "sub_families": [
        "Synth Lead", "Synth Bass", "FM Synth", "Wavetable Synth", "Analog Synth", "Supersaw",
        "Digital Organ", "Hammond Organ", "Church Organ",
        "Grand Piano", "Digital Piano", "Rhodes Piano", "Wurlitzer Piano", "CP Piano", "Clavinet", "Celesta", "Harpsichord",
        "Pad", "Atmosphere", "Texture", "Bell", "Church Bell", "Tubular Bells",
        "Marimba", "Vibraphone", "Glockenspiel", "Xylophone", "Steel Drums", "Kalimba",
        "Ocarina", "Pluck", "Music Box", "Tack Piano",
        "Violin", "Viola", "Cello", "Digital Strings", "Harp", "Celtic Harp", "Concert Harp",
        "Koto", "Sitar", "Fiddle",
        "Acoustic Guitar", "Nylon Guitar", "Electric Guitar",
        "Trumpet", "French Horn", "Flugelhorn", "Bass Trombone", "Tenor Trombone", "Tuba",
        "Flute", "Piccolo", "Clarinet", "Oboe", "Bassoon", "Irish Flute", "World Winds", "Saxophone",
        "Choir", "Synthetic Choir", "Synthetic Vox",
        "Sub Bass", "Reese Bass", "Analog Bass", "Wavetable Bass", "Picked Bass", "Digital Bass", "FM Bass",
        "Pan Flute",
    ],
    "timbre_tags": [
        # Frequency/Brightness
        "Upper Mids", "Mids", "Highs", "Low Mids",
        "Bright", "Dark", "Shiny", "Sparkly",
        "Warm", "Cold", "Silky", "Glass", "Glassy",
        # Texture/Character
        "Clean", "Gritty", "Retro", "Analog",
        "Crisp", "Focused", "Metallic", "Chiptune",
        "Woody", "Rubbery", "Buzzy", "Vintage",
        # Spatial/Dynamic
        "Wide", "Thick", "Thin", "Full",
        "Near", "Far", "Rich", "Tight",
        "Punchy", "Plucked", "Snappy", "Staccato",
        # Modulation/Effects
        "Saw", "Square", "Triangle",
        "Pitch Bend", "Filter", "Bitcrush", "Bitcrushed",
        "Growl", "Biting", "Harsh", "Overdriven",
        "Acid", "Reese", "Siren",
        # Environmental
        "Spacey", "Ambient", "Muffled", "Veiled",
        "Boomy", "Deep", "Rumble",
        "White Noise", "Laser", "FX",
        # Formant/Vocal
        "Formant Vocal", "Synthetic Vox",
        # Other
        "Round", "Nasal", "Noisy", "Dreamy", "Supersaw",
        "Airy", "Breathy", "Present",
        "303",
    ],
    "notation_tags": [
        "chord progression", "melody", "top melody", "arp", "triplets",
        "simple", "complex", "rising", "falling", "strummed", "sustained",
        "catchy", "epic", "slow", "fast"
    ],
    "fx_tags": [
        "Low Reverb", "Medium Reverb", "High Reverb", "Plate Reverb",
        "Low Delay", "Medium Delay", "High Delay", "Ping Pong Delay",
        "Stereo Delay", "Cross Delay", "Mono Delay",
        "Low Distortion", "Medium Distortion", "High Distortion",
        "Phaser", "Low Phaser", "Medium Phaser", "High Phaser",
        "Bitcrush", "High Bitcrush"
    ],
    "keys": KEYS_ALL,
    "bpms": BPMS_ALL,
    "bars": BARS_ALL,
}

# Infinite Pianos model definition
INFINITE_PIANOS_MODEL = {
    "id": "infinite-pianos",
    "repo_id": "RoyalCities/RC_Infinite_Pianos",
    "description": "Specialized piano model. BEST at: Chord Progressions with Melodies. OK at: Chord Progressions only. AVOID: Melody Only.",
    "major_families": ["Keys", "Piano", "Mallet"],
    "sub_families": [
        "Grand Piano", "Soft E. Piano", "Medium E. Piano"
    ],
    "chord_progression_modifiers": [
        "simple", "complex", "dance plucky", "fast", "jazzy", "low",
        "simple strummed", "rising strummed", "complex strummed", "jazzy strummed", "slow strummed",
        "plucky dance", "rising", "falling", "slow", "slow jazzy", "fast jazzy",
        "smooth", "strummed", "plucky"
    ],
    "melody_types": [
        "catchy melody", "complex melody", "complex top melody", "catchy top melody",
        "top melody", "smooth melody", "complex catchy melody", "jazzy melody", "smooth catchy melody",
        "plucky dance melody", "dance melody", "alternating low melody", "alternating top arp melody",
        "alternating top melody", "alternating catchy melody", "top arp melody",
        "slow top melody", "fast top melody", "fast catchy top melody", "slow catchy top melody",
        "alternating melody", "falling arp melody", "rising arp melody", "top catchy melody"
    ],
    "fx_tags": [
        "No Tremolo", "Low Tremolo", "Medium Tremolo", "High Tremolo",
        "No Reverb", "Low Reverb", "Medium Reverb", "High Reverb", "High Spacey Reverb"
    ],
    "keys": [
        "A# major", "A# minor", "C major", "C# major", "C# minor",
        "D major", "D# major", "D# minor", "F major", "F# major", "F# minor",
        "G major", "G# major", "G# minor"
    ],
    "bpms": BPMS_ALL,
    "bars": BARS_ALL,
}

# Vocal Textures model definition
VOCAL_TEXTURES_MODEL = {
    "id": "vocal-textures",
    "repo_id": "RoyalCities/Vocal_Textures_Main",
    "description": "Specialized vocal textures. BEST at: Chord Progressions. AVOID: Melodies.",
    "major_families": ["Vocal", "Choir", "Pad", "Atmosphere"],
    "sub_families": ["Male Vocal Texture", "Female Vocal Texture", "Ensemble Vocal Texture"],
    "notation_tags": ["chord progression"],
    "keys": KEYS_ALL,
    "bpms": BPMS_ALL,
    "bars": BARS_ALL,
}

# All models list
ALL_MODELS = [FOUNDATION_1_MODEL, INFINITE_PIANOS_MODEL, VOCAL_TEXTURES_MODEL]

# Exported constants for convenience
TIMBRE_TAGS_F1 = FOUNDATION_1_MODEL["timbre_tags"]
FX_TAGS_F1 = FOUNDATION_1_MODEL["fx_tags"]
NOTATION_TAGS_F1 = FOUNDATION_1_MODEL["notation_tags"]
