// MC Clanker - Professional DJ Interface Logic

class DJSlopApp {
    constructor() {
        this.state = {
            isPlaying: false,
            isRecording: false,
            volume: 0.8,
            bpm: 120,
            key: 'C minor',
            bpmOverride: null,
            keyOverride: null,
            vibe: '',
            vibeActive: false,
            instruments: {},
            reasoning: '',
            currentStems: [],
            prevStems: [],
            nextStems: [],
            isEngineRunning: false,
            lastActions: null,
            stemMixerData: []
        };

        this.recordingStartTime = null;
        this.recordingTimer = null;
        this.pollTimer = null;
        this.audioContext = null;
        this.analyser = null;
        this.source = null;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 2000; // ms between reconnect attempts

        this.init();
    }

    init() {
        this.bindElements();
        this.bindEvents();
        this.initAudio();
        this.loadState();
        this.startPolling();
        this.animateVisualizer();
        this.bindInstrumentRackToggle();
        this.resizeVisualizer();
    }

    bindElements() {
        // Status
        this.statusIndicator = document.getElementById('status-indicator');

        // Transport
        this.playBtn = document.getElementById('play-btn');
        this.stopBtn = document.getElementById('stop-btn');
        this.volumeSlider = document.getElementById('volume-slider');
        this.volumeValue = document.getElementById('volume-value');
        this.audioPlayer = document.getElementById('audio-player');

        // DJ Controls
        this.bpmDisplay = document.getElementById('bpm-display');
        this.keyDisplay = document.getElementById('key-display');
        this.bpmOverride = document.getElementById('bpm-override');
        this.keyOverride = document.getElementById('key-override');
        this.vibeInput = document.getElementById('vibe-input');
        this.vibeCharCount = document.getElementById('vibe-char-count');
        this.vibeActiveIndicator = document.getElementById('vibe-active-indicator');
        this.instrumentCategories = document.getElementById('instrument-categories');
        this.applyAllBtn = document.getElementById('apply-all-btn');

        // Info
        this.reasoningBox = document.getElementById('reasoning-box');
        this.actionLog = document.getElementById('action-log');
        this.currentTrackName = document.getElementById('current-track-name');

        // Export
        this.recordBtn = document.getElementById('record-btn');
        this.recordLabel = document.getElementById('record-label');
        this.exportFormat = document.getElementById('export-format');
        this.recordingIndicator = document.getElementById('recording-indicator');
        this.recordingTime = document.getElementById('recording-time');

        // Stop Modal
        this.stopModal = document.getElementById('stop-modal');
        this.stopConfirmBtn = document.getElementById('stop-confirm-btn');
        this.stopCancelBtn = document.getElementById('stop-cancel-btn');

        // Active Vibe Display
        this.activeVibeDisplay = document.getElementById('active-vibe-display');
        this.activeVibeText = document.getElementById('active-vibe-text');
        this.clearVibeBtn = document.getElementById('clear-vibe-btn');

        // Settings
        this.settingsBtn = document.getElementById('settings-btn');
        this.settingsModal = document.getElementById('settings-modal');
        this.closeSettings = document.getElementById('close-settings');
        this.saveSettingsBtn = document.getElementById('save-settings-btn');
        this.testConnectionBtn = document.getElementById('test-connection-btn');
        this.llmUrl = document.getElementById('llm-url');
        this.llmKey = document.getElementById('llm-key');
        this.llmModel = document.getElementById('llm-model');
        this.icecastEnabled = document.getElementById('icecast-enabled');
        this.togglePasswordBtn = document.getElementById('toggle-password');
        
        // Phase 3: New Elements
        this.stemDeck = document.getElementById('stem-deck');
        this.loopCounter = document.getElementById('loop-counter');
        this.cfgScale = document.getElementById('cfg-scale');
        this.cfgVal = document.getElementById('cfg-val');
        this.stepsRange = document.getElementById('steps-range');
        this.stepsVal = document.getElementById('steps-val');

        // Visualizer
        this.visualizer = document.getElementById('visualizer');
        this.visualizerCtx = this.visualizer.getContext('2d');
    }

