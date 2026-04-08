"""Tests for frontend BPM/Key dropdown alignment with schema enums."""
import re
from pathlib import Path


class TestFrontendBPMAlignment:
    def test_bpm_presets_match_schema(self):
        """BPM preset buttons in index.html must all be in VALID_BPMS."""
        from app.lib.constants import VALID_BPMS

        html_path = Path("static/mc-clanker/index.html")
        html = html_path.read_text()
        presets = re.findall(r'data-bpm="(\d+)"', html)
        for bpm in presets:
            assert int(bpm) in VALID_BPMS, f"BPM preset {bpm} not in schema enum"

    def test_bpm_dropdown_values_match_schema(self):
        """All options in #bpm-override must be in VALID_BPMS."""
        from app.lib.constants import VALID_BPMS

        html_path = Path("static/mc-clanker/index.html")
        html = html_path.read_text()
        # Match option values that are digits (skip the empty Auto value)
        options = re.findall(r'<option value="(\d+)">', html)
        for bpm in options:
            assert int(bpm) in VALID_BPMS, f"BPM dropdown option {bpm} not in schema enum"

    def test_bpm_dropdown_has_no_empty_auto(self):
        """#bpm-override should not have <option value="">Auto</option>."""
        html_path = Path("static/mc-clanker/index.html")
        html = html_path.read_text()
        assert '<option value="">Auto</option>' not in html, \
            "BPM dropdown should not have empty-value Auto option"

    def test_key_dropdown_has_no_empty_auto(self):
        """#key-override should not have <option value="">Auto</option>."""
        html_path = Path("static/mc-clanker/index.html")
        html = html_path.read_text()
        # Find the key-override select and check it doesn't have Auto
        key_select = re.search(r'<select id="key-override"[^>]*>(.*?)</select>', html, re.DOTALL)
        if key_select:
            content = key_select.group(1)
            assert '<option value="">Auto</option>' not in content, \
                "Key dropdown should not have empty-value Auto option"
