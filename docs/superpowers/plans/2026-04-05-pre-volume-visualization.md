# Pre-Volume Audio Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move audio visualization processing before volume adjustment in both DJ and audience interfaces, so the visualizer sees unmodified audio levels.

**Architecture:** The current audio chain uses `HTMLAudioElement.volume` for volume control, which applies volume *before* `createMediaElementSource` captures audio. The fix inserts a Web Audio `GainNode` for volume control, so the chain becomes: `source → analyser → gainNode → destination`. The analyser sees full-level audio while volume is controlled via `gainNode.gain.value`. `audioPlayer.volume` is kept fixed at 1.0.

**Tech Stack:** Web Audio API (`AudioContext`, `GainNode`, `AnalyserNode`), vanilla JS, HTML5 Audio

---

## Files to Modify

| File | Changes |
|------|---------|
| `static/audience/app.js` | Add `gainNode`, update `setupAudio()`, redirect volume control to gainNode |
| `static/mc-clanker/app.js` | Add `gainNode`, update `setupAnalyser()`, redirect volume control to gainNode, fix reconnection logic |

---

## Task 1: Update Audience App — Add gainNode to constructor and fix init

**Files:** `static/audience/app.js:57-62`, `static/audience/app.js:66-78`

- [ ] **Step 1: Add gainNode property in constructor**

In the constructor, after `this.analyser = null;` (line 58), add:
```javascript
this.gainNode = null;
```

- [ ] **Step 2: Fix init() to set audioPlayer.volume = 1.0 and use gainNode for volume**

In `init()` (lines 65-78), change:
```javascript
// OLD (line 67):
this.audioPlayer.volume = this.volumeSlider.value / 100;
// NEW:
this.audioPlayer.volume = 1.0; // Fixed - volume controlled via gainNode
```

```javascript
// OLD (lines 74-78):
this.volumeSlider.addEventListener('input', (e) => {
    this.audioPlayer.volume = e.target.value / 100;
    this.volumeValue.textContent = e.target.value;
    this.updateVolumeFill(e.target.value);
});
// NEW:
this.volumeSlider.addEventListener('input', (e) => {
    const vol = e.target.value / 100;
    this.audioPlayer.volume = 1.0; // Always max on element
    if (this.gainNode) this.gainNode.gain.value = vol;
    this.volumeValue.textContent = e.target.value;
    this.updateVolumeFill(e.target.value);
});
```

---

## Task 2: Update Audience App — Insert gainNode into audio chain

**Files:** `static/audience/app.js:133-163`

- [ ] **Step 1: Update setupAudio() to create and use gainNode**

Replace the entire `setupAudio()` method (lines 133-163) with:

```javascript
setupAudio() {
    // If analyser and gainNode exist and context is running, nothing to do
    if (this.analyser && this.gainNode && this.audioContext && this.audioContext.state !== 'suspended') {
        return;
    }

    // If we have a context but no analyser or gainNode (setup failed before), recreate both
    if (this.audioContext && (!this.analyser || !this.gainNode)) {
        this.audioContext.close().catch(() => {});
        this.audioContext = null;
    }

    if (!this.audioContext) {
        try {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;
            this.analyser.smoothingTimeConstant = 0.8;
            this.gainNode = this.audioContext.createGain();
            this.gainNode.gain.value = this.volumeSlider.value / 100; // Match initial slider

            const source = this.audioContext.createMediaElementSource(this.audioPlayer);
            source.connect(this.analyser);
            this.analyser.connect(this.gainNode);
            this.gainNode.connect(this.audioContext.destination);
        } catch (e) {
            console.error("Analyser setup failed:", e);
            this.audioContext = null;
            this.analyser = null;
            this.gainNode = null;
        }
    } else if (this.audioContext.state === 'suspended') {
        // If context was suspended, resume it
        this.audioContext.resume();
    }
}
```

---

## Task 3: Update DJ App — Add gainNode to constructor and fix initAudio

**Files:** `static/mc-clanker/app.js:38-42`, `static/mc-clanker/app.js:470-474`

- [ ] **Step 1: Add gainNode property in constructor**

After `this.source = null;` (line 40), add:
```javascript
this.gainNode = null;
```

- [ ] **Step 2: Fix initAudio() to set audioPlayer.volume = 1.0**

In `initAudio()` (line 471), change:
```javascript
// OLD:
this.audioPlayer.volume = this.state.volume;
// NEW:
this.audioPlayer.volume = 1.0; // Fixed - volume controlled via gainNode
```

---

## Task 4: Update DJ App — Insert gainNode into audio chain

**Files:** `static/mc-clanker/app.js:1954-1967`

- [ ] **Step 1: Update setupAnalyser() to create and use gainNode**

Replace `setupAnalyser()` (lines 1954-1967) with:

```javascript
setupAnalyser() {
    try {
        this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        this.analyser = this.audioContext.createAnalyser();
        this.analyser.fftSize = 256;
        this.analyser.smoothingTimeConstant = 0.8;
        this.gainNode = this.audioContext.createGain();
        this.gainNode.gain.value = this.state.volume; // Match current volume

        this.source = this.audioContext.createMediaElementSource(this.audioPlayer);
        this.source.connect(this.analyser);
        this.analyser.connect(this.gainNode);
        this.gainNode.connect(this.audioContext.destination);
    } catch (e) {
        console.log('Audio analyser setup failed (likely due to autoplay policy):', e);
    }
}
```

---

## Task 5: Update DJ App — Redirect setVolume to gainNode

**Files:** `static/mc-clanker/app.js:1354-1358`

- [ ] **Step 1: Update setVolume() to use gainNode**

Replace `setVolume()` (lines 1354-1358):
```javascript
// OLD:
setVolume(value) {
    this.state.volume = value / 100;
    this.audioPlayer.volume = this.state.volume;
    this.volumeValue.textContent = `${value}%`;
}

// NEW:
setVolume(value) {
    this.state.volume = value / 100;
    this.audioPlayer.volume = 1.0; // Fixed - volume controlled via gainNode
    if (this.gainNode) this.gainNode.gain.value = this.state.volume;
    this.volumeValue.textContent = `${value}%`;
}
```

---

## Task 6: Update DJ App — Fix play() reconnection logic for gainNode

**Files:** `static/mc-clanker/app.js:1091-1097`

- [ ] **Step 1: Update analyser reconnection in play() to include gainNode chain**

In the `play()` method (lines 1091-1097), update the reconnection logic:
```javascript
// OLD:
if (this.source && this.analyser) {
    try {
        this.source.disconnect();
    } catch (e) {}
    try {
        this.source.connect(this.analyser);
    } catch (e) {}
}

// NEW:
if (this.source && this.analyser && this.gainNode) {
    try {
        this.source.disconnect();
    } catch (e) {}
    try {
        this.analyser.disconnect();
    } catch (e) {}
    try {
        this.source.connect(this.analyser);
        this.analyser.connect(this.gainNode);
        this.gainNode.connect(this.audioContext.destination);
    } catch (e) {}
}
```

---

## Verification

- [ ] **Step 1: Load DJ interface** (`/dj`) and verify no console errors on page load
- [ ] **Step 2: Click Play** — verify audio plays and visualizer animates
- [ ] **Step 3: Adjust volume slider** — verify audio volume changes and visualizer bars remain at consistent height (not affected by volume)
- [ ] **Step 4: Load audience interface** (`/listen`) and repeat steps 2-3
- [ ] **Step 5: Test volume at different levels** — visualizer should show same amplitude range whether volume is at 10% or 100%
