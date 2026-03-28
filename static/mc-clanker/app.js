// MC Clanker - Professional DJ Interface Logic

class DJSlopApp {
    constructor() {
        this.state = {
            isPlaying: false,
            isShowStarted: false,
            isRecording: false,
            volume: 0.8,
            bpm: 120,
            key: 'C minor',
            bpmOverride: null,
            keyOverride: null,
            vibe: '',
            vibeActive: false,
            activeVibePreset: null,
            instruments: {},
            reasoning: '',
            currentStems: [],
            prevStems: [],
            nextStems: [],
            isEngineRunning: false,
            lastActions: null,
            stemMixerData: [],
            loopCount: 0
        };

        this.recordingStartTime = null;
        this.recordingTimer = null;
        this.pollTimer = null;
        this.audioContext = null;
        this.analyser = null;
        this.source = null;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 2000;
        this.prevFreqData = null;
        this.vuLeft = null;
        this.vuRight = null;

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
        this.bindKeyPicker();
        this.bindShowModeToggle();
        this.resizeVisualizer();
        this.bindVibePresets();
        this.bindTempoPresets();
        this.populateCustomStemInstruments();
        this.populateCustomStemModels();
        this.bindBroadcastEvents();
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
        this.keyFull = document.getElementById('key-full');
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

        // End Show Modal
        this.endShowModal = document.getElementById('end-show-modal');
        this.endShowConfirmBtn = document.getElementById('end-show-confirm-btn');
        this.endShowCancelBtn = document.getElementById('end-show-cancel-btn');

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
        this.audiencePassword = document.getElementById('audience-password');
        this.togglePasswordBtn = document.getElementById('toggle-password');

        // Phase 3: New Elements
        this.stemDeck = document.getElementById('stem-deck');
        this.loopCounter = document.getElementById('loop-counter');
        this.headerLoopCounter = document.getElementById('header-loop-counter');
        this.cfgScale = document.getElementById('cfg-scale');
        this.cfgVal = document.getElementById('cfg-val');
        this.stepsRange = document.getElementById('steps-range');
        this.stepsVal = document.getElementById('steps-val');



        // VU meters
        this.vuLeftEl = document.getElementById('vu-left');
        this.vuRightEl = document.getElementById('vu-right');

        // Visualizer
        this.visualizer = document.getElementById('visualizer');
        this.visualizerCtx = this.visualizer.getContext('2d');

        // Show Control
        this.startShowBtn = document.getElementById('start-show-btn');
        this.showLiveIndicator = document.getElementById('show-live-indicator');
        this.endShowBtn = document.getElementById('end-show-btn');
        this.showStatusText = document.getElementById('show-status-text');
        this.queueText = document.getElementById('queue-text');
        this.showQueueStatus = document.getElementById('show-queue-status');

        this.showStartTime = null;
        this.queueTimerInterval = null;
    }