    bindEvents() {
        // Transport
        this.playBtn.addEventListener('click', () => this.togglePlay());
        this.stopBtn.addEventListener('click', () => this.stop());
        this.volumeSlider.addEventListener('input', (e) => this.setVolume(e.target.value));
        this.reconnectBtn = document.getElementById('reconnect-btn');
        this.reconnectBtn.addEventListener('click', () => this.reconnectStream());

        // DJ Controls - unified apply
        this.vibeInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.applyAll();
            }
        });
        this.vibeInput.addEventListener('input', () => {
            this.vibeCharCount.textContent = `${this.vibeInput.value.length}/200`;
        });
        this.applyAllBtn.addEventListener('click', () => this.applyAll());

        // Export
        this.recordBtn.addEventListener('click', () => this.toggleRecording());

        // Stop Modal
        this.stopConfirmBtn.addEventListener('click', () => this.confirmStop());
        this.stopCancelBtn.addEventListener('click', () => this.closeStopModal());
        this.stopModal.addEventListener('click', (e) => {
            if (e.target === this.stopModal) this.closeStopModal();
        });

        // Active Vibe Clear
        this.clearVibeBtn.addEventListener('click', () => this.clearVibe());

        // Settings
        this.settingsBtn.addEventListener('click', () => this.openSettings());
        this.closeSettings.addEventListener('click', () => this.closeSettingsModal());
        this.saveSettingsBtn.addEventListener('click', () => this.saveSettings());
        this.testConnectionBtn.addEventListener('click', () => this.testConnection());
        this.togglePasswordBtn.addEventListener('click', () => this.togglePasswordVisibility());

        // Generation Config
        if (this.cfgScale) {
            this.cfgScale.addEventListener('input', () => {
                this.cfgVal.textContent = parseFloat(this.cfgScale.value).toFixed(1);
                this.updateGenerationConfig();
            });
        }
        if (this.stepsRange) {
            this.stepsRange.addEventListener('input', () => {
                this.stepsVal.textContent = this.stepsRange.value;
                this.updateGenerationConfig();
            });
        }

        // Settings modal - close on backdrop click
        this.settingsModal.addEventListener('click', (e) => {
            if (e.target === this.settingsModal) this.closeSettingsModal();
        });

        // Settings - Enter key to save
        this.settingsModal.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && this.settingsModal.classList.contains('active')) {
                e.preventDefault();
                this.saveSettings();
            }
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.code === 'Space' && !e.target.matches('input, textarea')) {
                e.preventDefault();
                this.togglePlay();
            }
            if (e.code === 'Escape') {
                this.closeSettingsModal();
                this.closeStopModal();
            }
        });

        // Window resize
        window.addEventListener('resize', () => this.resizeVisualizer());

        // Stem deck event delegation
        if (this.stemDeck) {
            this.stemDeck.addEventListener('input', (e) => {
                if (e.target.classList.contains('stem-vol-slider')) {
                    const index = parseInt(e.target.dataset.stemIndex);
                    this.setStemVolume(index, e.target.value);
                }
            });
            this.stemDeck.addEventListener('click', (e) => {
                const btn = e.target.closest('.stem-btn');
                if (!btn) return;
                const action = btn.dataset.action;
                const index = parseInt(btn.dataset.stemIndex);
                const set = btn.dataset.stemSet || 'active';
                if (action === 'mute') this.toggleStemMute(index);
                else if (action === 'solo') this.toggleStemSolo(index);
                else if (action === 'download') this.downloadStem(index, set);
            });
        }
    }

    bindInstrumentRackToggle() {
        const header = document.querySelector('.instrument-rack .panel-header');
        if (header) {
            header.addEventListener('click', () => this.toggleSection(header));
            header.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.toggleSection(header);
                }
            });
        }
    }

    toggleSection(header) {
        const rack = header.parentElement;
        const isCollapsed = rack.classList.toggle('collapsed');
        header.setAttribute('aria-expanded', !isCollapsed);
    }

    initAudio() {
        this.audioPlayer.volume = this.state.volume;
        this.audioPlayer.src = '/stream.mp3?t=' + Date.now();
        this.audioPlayer.crossOrigin = 'anonymous';

        // Show connecting state
        this.audioPlayer.addEventListener('waiting', () => {
            this.setStatus('connecting');
        });

        this.audioPlayer.addEventListener('playing', () => {
            this.setStatus('connected');
            this.reconnectAttempts = 0; // Reset reconnect counter on successful play
        });

        this.audioPlayer.addEventListener('error', () => {
            this.setStatus('disconnected');
            this.handleAudioError();
        });

        // Also handle ended event - stream may end without error
        this.audioPlayer.addEventListener('ended', () => {
            this.setStatus('disconnected');
            this.handleAudioError();
        });
    }

    handleAudioError() {
        if (!this.state.isPlaying) return; // Only auto-reconnect if we think we should be playing
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            this.showToast('Stream disconnected after multiple attempts. Use the reconnect button.', 'error');
            return;
        }
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * this.reconnectAttempts;
        this.showToast(`Stream interrupted, reconnecting... (attempt ${this.reconnectAttempts})`);
        setTimeout(() => {
            if (this.state.isPlaying) {
                this.reconnectStream();
            }
        }, delay);
    }

    setStatus(status) {
        switch(status) {
            case 'connecting':
                this.statusIndicator.className = 'status connecting';
                this.statusIndicator.textContent = 'Connecting...';
                break;
            case 'connected':
                this.statusIndicator.className = 'status connected';
                this.statusIndicator.textContent = 'LIVE';
                break;
            case 'paused':
                this.statusIndicator.className = 'status connected paused';
                this.statusIndicator.textContent = 'Paused';
                break;
            case 'disconnected':
            default:
                this.statusIndicator.className = 'status disconnected';
                this.statusIndicator.textContent = 'Disconnected';
                break;
        }
    }

    async loadState() {
        try {
            const response = await fetch('/api/state');
            if (response.ok) {
                const data = await response.json();
                this.updateUIFromState(data);
                this.state.isEngineRunning = data.is_running || false;
                this.setStatus(this.state.isPlaying ? 'connected' : (this.state.isEngineRunning ? 'paused' : 'disconnected'));
            } else {
                this.setStatus('disconnected');
            }
        } catch (e) {
            console.error('Failed to load state:', e);
            this.setStatus('disconnected');
            this.showToast('Cannot connect to server', 'error');
        }

        // Load instruments
        try {
            const response = await fetch('/api/instruments');
            if (response.ok) {
                const instruments = await response.json();
                this.renderInstruments(instruments);
            }
        } catch (e) {
            console.error('Failed to load instruments:', e);
        }

        // Load LLM config
        try {
            const response = await fetch('/api/llm-config');
            if (response.ok) {
                const config = await response.json();
                this.llmUrl.value = config.base_url || '';
                this.llmKey.value = config.api_key || '';
                this.llmModel.value = config.model || '';
                this.icecastEnabled.checked = config.icecast_enabled || false;
            }
        } catch (e) {
            console.error('Failed to load LLM config:', e);
        }

        // Load mixer data
        this.updateStemMixer();
    }

    updateUIFromState(data) {
        // Set Name
        this.state.current_set_name = data.current_set_name || 'Waiting for track...';

        // BPM
        this.state.bpm = data.current_bpm || 120;
        this.bpmDisplay.textContent = this.state.bpm;

        // Key
        this.state.key = data.current_key || 'C minor';
        this.keyDisplay.textContent = this.state.key;

        // Reasoning
        this.state.reasoning = data.llm_reasoning || '';
        this.reasoningBox.innerHTML = this.state.reasoning
            ? `<p>${this.state.reasoning}</p>`
            : '<p class="placeholder">Waiting for track...</p>';

        // Stems
        this.state.currentStems = data.active_stems || [];
        this.state.prevStems = data.previous_stems || [];
        this.state.nextStems = data.next_stems || [];

        // Playing state from backend
        this.state.isPlaying = data.is_generating || false;
        this.updatePlayButton();

        // Update loop counter
        if (this.loopCounter && data.loop_count !== undefined) {
            this.loopCounter.textContent = `LOOP: ${data.loop_count}`;
        }

        // Action Log
        if (this.actionLog && data.last_actions) {
            const newActions = JSON.stringify(data.last_actions);
            if (newActions !== this.state.lastActions) {
                this.state.lastActions = newActions;
                this.actionLog.innerHTML = data.last_actions.map(action =>
                    `<div class="action-entry">${action}</div>`
                ).join('');
            }
        }
    }

    renderStemDeck() {
        // Current track name
        this.currentTrackName.textContent = this.state.current_set_name || 'Waiting for track...';

        if (!this.stemDeck) return;

        // Build unified list of all stems with position
        const allStems = [];

        // Previous stems
        this.state.prevStems.forEach((stem, idx) => {
            allStems.push({ ...stem, position: 'previous', index: idx });
        });

        // Current stems - merge with mixer data
        const mixerData = this.state.stemMixerData || [];
        this.state.currentStems.forEach((stem, idx) => {
            const mixer = mixerData.find(m => m.index === idx) || {};
            allStems.push({
                ...stem,
                position: 'current',
                index: idx,
                volume: mixer.volume ?? 1.0,
                is_muted: mixer.is_muted ?? false,
                is_soloed: mixer.is_soloed ?? false,
                hasMixerControls: true
            });
        });

        // Next stems
        this.state.nextStems.forEach((stem, idx) => {
            allStems.push({ ...stem, position: 'next', index: idx });
        });

        if (allStems.length === 0) {
            this.stemDeck.innerHTML = '<p class="placeholder">No stems yet</p>';
            return;
        }

        this.stemDeck.innerHTML = allStems.map(stem => this.renderStemRow(stem)).join('');
    }

    async updateStemMixer() {
        try {
            const response = await fetch('/api/stems');
            if (response.ok) {
                this.state.stemMixerData = await response.json();
                this.renderStemDeck();
            }
        } catch (e) {
            console.error('Failed to update stem mixer:', e);
        }
    }

    renderStemRow(stem) {
        const position = stem.position || 'current';
        const prompt = stem.prompt || '';
        const bars = stem.bars || '';
        const hasControls = stem.hasMixerControls && stem.index !== undefined;
        const hasDownload = stem.position === 'previous' || hasControls;

        // Render tags
        const tags = prompt.split(',').map(tag => {
            const trimmed = tag.trim().toLowerCase();
            let type = 'default';
            if (trimmed.includes('drum') || trimmed.includes('kick') || trimmed.includes('snare') || trimmed.includes('hihat') || trimmed.includes('hat')) type = 'drums';
            else if (trimmed.includes('bass')) type = 'bass';
            else if (trimmed.includes('synth') || trimmed.includes('pad') || trimmed.includes('lead')) type = 'synth';
            else if (trimmed.includes('vocal') || trimmed.includes('voice') || trimmed.includes('singer')) type = 'vocals';
            return `<span class="stem-preview-tag" data-type="${type}">${tag.trim()}</span>`;
        }).join('');

        const timeInfo = bars ? `${bars} bars` : '';

        return `
            <div class="stem-row ${position}">
                <div class="stem-row-header">
                    <span class="stem-position-label">${position.toUpperCase()}</span>
                    ${timeInfo ? `<span class="stem-time">${timeInfo}</span>` : ''}
                </div>
                <div class="stem-tags">${tags}</div>
                ${hasControls ? `
                <div class="stem-controls">
                    <input type="range" class="stem-vol-slider" min="0" max="2" step="0.05"
                           value="${stem.volume ?? 1.0}"
                           data-stem-index="${stem.index}">
                    <div class="stem-btns">
                        <button class="stem-btn ${stem.is_muted ? 'active-mute' : ''}" data-action="mute" data-stem-index="${stem.index}">M</button>
                        <button class="stem-btn ${stem.is_soloed ? 'active-solo' : ''}" data-action="solo" data-stem-index="${stem.index}">S</button>
                        <button class="stem-btn stem-btn-dl" data-action="download" data-stem-index="${stem.index}" data-stem-set="${position}">↓</button>
                    </div>
                </div>
                ` : ''}
                ${hasDownload && !hasControls ? `
                <div class="stem-controls">
                    <div class="stem-btns">
                        <button class="stem-btn stem-btn-dl" data-action="download" data-stem-index="${stem.index}" data-stem-set="${position}">↓</button>
                    </div>
                </div>
                ` : ''}
            </div>
        `;
    }

    renderInstruments(instruments) {
        this.instrumentCategories.innerHTML = '';
        this.state.instruments = instruments;

        for (const [category, items] of Object.entries(instruments)) {
            if (category === 'Custom' && items.length === 0) continue;

            const categoryEl = document.createElement('div');
            categoryEl.className = 'instrument-category';

            const header = document.createElement('div');
            header.className = 'category-header';
            header.innerHTML = `
                <input type="checkbox" class="category-checkbox" data-category="${category}" checked>
                <span>${category}</span>
            `;

            const list = document.createElement('div');
            list.className = 'instruments-list';

            items.forEach(item => {
                const itemEl = document.createElement('label');
                itemEl.className = 'instrument-item selected';
                itemEl.innerHTML = `
                    <input type="checkbox" value="${item}" checked>
                    ${item}
                `;
                list.appendChild(itemEl);
            });

            categoryEl.appendChild(header);
            categoryEl.appendChild(list);
            this.instrumentCategories.appendChild(categoryEl);

            // Category checkbox toggle
            header.querySelector('.category-checkbox').addEventListener('change', (e) => {
                const checked = e.target.checked;
                list.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = checked);
                list.querySelectorAll('.instrument-item').forEach(el => {
                    el.classList.toggle('selected', checked);
                });
            });

            // Individual instrument checkboxes - update category checkbox
            list.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                cb.addEventListener('change', (e) => {
                    e.target.closest('.instrument-item').classList.toggle('selected', e.target.checked);
                    this.updateCategoryCheckbox(header.querySelector('.category-checkbox'), list);
                });
            });
        }
    }

    updateCategoryCheckbox(categoryCheckbox, listEl) {
        const allItems = listEl.querySelectorAll('input');
        const checkedItems = listEl.querySelectorAll('input:checked');
        categoryCheckbox.checked = allItems.length > 0 && allItems.length === checkedItems.length;
    }

    // Transport Controls
    togglePlay() {
        if (this.state.isPlaying) {
            this.pause();
        } else {
            this.play();
        }
    }

    play() {
        this.state.isPlaying = true;
        this.updatePlayButton();
        this.setStatus('connecting');
        this.reconnectAttempts = 0; // Reset reconnect counter on manual play

        // Refresh the audio stream source before playing
        this.audioPlayer.src = '/stream.mp3?t=' + Date.now();
        this.audioPlayer.load();

        // Apply state to backend
        this.applyState({ is_generating: true }).catch(e => {
            console.error('Play failed:', e);
            this.showToast('Failed to start playback', 'error');
        });

        // Play with proper error handling
        const playPromise = this.audioPlayer.play();
        if (playPromise !== undefined) {
            playPromise.catch(e => {
                console.error('Play error:', e);
                // If play fails, show disconnected state
                if (this.state.isPlaying) {
                    this.setStatus('disconnected');
                }
            });
        }
    }

    pause() {
        this.state.isPlaying = false;
        this.updatePlayButton();
        this.setStatus('paused');
        this.reconnectAttempts = 0; // Don't auto-reconnect when paused
        this.applyState({ is_generating: false }).catch(e => {
            console.error('Pause failed:', e);
        });
        this.audioPlayer.pause();
    }

    stop() {
        this.openStopModal();
    }

    openStopModal() {
        this.stopModal.classList.add('active');
    }

    closeStopModal() {
        this.stopModal.classList.remove('active');
    }

    confirmStop() {
        this.closeStopModal();
        this.state.isPlaying = false;
        this.updatePlayButton();
        this.setStatus('disconnected');
        this.clearOverrideIndicators();
        this.clearVibe();
        this.reconnectAttempts = 0; // Reset reconnect counter on stop
        this.applyState({ is_generating: false, should_reset: true }).catch(e => {
            console.error('Stop failed:', e);
        });
        this.audioPlayer.pause();
        this.audioPlayer.currentTime = 0;
        this.audioPlayer.src = '/stream.mp3?t=' + Date.now();
        this.showToast('Engine reset');
    }

    clearVibe() {
        this.state.vibe = '';
        this.state.vibeActive = false;
        this.vibeActiveIndicator.classList.remove('active');
        this.activeVibeText.textContent = '';
        this.activeVibeDisplay.classList.remove('visible');
    }

    updatePlayButton() {
        this.playBtn.classList.toggle('playing', this.state.isPlaying);
        this.playBtn.classList.toggle('play', !this.state.isPlaying);
        this.playBtn.setAttribute('aria-pressed', this.state.isPlaying.toString());
    }

    reconnectStream() {
        this.showToast('Reconnecting stream...');
        this.audioPlayer.src = '/stream.mp3?t=' + Date.now();
        this.audioPlayer.load();
        if (this.state.isPlaying) {
            this.audioPlayer.play().then(() => {
                this.reconnectAttempts = 0; // Reset on successful reconnect
            }).catch(e => {
                console.error('Reconnect play error:', e);
                this.showToast('Reconnect failed', 'error');
            });
        }
    }

    setVolume(value) {
        this.state.volume = value / 100;
        this.audioPlayer.volume = this.state.volume;
        this.volumeValue.textContent = `${value}%`;
    }

    // DJ Controls - Unified Apply
    applyAll() {
        const selectedInstruments = [];
        this.instrumentCategories.querySelectorAll('.instrument-item input:checked').forEach(cb => {
            selectedInstruments.push(cb.value);
        });

        const bpmVal = this.bpmOverride.value ? parseInt(this.bpmOverride.value) : null;
        const keyVal = this.keyOverride.value || null;
        const vibeVal = this.vibeInput.value.trim();

        const updates = {
            target_bpm_override: bpmVal,
            target_key_override: keyVal,
            available_instruments: selectedInstruments
        };

        if (vibeVal) {
            updates.user_override = vibeVal;
        }

        // Apply loading state
        this.applyAllBtn.disabled = true;
        this.applyAllBtn.classList.add('loading');
        this.applyAllBtn.textContent = 'Applying...';

        this.applyState(updates)
            .then(() => {
                if (bpmVal) {
                    this.state.bpm = bpmVal;
                    this.bpmDisplay.textContent = bpmVal;
                    this.bpmOverride.classList.add('override-active');
                    this.bpmOverride.value = '';
                }
                if (keyVal) {
                    this.state.key = keyVal;
                    this.keyDisplay.textContent = keyVal;
                    this.keyOverride.classList.add('override-active');
                    this.keyOverride.value = '';
                }
                if (vibeVal) {
                    this.state.vibe = vibeVal;
                    this.state.vibeActive = true;
                    this.vibeActiveIndicator.classList.add('active');
                    this.activeVibeText.textContent = vibeVal;
                    this.activeVibeDisplay.classList.add('visible');
                    this.vibeInput.value = '';
                    this.vibeCharCount.textContent = '0/200';
                }

                let msg = 'Settings applied';
                if (bpmVal) msg += ` (BPM: ${bpmVal})`;
                if (keyVal) msg += ` (Key: ${keyVal})`;
                if (vibeVal) msg += ` - "${vibeVal}"`;
                this.showToast(msg);
            })
            .catch((e) => {
                console.error('Apply failed:', e);
                this.showToast('Failed to apply settings', 'error');
            })
            .finally(() => {
                this.applyAllBtn.disabled = false;
                this.applyAllBtn.classList.remove('loading');
                this.applyAllBtn.textContent = 'Apply All Settings';
            });
    }

    async setStemVolume(index, volume) {
        // Update local state immediately for responsive UI
        const mixerEntry = this.state.stemMixerData.find(m => m.index === index);
        if (mixerEntry) {
            mixerEntry.volume = parseFloat(volume);
        }

        try {
            await fetch(`/api/stems/${index}/volume`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ volume: parseFloat(volume) })
            });
        } catch (e) {
            console.error('Failed to set stem volume:', e);
        }
    }

    async toggleStemMute(index) {
        try {
            const resp = await fetch(`/api/stems/${index}/mute`, { method: 'POST' });
            if (resp.ok) this.updateStemMixer();
        } catch (e) {
            console.error('Failed to toggle mute:', e);
        }
    }

    async toggleStemSolo(index) {
        try {
            const resp = await fetch(`/api/stems/${index}/solo`, { method: 'POST' });
            if (resp.ok) this.updateStemMixer();
        } catch (e) {
            console.error('Failed to toggle solo:', e);
        }
    }

    async downloadStem(index, set = 'active') {
        window.open(`/api/stems/${index}/download?set=${set}`, '_blank');
    }

    async updateGenerationConfig() {
        if (!this.cfgScale || !this.stepsRange) return;
        try {
            await fetch('/api/generation-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cfg_scale: parseFloat(this.cfgScale.value),
                    steps: parseInt(this.stepsRange.value)
                })
            });
        } catch (e) {
            console.error('Failed to update generation config:', e);
        }
    }

    async loadGenerationConfig() {
        try {
            const response = await fetch('/api/generation-config');
            if (response.ok) {
                const config = await response.json();
                if (this.cfgScale) {
                    this.cfgScale.value = config.cfg_scale;
                    this.cfgVal.textContent = config.cfg_scale.toFixed(1);
                }
                if (this.stepsRange) {
                    this.stepsRange.value = config.steps;
                    this.stepsVal.textContent = config.steps;
                }
            }
        } catch (e) {
            console.error('Failed to load generation config:', e);
        }
    }

    showToast(message, type = 'success') {
        const existing = document.querySelector('.toast-notification');
        if (existing) existing.remove();

        const toast = document.createElement('div');
        toast.className = 'toast-notification';
        toast.textContent = message;
        if (type === 'error') {
            toast.style.borderColor = 'var(--danger)';
            toast.style.boxShadow = '0 4px 20px rgba(255, 68, 68, 0.2)';
        }
        document.body.appendChild(toast);

        requestAnimationFrame(() => {
            toast.classList.add('visible');
        });

        setTimeout(() => {
            toast.classList.remove('visible');
            setTimeout(() => toast.remove(), 300);
        }, 2500);
    }

    async applyState(updates) {
        const response = await fetch('/api/state', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updates)
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
    }

    // Recording
    toggleRecording() {
        if (this.state.isRecording) {
            this.stopRecording();
        } else {
            this.startRecording();
        }
    }

    async startRecording() {
        try {
            const response = await fetch('/api/export/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ format: this.exportFormat.value })
            });

            if (response.ok) {
                this.state.isRecording = true;
                this.recordBtn.classList.add('recording');
                this.recordLabel.textContent = 'Stop Recording';
                this.recordingIndicator.classList.add('active');
                this.recordingStartTime = Date.now();
                this.updateRecordingTime();
                this.recordingTimer = setInterval(() => this.updateRecordingTime(), 1000);
                this.showToast('Recording started');
            } else {
                const err = await response.json();
                this.showToast(err.detail || 'Failed to start recording', 'error');
            }
        } catch (e) {
            console.error('Failed to start recording:', e);
            this.showToast('Failed to start recording', 'error');
        }
    }

    async stopRecording() {
        let filePath = null;
        try {
            const response = await fetch('/api/export/stop', {
                method: 'POST'
            });

            if (response.ok) {
                const data = await response.json();
                filePath = data.file_path;
            } else {
                const err = await response.json();
                this.showToast(err.detail || 'Failed to stop recording', 'error');
            }
        } catch (e) {
            console.error('Failed to stop recording:', e);
            this.showToast('Failed to stop recording', 'error');
        }

        this.state.isRecording = false;
        this.recordBtn.classList.remove('recording');
        this.recordLabel.textContent = 'Record to File';
        this.recordingIndicator.classList.remove('active');
        clearInterval(this.recordingTimer);
        this.recordingTime.textContent = '00:00';

        if (filePath) {
            const filename = filePath.split('/').pop();
            this.showToast('Recording saved: ' + filename);
        }
    }

    updateRecordingTime() {
        if (!this.recordingStartTime) return;
        const elapsed = Math.floor((Date.now() - this.recordingStartTime) / 1000);
        const mins = Math.floor(elapsed / 60).toString().padStart(2, '0');
        const secs = (elapsed % 60).toString().padStart(2, '0');
        this.recordingTime.textContent = `${mins}:${secs}`;
    }

    // Settings
    openSettings() {
        this.settingsModal.classList.add('active');
        this.llmUrl.focus();
    }

    closeSettingsModal() {
        this.settingsModal.classList.remove('active');
    }

    async saveSettings() {
        const url = this.llmUrl.value.trim();

        // Validate URL format
        if (url) {
            try {
                new URL(url);
            } catch (e) {
                this.showToast('Invalid URL format', 'error');
                this.llmUrl.focus();
                return;
            }
        }

        this.saveSettingsBtn.disabled = true;
        this.saveSettingsBtn.textContent = 'Saving...';

        const config = {
            base_url: url,
            api_key: this.llmKey.value,
            model: this.llmModel.value,
            icecast_enabled: this.icecastEnabled.checked
        };

        try {
            const response = await fetch('/api/llm-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });
            if (response.ok) {
                this.closeSettingsModal();
                this.showToast('Settings saved');
            } else {
                const err = await response.json();
                this.showToast(err.detail || 'Failed to save settings', 'error');
            }
        } catch (e) {
            console.error('Failed to save settings:', e);
            this.showToast('Failed to save settings', 'error');
        } finally {
            this.saveSettingsBtn.disabled = false;
            this.saveSettingsBtn.textContent = 'Save Settings';
        }
    }

    async testConnection() {
        const url = this.llmUrl.value.trim();
        if (!url) {
            this.showToast('Please enter a URL first', 'error');
            return;
        }

        this.testConnectionBtn.disabled = true;
        this.testConnectionBtn.textContent = 'Testing...';

        try {
            // Try to fetch models list from the LLM endpoint
            const response = await fetch(url + '/models', {
                method: 'GET',
                headers: { 'Authorization': 'Bearer ' + this.llmKey.value }
            });

            if (response.ok) {
                const data = await response.json();
                const modelName = data.data?.[0]?.id || 'Unknown';
                this.showToast(`Connection successful - Model: ${modelName}`);
            } else {
                // Try a simple completion test
                const testResponse = await fetch(url + '/chat/completions', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + this.llmKey.value
                    },
                    body: JSON.stringify({
                        model: this.llmModel.value || 'local-model',
                        messages: [{ role: 'user', content: 'test' }],
                        max_tokens: 5
                    })
                });

                if (testResponse.ok) {
                    this.showToast('Connection successful');
                } else {
                    this.showToast('Connection failed: ' + testResponse.status, 'error');
                }
            }
        } catch (e) {
            this.showToast('Connection failed: ' + e.message, 'error');
        } finally {
            this.testConnectionBtn.disabled = false;
            this.testConnectionBtn.textContent = 'Test Connection';
        }
    }

    togglePasswordVisibility() {
        const isPassword = this.llmKey.type === 'password';
        this.llmKey.type = isPassword ? 'text' : 'password';
        this.togglePasswordBtn.innerHTML = isPassword ? '&#128064;' : '&#128065;';
        this.togglePasswordBtn.setAttribute('aria-label', isPassword ? 'Hide password' : 'Show password');
    }

    clearOverrideIndicators() {
        this.bpmOverride.classList.remove('override-active');
        this.keyOverride.classList.remove('override-active');
        this.vibeActiveIndicator.classList.remove('active');
        this.activeVibeText.textContent = '';
        this.activeVibeDisplay.classList.remove('visible');
        this.state.vibeActive = false;
    }

    // Polling
    startPolling() {
        this.pollTimer = setInterval(() => this.pollState(), 2000);
    }

    async pollState() {
        try {
            const response = await fetch('/api/state');
            if (response.ok) {
                const data = await response.json();

                // Check if anything meaningful changed before updating UI
                const hasChanged =
                    data.current_set_name !== this.state.current_set_name ||
                    data.current_bpm !== this.state.bpm ||
                    data.current_key !== this.state.key ||
                    data.llm_reasoning !== this.state.reasoning ||
                    data.loop_count !== this.state.loop_count ||
                    JSON.stringify(data.active_stems) !== JSON.stringify(this.state.currentStems) ||
                    JSON.stringify(data.previous_stems) !== JSON.stringify(this.state.prevStems) ||
                    JSON.stringify(data.next_stems) !== JSON.stringify(this.state.nextStems) ||
                    data.is_generating !== this.state.isPlaying ||
                    data.is_running !== this.state.isEngineRunning;

                if (hasChanged) {
                    this.updateUIFromState(data);
                    // Also refresh mixer data (volume, mute, solo)
                    this.updateStemMixer();
                }

                this.state.isEngineRunning = data.is_running || false;
                if (data.is_running === false) {
                    this.setStatus('disconnected');
                } else if (data.is_generating) {
                    this.setStatus('connected');
                } else {
                    this.setStatus('paused');
                }
            } else {
                this.setStatus('disconnected');
            }
        } catch (e) {
            this.setStatus('disconnected');
        }
    }

    // Visualizer
    animateVisualizer() {
        if (!this.analyser) {
            this.setupAnalyser();
        }

        this.drawVisualizer();
        requestAnimationFrame(() => this.animateVisualizer());
    }

    setupAnalyser() {
        try {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;

            this.source = this.audioContext.createMediaElementSource(this.audioPlayer);
            this.source.connect(this.analyser);
            this.analyser.connect(this.audioContext.destination);
        } catch (e) {
            console.log('Audio analyser setup failed (likely due to autoplay policy):', e);
        }
    }

    resizeVisualizer() {
        const container = this.visualizer.parentElement;
        this.visualizer.width = container.clientWidth;
        this.visualizer.height = container.clientHeight;
    }

    drawVisualizer() {
        // Don't resize here - only resize on window resize event
        const ctx = this.visualizerCtx;
        const width = this.visualizer.width;
        const height = this.visualizer.height;

        // Clear with slight fade for trail effect
        ctx.fillStyle = 'rgba(6, 6, 10, 0.25)';
        ctx.fillRect(0, 0, width, height);

        if (!this.analyser) {
            // Draw animated placeholder - pulsing ambient waves
            const time = Date.now() * 0.001;
            for (let i = 0; i < 48; i++) {
                const phase = (i / 48) * Math.PI * 2;
                const barHeight = 15 + Math.sin(time * 1.5 + phase) * 12 + Math.sin(time * 0.7 + phase * 0.5) * 8;

                // Gradient per bar
                const gradient = ctx.createLinearGradient(0, height, 0, height - barHeight - 30);
                gradient.addColorStop(0, 'rgba(255, 170, 0, 0.9)');
                gradient.addColorStop(0.5, 'rgba(255, 136, 0, 0.6)');
                gradient.addColorStop(1, 'rgba(0, 240, 255, 0.15)');

                ctx.fillStyle = gradient;
                const x = (width / 48) * i + 2;
                const y = height - barHeight - 30;

                // Rounded top bars with enhanced glow
                ctx.shadowBlur = 15;
                ctx.shadowColor = 'rgba(255, 170, 0, 0.6)';
                ctx.beginPath();
                ctx.roundRect(x, y, (width / 48) - 4, barHeight, 3);
                ctx.fill();
                ctx.shadowBlur = 0;

                // Top highlight
                if (barHeight > 18) {
                    ctx.shadowBlur = 10;
                    ctx.shadowColor = 'rgba(255, 255, 200, 0.8)';
                    ctx.fillStyle = 'rgba(255, 220, 150, 0.9)';
                    ctx.fillRect(x + 2, y, (width / 48) - 8, 2);
                    ctx.shadowBlur = 0;
                }
            }
            return;
        }

        const bufferLength = this.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        this.analyser.getByteFrequencyData(dataArray);

        const barCount = 64;
        const barWidth = width / barCount;
        const gap = 3;

        // Calculate average for ambient glow
        let avgValue = 0;
        for (let i = 0; i < bufferLength; i++) {
            avgValue += dataArray[i];
        }
        avgValue = avgValue / bufferLength / 255;

        // Draw center beam effect
        if (this.state.isPlaying && avgValue > 0.3) {
            const beamGradient = ctx.createRadialGradient(
                width / 2, height / 2, 0,
                width / 2, height / 2, width * 0.4
            );
            beamGradient.addColorStop(0, `rgba(255, 170, 0, ${avgValue * 0.15})`);
            beamGradient.addColorStop(0.5, `rgba(255, 68, 0, ${avgValue * 0.08})`);
            beamGradient.addColorStop(1, 'transparent');
            ctx.fillStyle = beamGradient;
            ctx.fillRect(0, 0, width, height);
        }

        for (let i = 0; i < barCount; i++) {
            const dataIndex = Math.floor((i / barCount) * bufferLength);
            const value = dataArray[dataIndex];

            // Smooth the values with previous frame (if stored)
            const smoothed = this.prevFreqData ? this.prevFreqData[i] * 0.7 + value * 0.3 : value;
            if (!this.prevFreqData) this.prevFreqData = new Uint8Array(barCount);
            this.prevFreqData[i] = value;

            const barHeight = Math.max(4, (smoothed / 255) * (height - 50));

            const x = barWidth * i + gap;
            const y = height - barHeight - 30;

            // Multi-stop gradient for each bar
            const gradient = ctx.createLinearGradient(0, y + barHeight, 0, y);
            gradient.addColorStop(0, 'rgba(255, 170, 0, 1)');
            gradient.addColorStop(0.4, 'rgba(255, 136, 0, 0.9)');
            gradient.addColorStop(0.7, 'rgba(0, 240, 255, 0.8)');
            gradient.addColorStop(1, 'rgba(0, 240, 255, 0.3)');

            ctx.fillStyle = gradient;

            // Enhanced glow
            ctx.shadowBlur = 15;
            ctx.shadowColor = 'rgba(255, 170, 0, 0.7)';

            // Draw bar with rounded top
            ctx.beginPath();
            ctx.roundRect(x, y, barWidth - gap * 2, barHeight, [4, 4, 0, 0]);
            ctx.fill();
            ctx.shadowBlur = 0;

            // Top highlight glow with bloom effect
            if (barHeight > 20) {
                ctx.shadowBlur = 12;
                ctx.shadowColor = 'rgba(255, 170, 0, 0.9)';
                ctx.fillStyle = 'rgba(255, 200, 100, 0.9)';
                ctx.fillRect(x + 2, y, barWidth - gap * 2 - 4, 2);

                // Secondary bloom
                ctx.shadowBlur = 20;
                ctx.shadowColor = 'rgba(255, 170, 0, 0.4)';
                ctx.fillRect(x + 4, y + 2, barWidth - gap * 2 - 8, 1);
                ctx.shadowBlur = 0;
            }

            // Reflection effect
            const reflectionGradient = ctx.createLinearGradient(0, height - 20, 0, height);
            reflectionGradient.addColorStop(0, 'rgba(0, 240, 255, 0.2)');
            reflectionGradient.addColorStop(1, 'rgba(0, 240, 255, 0)');
            ctx.fillStyle = reflectionGradient;
            ctx.fillRect(x, height - 20, barWidth - gap * 2, 15);
        }
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.djSlop = new DJSlopApp();
});
