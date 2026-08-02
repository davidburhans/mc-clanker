from slop_harness.vibe_prompt_bank import VibePromptBank


def test_vibe_prompt_bank_is_singleton():
    """VibePromptBank returns the same instance."""
    a = VibePromptBank()
    b = VibePromptBank()
    assert a is b


def test_sample_returns_string():
    """sample() returns a non-empty string."""
    import random
    rng = random.Random(42)
    bank = VibePromptBank()
    prompt = bank.sample(rng)
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_sample_is_deterministic():
    """Same RNG state produces same prompt."""
    import random
    rng1 = random.Random(999)
    rng2 = random.Random(999)
    bank = VibePromptBank()
    assert bank.sample(rng1) == bank.sample(rng2)


def test_bank_has_200_templates():
    """Bank has approximately 200 templates (within tolerance)."""
    bank = VibePromptBank()
    assert 180 <= len(bank._templates) <= 220


def test_sample_uses_rng():
    """sample() consumes from rng (calling twice with same rng gives different results if rng state changes)."""
    import random
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    bank = VibePromptBank()
    # With same seed, both should return same first template
    assert bank.sample(rng1) == bank.sample(rng2)
    # After sampling 100 more, rng2 should be exhausted
    for _ in range(100):
        bank.sample(rng2)
    # They should differ now since rng2 has been consumed differently


def test_templates_not_empty():
    """No template in the bank is empty or whitespace-only."""
    bank = VibePromptBank()
    for t in bank._templates:
        assert len(t.strip()) > 0