    bindEvents() {
        // Transport
        this.playBtn.addEventListener('click', () => this.togglePlay());
        this.stopBtn.addEventListener('click', () => this.stop());
        this.volumeSlider.addEventListener('input', (e) => this.setVolume(e.target.value));
        this.reconnectBtn = document.getElementById('reconnect-btn');
        this.reconnectBtn.addEventListener('click', () => this.reconnectStream());

        // Show Control
        this.startShowBtn.addEventListener('click', () => this.startShow());
        this.endShowBtn.addEventListener('click', () => this.endShow());

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

        // End Show Modal
        this.endShowConfirmBtn.addEventListener('click', () => this.confirmEndShow());
        this.endShowCancelBtn.addEventListener('click', () => this.closeEndShowModal());
        this.endShowModal.addEventListener('click', (e) => {
            if (e.target === this.endShowModal) this.closeEndShowModal();
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
                this.closeEndShowModal();
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
                const btn = e.target.closest('.stem-btn, .stem-remove-btn');
                if (!btn) return;
                const action = btn.dataset.action;
                const index = parseInt(btn.dataset.stemIndex);
                const set = btn.dataset.stemSet || 'active';
                if (action === 'mute') this.toggleStemMute(index);
                else if (action === 'solo') this.toggleStemSolo(index);
                else if (action === 'download') this.downloadStem(index, set);
                else if (action === 'remove-next') this.removeNextStem(index);
            });
        }
    }

    bindVibePresets() {
        document.querySelectorAll('.vibe-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                // Remove selected class from all chips
                document.querySelectorAll('.vibe-chip').forEach(c => c.classList.remove('selected'));
                // Add selected class to clicked chip
                chip.classList.add('selected');

                const vibe = chip.dataset.vibe;
                this.vibeInput.value = vibe;
                this.vibeCharCount.textContent = `${vibe.length}/200`;
                this.state.activeVibePreset = vibe;
                this.applyAll();

                // Remove selection after applying
                setTimeout(() => {
                    chip.classList.remove('selected');
                }, 1500);
            });
        });
    }

    bindTempoPresets() {
        document.querySelectorAll('.tempo-preset').forEach(btn => {
            btn.addEventListener('click', () => {
                const bpm = btn.dataset.bpm;
                this.bpmOverride.value = bpm;
                this.applyAll();
            });
        });
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

    bindKeyPicker() {
        const keyWheel = document.getElementById('key-display');
        const keyPickerGrid = document.getElementById('key-picker-grid');
        const keyPickBtns = document.querySelectorAll('.key-pick-btn');

        if (keyWheel && keyPickerGrid) {
            keyWheel.addEventListener('click', () => {
                keyPickerGrid.classList.toggle('active');
            });

            keyPickBtns.forEach(btn => {
                btn.addEventListener('click', () => {
                    const key = btn.dataset.key;
                    if (key) {
                        // Update selection visual
                        keyPickBtns.forEach(b => b.classList.remove('selected'));
                        btn.classList.add('selected');
                        // Set the value
                        this.keyOverride.value = key;
                        // Trigger apply
                        this.applyAll();
                        // Close picker
                        keyPickerGrid.classList.remove('active');
                    }
                });
            });
        }
    }

    bindShowModeToggle() {
        const modeAmbient = document.getElementById('mode-ambient');
        const modeLive = document.getElementById('mode-live');

        if (modeAmbient && modeLive) {
            modeAmbient.addEventListener('click', () => {
                modeAmbient.classList.add('active');
                modeLive.classList.remove('active');
                // Could trigger ambient mode API call here
            });

            modeLive.addEventListener('click', () => {
                modeLive.classList.add('active');
                modeAmbient.classList.remove('active');
            });
        }
    }

    toggleSection(header) {
        const rack = header.parentElement;
        const isCollapsed = rack.classList.toggle('collapsed');
        header.setAttribute('aria-expanded', !isCollapsed);
        header.classList.toggle('expanded', !isCollapsed);
    }

    initAudio() {
        this.audioPlayer.volume = this.state.volume;
        // Don't set src here - only request stream when user clicks Play
        // This prevents browser from connecting to stream before audio is actually playing
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
        await this.loadModelsConfig();
        this.updateStemMixer();
        await this.pollVRAM();
    }


    async loadModelsConfig() {
        try {
            // Fetch both models config and status
            const [modelsRes, statusRes] = await Promise.all([
                fetch("/api/models"),
                fetch("/api/models/status")
            ]);

            const modelsData = modelsRes.ok ? await modelsRes.json() : { models: {} };
            const statusData = statusRes.ok ? await statusRes.json() : { models: {} };

            const container = document.getElementById("models-list-container");
            if (!container) return;

            container.innerHTML = "";
            const models = modelsData.models || {};
            const modelStates = statusData.models || {};

            for (const [id, info] of Object.entries(models)) {
                const div = document.createElement("div");
                div.className = "model-item";

                const modelState = modelStates[id] || {};
                const state = modelState.state || "idle";
                const isLoaded = modelState.is_loaded || false;

                // State badge
                const badge = document.createElement("span");
                badge.className = `model-state-badge ${state}`;
                badge.textContent = state;
                badge.title = modelState.error || state;

                // Model label
                const label = document.createElement("label");
                const families = info.supported_families ? info.supported_families.join(", ") : "Any";
                label.textContent = id + " (" + info.engine + ")";
                label.title = info.description + " | Supported: " + families;

                // Action button
                const actionBtn = document.createElement("button");
                actionBtn.className = "model-action-btn";
                actionBtn.dataset.modelId = id;

                if (isLoaded) {
                    actionBtn.textContent = "Unload";
                    actionBtn.classList.add("unload");
                } else {
                    actionBtn.textContent = state === "loading" ? "Loading..." : "Load";
                    actionBtn.classList.add("load");
                    if (state === "loading") {
                        actionBtn.disabled = true;
                    }
                }

                actionBtn.onclick = async () => {
                    const action = isLoaded ? "unload" : "load";
                    actionBtn.disabled = true;
                    actionBtn.textContent = action === "load" ? "Loading..." : "Unloading...";

                    try {
                        const res = await fetch(`/api/models/${id}/${action}`, { method: "POST" });
                        if (res.ok) {
                            this.showToast(`${id} ${action}ed`);
                            await this.loadModelsConfig();
                            await this.pollVRAM();
                        } else {
                            const err = await res.json();
                            this.showToast(err.detail || `Failed to ${action} model`, "error");
                            actionBtn.disabled = false;
                            actionBtn.textContent = isLoaded ? "Unload" : "Load";
                        }
                    } catch (err) {
                        this.showToast(`Error ${action}ing model`, "error");
                        actionBtn.disabled = false;
                        actionBtn.textContent = isLoaded ? "Unload" : "Load";
                    }
                };

                // Enable checkbox
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.checked = info.enabled;
                checkbox.onchange = async (e) => {
                    try {
                        const res = await fetch("/api/models", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ model_id: id, enabled: e.target.checked })
                        });
                        if (res.ok) {
                            this.showToast(id + (e.target.checked ? " enabled" : " disabled") + ". Restart required.");
                        } else {
                            e.target.checked = !e.target.checked; // revert
                            this.showToast("Failed to update model", "error");
                        }
                    } catch (err) {
                        e.target.checked = !e.target.checked; // revert
                        this.showToast("Error updating model", "error");
                    }
                };

                div.appendChild(badge);
                div.appendChild(label);
                div.appendChild(actionBtn);
                div.appendChild(checkbox);
                container.appendChild(div);
            }
        } catch (e) {
            console.error("Failed to load models config:", e);
        }
    }

    updateUIFromState(data) {
        // Set Name
        this.state.current_set_name = data.current_set_name || 'Waiting for track...';

        // BPM - update both displays
        this.state.bpm = data.current_bpm || 120;
        if (this.bpmDisplay) {
            const tempoValueEl = this.bpmDisplay.querySelector('.tempo-value');
        if (tempoValueEl && tempoValueEl.textContent !== String(this.state.bpm)) {
            tempoValueEl.textContent = this.state.bpm;
            tempoValueEl.classList.remove('changing');
            void tempoValueEl.offsetWidth; // Trigger reflow
            tempoValueEl.classList.add('changing');
            setTimeout(() => tempoValueEl.classList.remove('changing'), 400);
        }
        }

        // Key - update wheel and full text
        this.state.key = data.current_key || 'C minor';
        if (this.keyDisplay) {
            const shortKey = this.shortenKey(this.state.key);
            this.keyDisplay.textContent = shortKey;
        }
        if (this.keyFull) {
            this.keyFull.textContent = this.state.key;
        }

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

        // Show started state
        this.state.isShowStarted = data.is_show_started || false;
        this.updateShowControls();

        // Update loop counter
        if (data.loop_count !== undefined) {
            const prevCount = this.state.loopCount;
            this.state.loopCount = data.loop_count;
            if (this.loopCounter) {
                this.loopCounter.textContent = data.loop_count;
                // Pulse animation when counter increments
                if (data.loop_count > prevCount) {
                    this.loopCounter.classList.remove('pulse');
                    void this.loopCounter.offsetWidth; // Force reflow
                    this.loopCounter.classList.add('pulse');
                    setTimeout(() => this.loopCounter.classList.remove('pulse'), 500);
                }
            }
            if (this.headerLoopCounter) {
                this.headerLoopCounter.textContent = data.loop_count;
                if (data.loop_count > prevCount) {
                    this.headerLoopCounter.classList.remove('pulse');
                    void this.headerLoopCounter.offsetWidth;
                    this.headerLoopCounter.classList.add('pulse');
                    setTimeout(() => this.headerLoopCounter.classList.remove('pulse'), 500);
                }
            }
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

    shortenKey(key) {
        if (!key) return '?';
        const match = key.match(/^([A-G]#?)\s*(minor|major)?$/i);
        if (!match) return key.substring(0, 2);
        const [, note, mode] = match;
        if (mode && mode.toLowerCase().startsWith('m')) {
            return note + 'm';
        }
        return note;
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
                    ${position === 'next' ? `
                    <button class="stem-remove-btn" data-action="remove-next" data-stem-index="${stem.index}" title="Remove from next loop">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"/>
                            <line x1="6" y1="6" x2="18" y2="18"/>
                        </svg>
                    </button>
                    ` : ''}
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

            // Add collapse toggle to category header
            header.addEventListener('click', (e) => {
                if (e.target.classList.contains('category-checkbox')) return;
                const isCollapsed = categoryEl.classList.toggle('collapsed');
                header.classList.toggle('expanded', !isCollapsed);
            });

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

        // Visual feedback
        const vizContainer = document.querySelector('.visualizer-container');
        if (vizContainer) vizContainer.classList.add('playing');

        // Ensure AudioContext is running before playback (required for Chrome)
        if (this.audioContext) {
            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume();
            }
            // If analyser was set up with a suspended context, reconnect the audio
            if (this.source && this.analyser) {
                try {
                    this.source.disconnect();
                } catch (e) {}
                try {
                    this.source.connect(this.analyser);
                } catch (e) {}
            }
        }

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

        // Visual feedback
        const vizContainer = document.querySelector('.visualizer-container');
        if (vizContainer) vizContainer.classList.remove('playing');
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

        // Visual feedback
        const vizContainer = document.querySelector('.visualizer-container');
        if (vizContainer) vizContainer.classList.remove('playing');

        this.showToast('Engine reset');
    }

    clearVibe() {
        this.state.vibe = '';
        this.state.vibeActive = false;
        this.state.activeVibePreset = null;
        this.vibeActiveIndicator.classList.remove('active');
        this.activeVibeText.textContent = '';
        this.activeVibeDisplay.classList.remove('visible');
        // Notify backend to clear the vibe
        this.applyState({ user_override: "" }).catch(e => {
            console.error('Clear vibe failed:', e);
        });
    }

    async startShow() {
        try {
            // Visual feedback - button loading state
            if (this.startShowBtn) {
                this.startShowBtn.disabled = true;
                const btnText = this.startShowBtn.querySelector('span');
                if (btnText) btnText.textContent = 'STARTING...';
            }

            await fetch('/api/show/start', { method: 'POST' });
            this.state.isShowStarted = true;
            this.updateShowControls();
            this.startShowTimer();

            // Update status text on DJ side
            if (this.showStatusText) this.showStatusText.textContent = 'Audience Connected';

            this.showToast('Show started - Audience now connected');

            // Add a brief visual flash to the show control bar
            const showControlBar = document.getElementById('show-control-bar');
            if (showControlBar) {
                showControlBar.classList.add('show-started-flash');
                setTimeout(() => showControlBar.classList.remove('show-started-flash'), 1000);
            }
        } catch (e) {
            console.error('Start show failed:', e);
            this.showToast('Failed to start show', 'error');
        } finally {
            // Reset button state
            if (this.startShowBtn) {
                this.startShowBtn.disabled = false;
                const btnText = this.startShowBtn.querySelector('span');
                if (btnText) btnText.textContent = 'START SHOW';
            }
        }
    }

    async endShow() {
        // Show confirmation modal instead of immediately ending
        this.openEndShowModal();
    }

    openEndShowModal() {
        if (this.endShowModal) {
            this.endShowModal.classList.add('active');
        }
    }

    closeEndShowModal() {
        if (this.endShowModal) {
            this.endShowModal.classList.remove('active');
        }
    }

    async confirmEndShow() {
        this.closeEndShowModal();
        this.stopShowTimer();

        // Visual feedback - update status immediately
        if (this.showStatusText) this.showStatusText.textContent = 'Ending broadcast...';

        try {
            await fetch('/api/show/stop', { method: 'POST' });
            this.state.isShowStarted = false;
            this.updateShowControls();
            this.showToast('Show ended - Audience sees waiting screen');
        } catch (e) {
            console.error('End show failed:', e);
            this.showToast('Failed to end show', 'error');
        }
    }

    startShowTimer() {
        this.showStartTime = Date.now();

        if (this.showQueueStatus) {
            this.showQueueStatus.style.display = 'flex';
        }

        this.queueTimerInterval = setInterval(() => {
            if (!this.showStartTime) return;

            const elapsed = Math.floor((Date.now() - this.showStartTime) / 1000);
            const mins = Math.floor(elapsed / 60).toString().padStart(2, '0');
            const secs = (elapsed % 60).toString().padStart(2, '0');

            if (this.queueText) {
                this.queueText.textContent = `${mins}:${secs} on air`;
            }
        }, 1000);
    }

    stopShowTimer() {
        if (this.queueTimerInterval) {
            clearInterval(this.queueTimerInterval);
            this.queueTimerInterval = null;
        }
        this.showStartTime = null;

        if (this.showQueueStatus) {
            this.showQueueStatus.style.display = 'none';
        }
    }

    updateShowControls() {
        if (this.state.isShowStarted) {
            this.startShowBtn.classList.add('hidden');
            this.showLiveIndicator.classList.remove('hidden');
            // Visual feedback - pulse the show live badge
            const badge = this.showLiveIndicator.querySelector('.show-live-badge');
            if (badge) {
                badge.classList.add('just-started');
                setTimeout(() => badge.classList.remove('just-started'), 2000);
            }
        } else {
            this.startShowBtn.classList.remove('hidden');
            this.showLiveIndicator.classList.add('hidden');
            this.stopShowTimer();
        }
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
        const btnText = this.applyAllBtn.querySelector('.btn-text');
        const btnIcon = this.applyAllBtn.querySelector('.btn-icon');
        if (btnText) btnText.textContent = 'Applying...';
        if (btnIcon) btnIcon.style.display = 'none';

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
                const btnText = this.applyAllBtn.querySelector('.btn-text');
                const btnIcon = this.applyAllBtn.querySelector('.btn-icon');
                if (btnText) btnText.textContent = 'Apply Settings';
                if (btnIcon) btnIcon.style.display = '';
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
            if (resp.ok) await this.loadModelsConfig();
        this.updateStemMixer();
        } catch (e) {
            console.error('Failed to toggle mute:', e);
        }
    }

    async toggleStemSolo(index) {
        try {
            const resp = await fetch(`/api/stems/${index}/solo`, { method: 'POST' });
            if (resp.ok) await this.loadModelsConfig();
        this.updateStemMixer();
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
            icecast_enabled: this.icecastEnabled.checked,
            audience_password: this.audiencePassword.value
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
        this.vramPollTimer = setInterval(() => this.pollVRAM(), 5000);
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
                    data.is_show_started !== this.state.isShowStarted ||
                    data.is_running !== this.state.isEngineRunning;

                if (hasChanged) {
                    this.updateUIFromState(data);
                    // Also refresh mixer data (volume, mute, solo)
                    await this.loadModelsConfig();
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

    async pollVRAM() {
        try {
            const response = await fetch('/api/vram');
            if (response.ok) {
                const data = await response.json();
                this.updateVRAMDisplay(data);
            }
        } catch (e) {
            console.error('Failed to poll VRAM:', e);
        }

        try {
            const response = await fetch('/api/download-progress');
            if (response.ok) {
                const data = await response.json();
                this.updateDownloadProgress(data);
            }
        } catch (e) {
            console.error('Failed to poll download progress:', e);
        }
    }

    updateVRAMDisplay(data) {
        let vramEl = document.getElementById('vram-meter');
        if (!vramEl) {
            // Create VRAM meter if it doesn't exist
            vramEl = document.createElement('div');
            vramEl.id = 'vram-meter';
            vramEl.className = 'vram-meter';

            // Find a good place to insert - after status indicator
            const statusIndicator = document.getElementById('status-indicator');
            if (statusIndicator && statusIndicator.parentNode) {
                statusIndicator.parentNode.insertBefore(vramEl, statusIndicator.nextSibling);
            }
        }

        const totalMB = data.total_mb || 0;
        const maxVRAM = 32 * 1024; // Assume 32GB max for percentage

        vramEl.innerHTML = `
            <div class="vram-bar-container">
                <div class="vram-bar" style="width: ${Math.min((totalMB / maxVRAM) * 100, 100)}%"></div>
            </div>
            <span class="vram-text">${totalMB.toFixed(0)} MB</span>
        `;
    }

    updateDownloadProgress(data) {
        let container = document.getElementById('download-progress-container');
        const downloads = data.downloads || {};

        // Only show if there are active downloads
        const activeDownloads = Object.entries(downloads).filter(([id, d]) => d.status === 'downloading');

        if (activeDownloads.length === 0) {
            if (container) {
                container.remove();
            }
            return;
        }

        if (!container) {
            container = document.createElement('div');
            container.id = 'download-progress-container';
            container.className = 'download-progress-container';
            document.body.appendChild(container);
        }

        container.innerHTML = activeDownloads.map(([id, d]) => `
            <div class="download-item">
                <div class="download-filename">${d.filename || id}</div>
                <div class="download-bar-container">
                    <div class="download-bar" style="width: ${d.progress || 0}%"></div>
                </div>
                <div class="download-percent">${Math.round(d.progress || 0)}%</div>
            </div>
        `).join('');
    }

    // Visualizer
    animateVisualizer() {
        if (!this.analyser && this.audioPlayer) {
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
            this.analyser.smoothingTimeConstant = 0.8;

            this.source = this.audioContext.createMediaElementSource(this.audioPlayer);
            this.source.connect(this.analyser);
            this.analyser.connect(this.audioContext.destination);
        } catch (e) {
            console.log('Audio analyser setup failed (likely due to autoplay policy):', e);
        }
    }

    resizeVisualizer() {
        const container = this.visualizer?.parentElement;
        if (!container) return;
        this.visualizer.width = container.clientWidth;
        this.visualizer.height = container.clientHeight;
    }

    drawVisualizer() {
        const ctx = this.visualizerCtx;
        const width = this.visualizer.width;
        const height = this.visualizer.height;

        // Clear with slight fade for trail effect
        ctx.fillStyle = 'rgba(5, 5, 8, 0.25)';
        ctx.fillRect(0, 0, width, height);

        if (!this.analyser) {
            // Draw animated placeholder - pulsing ambient waves
            const time = Date.now() * 0.001;
            for (let i = 0; i < 48; i++) {
                const phase = (i / 48) * Math.PI * 2;
                const barHeight = 15 + Math.sin(time * 1.5 + phase) * 12 + Math.sin(time * 0.7 + phase * 0.5) * 8;

                const gradient = ctx.createLinearGradient(0, height, 0, height - barHeight - 30);
                gradient.addColorStop(0, 'rgba(255, 170, 0, 0.9)');
                gradient.addColorStop(0.5, 'rgba(255, 136, 0, 0.6)');
                gradient.addColorStop(1, 'rgba(0, 229, 255, 0.15)');

                ctx.fillStyle = gradient;
                const x = (width / 48) * i + 2;
                const y = height - barHeight - 30;

                ctx.shadowBlur = 15;
                ctx.shadowColor = 'rgba(255, 170, 0, 0.6)';
                ctx.beginPath();
                ctx.roundRect(x, y, (width / 48) - 4, barHeight, 3);
                ctx.fill();
                ctx.shadowBlur = 0;
            }

            // Update VU meters with placeholder animation
            if (this.vuLeftEl && this.vuRightEl) {
                const vuLevel = 20 + Math.sin(time * 2) * 15 + Math.random() * 5;
                this.vuLeftEl.style.height = vuLevel + '%';
                this.vuRightEl.style.height = (vuLevel + Math.sin(time * 1.5) * 8) + '%';
            }
            return;
        }

        const bufferLength = this.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        this.analyser.getByteFrequencyData(dataArray);

        const barCount = 64;
        const barWidth = width / barCount;
        const gap = 3;

        // Calculate average for VU meters
        let leftSum = 0, rightSum = 0;
        const half = Math.floor(bufferLength / 2);
        for (let i = 0; i < half; i++) leftSum += dataArray[i];
        for (let i = half; i < bufferLength; i++) rightSum += dataArray[i];
        const leftAvg = (leftSum / half / 255) * 100;
        const rightAvg = (rightSum / half / 255) * 100;

        // Update VU meters
        if (this.vuLeftEl) this.vuLeftEl.style.height = leftAvg + '%';
        if (this.vuRightEl) this.vuRightEl.style.height = rightAvg + '%';

        // Draw center beam effect
        let avgValue = 0;
        for (let i = 0; i < bufferLength; i++) avgValue += dataArray[i];
        avgValue = avgValue / bufferLength / 255;

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

            const smoothed = this.prevFreqData ? this.prevFreqData[i] * 0.7 + value * 0.3 : value;
            if (!this.prevFreqData) this.prevFreqData = new Uint8Array(barCount);
            this.prevFreqData[i] = value;

            const barHeight = Math.max(4, (smoothed / 255) * (height - 50));

            const x = barWidth * i + gap;
            const y = height - barHeight - 30;

            const gradient = ctx.createLinearGradient(0, y + barHeight, 0, y);
            gradient.addColorStop(0, 'rgba(255, 170, 0, 1)');
            gradient.addColorStop(0.4, 'rgba(255, 136, 0, 0.9)');
            gradient.addColorStop(0.7, 'rgba(0, 229, 255, 0.8)');
            gradient.addColorStop(1, 'rgba(0, 229, 255, 0.3)');

            ctx.fillStyle = gradient;
            ctx.shadowBlur = 15;
            ctx.shadowColor = 'rgba(255, 170, 0, 0.7)';

            ctx.beginPath();
            ctx.roundRect(x, y, barWidth - gap * 2, barHeight, [4, 4, 0, 0]);
            ctx.fill();
            ctx.shadowBlur = 0;

            if (barHeight > 20) {
                ctx.shadowBlur = 12;
                ctx.shadowColor = 'rgba(255, 170, 0, 0.9)';
                ctx.fillStyle = 'rgba(255, 200, 100, 0.9)';
                ctx.fillRect(x + 2, y, barWidth - gap * 2 - 4, 2);
                ctx.shadowBlur = 0;
            }
        }
    }

    // === CUSTOM STEM CREATOR ===

    async populateCustomStemInstruments() {
        try {
            const response = await fetch('/api/instruments');
            if (response.ok) {
                const instruments = await response.json();
                const select = document.getElementById('custom-stem-instrument');
                if (!select) return;

                // Flatten all instruments into options
                select.innerHTML = '<option value="">Select instrument...</option>';
                for (const [category, items] of Object.entries(instruments)) {
                    if (category === 'Custom' && items.length === 0) continue;
                    const optgroup = document.createElement('optgroup');
                    optgroup.label = category;
                    items.forEach(item => {
                        const option = document.createElement('option');
                        option.value = item;
                        option.textContent = item;
                        optgroup.appendChild(option);
                    });
                    select.appendChild(optgroup);
                }
            }
        } catch (e) {
            console.error('Failed to load instruments:', e);
        }
    }

    async populateCustomStemModels() {
        try {
            const response = await fetch('/api/models');
            if (response.ok) {
                const data = await response.json();
                const select = document.getElementById('custom-stem-model');
                if (!select) return;

                select.innerHTML = '<option value="default">Default Model</option>';
                const models = data.models || {};
                for (const [id, info] of Object.entries(models)) {
                    if (info.enabled !== false) {
                        const option = document.createElement('option');
                        option.value = id;
                        option.textContent = id;
                        select.appendChild(option);
                    }
                }
            }
        } catch (e) {
            console.error('Failed to load models:', e);
        }
    }

    async createCustomStem() {
        const instrumentSelect = document.getElementById('custom-stem-instrument');
        const promptInput = document.getElementById('custom-stem-prompt');
        const modelSelect = document.getElementById('custom-stem-model');
        const btn = document.getElementById('add-custom-stem-btn');

        const instrument = instrumentSelect.value;
        const prompt = promptInput.value.trim();
        const modelId = modelSelect.value || 'default';

        if (!instrument || !prompt) {
            this.showToast('Please select an instrument and enter a prompt', 'error');
            return;
        }

        // Show loading state
        btn.classList.add('loading');
        const btnText = btn.querySelector('.btn-text');
        const btnLoading = btn.querySelector('.btn-loading');
        if (btnText) btnText.style.display = 'none';
        if (btnLoading) btnLoading.style.display = 'inline';

        try {
            const response = await fetch('/api/stems/custom', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ instrument, prompt, model_id: modelId })
            });

            if (response.ok) {
                const data = await response.json();
                this.showToast(`Custom stem added at position ${data.stem_index}`);
                promptInput.value = '';
                instrumentSelect.value = '';
                // Refresh stem deck to show new stem
                await this.updateStemMixer();
            } else {
                const err = await response.json();
                this.showToast(err.detail || 'Failed to create custom stem', 'error');
            }
        } catch (e) {
            console.error('Failed to create custom stem:', e);
            this.showToast('Failed to create custom stem', 'error');
        } finally {
            btn.classList.remove('loading');
            if (btnText) btnText.style.display = '';
            if (btnLoading) btnLoading.style.display = 'none';
        }
    }

    async removeNextStem(index) {
        try {
            const response = await fetch(`/api/stems/next/${index}`, {
                method: 'DELETE'
            });

            if (response.ok) {
                this.showToast('Stem removed from next loop');
                await this.updateStemMixer();
            } else {
                const err = await response.json();
                this.showToast(err.detail || 'Failed to remove stem', 'error');
            }
        } catch (e) {
            console.error('Failed to remove next stem:', e);
            this.showToast('Failed to remove stem', 'error');
        }
    }

    // === AUDIENCE MESSAGE BROADCAST ===

    async sendAudienceMessage() {
        const input = document.getElementById('audience-message-input');
        const btn = document.getElementById('broadcast-btn');

        const message = input.value.trim();
        if (!message) {
            this.showToast('Please enter a message', 'error');
            return;
        }

        // Disable button during send
        btn.disabled = true;
        btn.classList.add('sending');

        try {
            const response = await fetch('/api/message/audience', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message })
            });

            if (response.ok) {
                this.showToast('Message broadcast to audience');
                input.value = '';
                document.getElementById('broadcast-char-count').textContent = '0/200';
            } else {
                const err = await response.json();
                this.showToast(err.detail || 'Failed to send message', 'error');
            }
        } catch (e) {
            console.error('Failed to send audience message:', e);
            this.showToast('Failed to send message', 'error');
        } finally {
            btn.disabled = false;
            btn.classList.remove('sending');
        }
    }

    bindBroadcastEvents() {
        const messageInput = document.getElementById('audience-message-input');
        const charCount = document.getElementById('broadcast-char-count');
        const broadcastBtn = document.getElementById('broadcast-btn');
        const addStemBtn = document.getElementById('add-custom-stem-btn');

        if (messageInput && charCount) {
            messageInput.addEventListener('input', () => {
                charCount.textContent = `${messageInput.value.length}/200`;
            });

            messageInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendAudienceMessage();
                }
            });
        }

        if (broadcastBtn) {
            broadcastBtn.addEventListener('click', () => this.sendAudienceMessage());
        }

        if (addStemBtn) {
            addStemBtn.addEventListener('click', () => this.createCustomStem());
        }
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.djSlop = new DJSlopApp();
});
