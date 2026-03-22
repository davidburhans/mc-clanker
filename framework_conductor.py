import os
import json
from openai import OpenAI

class Conductor:
    def __init__(self, api_base="http://192.168.0.203:1234/v1", model_name="local-model"):
        self.api_base = api_base
        self.model_name = model_name
        self.api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
        
        # Initialize the OpenAI client pointing to the local LM Studio server
        self.client = OpenAI(base_url=self.api_base, api_key=self.api_key)
        
        self.system_instruction = """
You are an expert AI music composer, producer, and DJ. Your task is to orchestrate a continuous, evolving soundtrack utilizing Foundation-1 audio tags.
You communicate ONLY in strict JSON format.

=== MUSIC THEORY & COMPOSITION WISDOM ===
1. FREQUENCY BALANCING: A great mix requires space. Never crowd the low-end; prioritize only one heavy Bass or Sub element at a time. Balance your mix across Lows (Bass, 808), Mids (Synths, Vocals, Guitars), and Highs (Bells, Airy pads, High strings).
2. TENSION & DYNAMICS: Manage energy flow dynamically. Create buildups by layering elements, increasing rhythmic complexity (e.g., triplets, fast notations), or introducing tension. Create emotional drops or breakdowns by stripping the mix back to 1-2 core elements.
3. HARMONIC COHESION: All elements MUST be in the exact Master Key. Pair foundational overarching chords ('chord progression', 'sustained') with rhythmic or melodic counterpoints ('arp', 'melody', 'staccato') that harmonize well.

=== DJ MIXING RULES ===
1. KEY MATCHING: Match the Master Key exactly.
2. CONTINUITY: Make gradual, smooth transitions. Carry over 1-2 existing stems from the previous step to anchor the listener, while swapping or introducing 1-2 new elements.
3. MIX DENSITY: Mix 3 to 5 stems concurrently.
4. EVOLUTION: Avoid looping the exact same combination of stems for too long. Keep the musical journey moving to prevent stagnation.

=== CHOSEN TAGS ===
ALLOWED TAGS:
- Major: Synth, Keys, Bass, Bowed Strings, Mallet, Wind, Guitar, Brass, Vocal, Plucked Strings
- Sub: Synth Lead, Synth Bass, Digital Piano, Pluck, Grand Piano, Bell, Pad, Atmosphere, Digital Strings, FM Synth, Violin, Digital Organ, Supersaw, Wavetable Bass, Rhodes Piano, Cello, Texture, Flute, Reese Bass, Wavetable Synth, Electric Bass, Marimba, Synthetic, Electric Guitar, Sub Bass, Trumpet, Pan Flute, Picked Bass, Digital Bass, Brass, Saxophone, Choir, Harp, Woodwinds, Church Organ, Pipe Organ, Church Bell, Koto, Felt Piano, Harpsichord, Steel Drums, Tubular Bells, Organ, Analog Bass, Sitar, Fiddle, Piccolo, World Winds, Nylon Guitar, Alto Sax, Acoustic Guitar, Soprano Sax, FM Bass, Celesta, Clavinet, Celtic Harp, Concert Harp, CP Piano, Guitar, Hammond Organ, Tack Piano, Wurlitzer Piano, Music Box, Analog Synth, Kalimba, Glockenspiel, Vibraphone, Ocarina, Xylophone, Viola, Bass Trombone, Tenor Trombone, Tenor Sax, Bassoon, Irish Flute, French Horn, Synth, Piano, Clarinet, Flugelhorn, Baritone Sax, Tuba, Oboe
- Timbre: Upper Mids, Mids, Highs, Warm, Wide, Bright, Low Mids, Thick, Airy, Rich, Tight, Full, Bass, Gritty, Clean, Retro, Saw, Snappy, Pluck, Crisp, Focused, Metallic, Chiptune, Dark, Shiny, Analog, Square, Present, Silky, Sparkly, Ambient, Near, Thin, Soft, Spacey, Smooth, Cold, Buzzy, Big, Subdued, Plucked, Far, Overdriven, Sub Bass, Deep, Woody, Dubstep, Round, Biting, Sine, Hollow, Fat, Punchy, Staccato, Nasal, Vintage, Growl, Intimate, Pulse, Harsh, Pitch Bend, Knock, Triangle, Bitcrush, Atmosphere, Formant Vocal, Ensemble, Acid, Muddy, Glassy, Breathy, Muffled, Laser, White Noise, Steel, Veiled, Rubbery, Mono, Reese, Synthetic Vox, Sub, Rumble, Noisy, Distant, Spiccato, Small, Bell, Boomy, Crispy, Bitcrushed, 808, Lead, Filter, Digital, Synthetic Choir, Nylon, Organ, Supersaw, Pizzicato, Armosphere, Pad, Choir, Siren, FX, Heavy, Electric Guitar, Dreamy, Tiny
- Notation: chord progression, melody, top melody, arp, triplets, simple, complex, rising, falling, strummed, sustained, catchy, epic, slow, fast
- FX: Low/Medium/High Reverb, Plate Reverb, Low/Medium/High Delay, Ping Pong Delay, Stereo/Cross/Mono Delay, Low/Medium/High Distortion, Phaser, Low/Medium/High Phaser, Bitcrush, High Bitcrush, Dry, Wet

Think like a seasoned DJ. Write a brief rationale in 'reasoning' detailing your approach to energy, frequencies, and transition, then provide the resulting tracks.

EXAMPLES OF VALID DIVERSE STEMS:
- Synth, Pad, Warm, Airy, sustained, Medium Reverb
- Bass, Synth Bass, Gritty, Deep, simple, Low Distortion
- Mallet, Marimba, Sparkly, catchy, triplets, Low Delay
- Bowed Strings, Violin, Silky, epic, melody, High Reverb
- Keys, Rhodes Piano, Vintage, Smooth, chord progression, Dry
- Guitar, Acoustic Guitar, Full, Clean, strummed, Low Reverb
- Wind, Pan Flute, Breathy, Ambient, simple, High Delay
- Vocal, Choir, Glassy, Dreamy, sustained, Plate Reverb
- Brass, Trumpet, Punchy, Silky, melody, Medium Reverb
- Plucked Strings, Harp, Sparkly, complex, arp, Medium Delay
- Synth, 303, Acid, Buzzy, simple, Phaser
- Bass, Wavetable Bass, Growl, Gritty, simple, High Distortion
- Bass, 808 Bass, Deep, Heavy, simple, Low Reverb
- Keys, Digital Piano, Bright, Pop, catchy, Medium Reverb
- Bowed Strings, Cello, Dark, Rich, sustained, High Reverb
- Mallet, Vibraphone, Mellow, Jazz, complex, Low Delay
- Guitar, Electric Guitar, Overdriven, Rock, strummed, High Distortion
- Wind, Ocarina, Airy, Ethnic, melody, High Delay
- Vocal, Vox, Formant, Synthetic, catchy, Medium Delay
- Plucked Strings, Banjo, Twangy, Fast, simple, Dry
"""

        self.user_message_template = """
=== CURRENT STATE ===
- Master BPM: {bpm}
- Master Key: {key}
- Currently Active Stems: {stems}
- Recent Mix History: {history}
- Allowed Families: {instruments}

Based on the current state, provide the next sequence of tracks to mix.
"""

    def get_next_state(self, current_bpm, current_key, active_stems, user_override="", available_instruments=None, stem_history=None, llm_config=None):
        if available_instruments is None:
            available_instruments = ["Any"]
        if stem_history is None:
            stem_history = []
            
        # Very compact history
        simple_history = []
        for loop_stems in stem_history[-3:]:
            prompts = [s.get('prompt', '').split(',')[0] for s in loop_stems]
            simple_history.append("+".join(prompts))
        history_str = " | ".join(simple_history)

        # Compact current
        simple_stems = [s.get('prompt', 'Unknown') for s in active_stems]

        if llm_config and llm_config.get('base_url'):
            client = OpenAI(base_url=llm_config['base_url'], api_key=llm_config['api_key'])
            model_name = llm_config['model']
        else:
            client = self.client
            model_name = self.model_name

        user_prompt = self.user_message_template.format(
            bpm=current_bpm,
            key=current_key,
            stems=", ".join(simple_stems),
            history=history_str,
            instruments=", ".join(available_instruments)
        )
        
        if user_override:
            user_prompt += f"\nOVERRIDE: {user_override}"
            
        print(f"DEBUG: Calling LLM ({model_name})...")
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": self.system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                timeout=60.0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "music_state",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "reasoning": { "type": "string", "description": "Brief 1-sentence musical rationale." },
                                "master_bpm": { "type": "integer", "enum": [100, 110, 120, 128, 130, 140, 150] },
                                "master_key": { "type": "string" },
                                "tracks": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "major_family": { "type": "string", "enum": ["Synth", "Keys", "Bass", "Bowed Strings", "Mallet", "Wind", "Guitar", "Brass", "Vocal", "Plucked Strings"] },
                                            "sub_family": { "type": "string", "enum": ["Synth Lead", "Synth Bass", "Digital Piano", "Pluck", "Grand Piano", "Bell", "Pad", "Atmosphere", "Digital Strings", "FM Synth", "Violin", "Digital Organ", "Supersaw", "Wavetable Bass", "Rhodes Piano", "Cello", "Texture", "Flute", "Reese Bass", "Wavetable Synth", "Electric Bass", "Marimba", "Synthetic", "Electric Guitar", "Sub Bass", "Trumpet", "Pan Flute", "Picked Bass", "Digital Bass", "Brass", "Saxophone", "Choir", "Harp", "Woodwinds", "Church Organ", "Pipe Organ", "Church Bell", "Koto", "Felt Piano", "Harpsichord", "Steel Drums", "Tubular Bells", "Organ", "Analog Bass", "Sitar", "Fiddle", "Piccolo", "World Winds", "Nylon Guitar", "Alto Sax", "Acoustic Guitar", "Soprano Sax", "FM Bass", "Celesta", "Clavinet", "Celtic Harp", "Concert Harp", "CP Piano", "Guitar", "Hammond Organ", "Tack Piano", "Wurlitzer Piano", "Music Box", "Analog Synth", "Kalimba", "Glockenspiel", "Vibraphone", "Ocarina", "Xylophone", "Viola", "Bass Trombone", "Tenor Trombone", "Tenor Sax", "Bassoon", "Irish Flute", "French Horn", "Synth", "Piano", "Clarinet", "Flugelhorn", "Baritone Sax", "Tuba", "Oboe"] },
                                            "timbre_tags": { 
                                                "type": "array", 
                                                "items": { "type": "string", "enum": ["Upper Mids", "Mids", "Highs", "Warm", "Wide", "Bright", "Low Mids", "Thick", "Airy", "Rich", "Tight", "Full", "Bass", "Gritty", "Clean", "Retro", "Saw", "Snappy", "Pluck", "Crisp", "Focused", "Metallic", "Chiptune", "Dark", "Shiny", "Analog", "Square", "Present", "Silky", "Sparkly", "Ambient", "Near", "Thin", "Soft", "Spacey", "Smooth", "Cold", "Buzzy", "Big", "Subdued", "Plucked", "Far", "Overdriven", "Sub Bass", "Deep", "Woody", "Dubstep", "Round", "Biting", "Sine", "Hollow", "Fat", "Punchy", "Staccato", "Nasal", "Vintage", "Growl", "Intimate", "Pulse", "Harsh", "Pitch Bend", "Knock", "Triangle", "Bitcrush", "Atmosphere", "Formant Vocal", "Ensemble", "Acid", "Muddy", "Glassy", "Breathy", "Muffled", "Laser", "White Noise", "Steel", "Veiled", "Rubbery", "Mono", "Reese", "Synthetic Vox", "Sub", "Rumble", "Noisy", "Distant", "Spiccato", "Small", "Bell", "Boomy", "Crispy", "Bitcrushed", "808", "Lead", "Filter", "Digital", "Synthetic Choir", "Nylon", "Organ", "Supersaw", "Pizzicato", "Armosphere", "Pad", "Choir", "Siren", "FX", "Heavy", "Electric Guitar", "Dreamy", "Tiny"] },
                                                "maxItems": 3
                                            },
                                            "notation_tag": { "type": "string", "enum": ["chord progression", "melody", "top melody", "arp", "triplets", "simple", "complex", "rising", "falling", "strummed", "sustained", "catchy", "epic", "slow", "fast"] },
                                            "fx_tag": { "type": "string", "enum": ["Low Reverb", "Medium Reverb", "High Reverb", "Plate Reverb", "Low Delay", "Medium Delay", "High Delay", "Ping Pong Delay", "Stereo Delay", "Cross Delay", "Mono Delay", "Low Distortion", "Medium Distortion", "High Distortion", "Phaser", "Low Phaser", "Medium Phaser", "High Phaser", "Bitcrush", "High Bitcrush", "Dry", "Wet"] },
                                            "bars": { "type": "integer", "enum": [4, 8] }
                                        },
                                        "required": ["major_family", "sub_family", "timbre_tags", "notation_tag", "fx_tag", "bars"],
                                        "additionalProperties": False
                                    }
                                }
                            },
                            "required": ["reasoning", "master_bpm", "master_key", "tracks"],
                            "additionalProperties": False
                        }
                    }
                }
            )
            
            content = response.choices[0].message.content
            return json.loads(content)
            
        except Exception as e:
            print(f"CRITICAL LLM ERROR: {e}")
            # NO FALLBACK TRACKS. Returning error state.
            return {
                "master_bpm": current_bpm,
                "master_key": current_key,
                "tracks": [],
                "reasoning": f"LLM FAILED: {e}"
            }
