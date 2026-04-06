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
            loopCount: 0,
            // Loop sync fields
            loopHistory: [],                    // Past loops for navigation
            currentVisibleLoopIndex: 1,         // Which loop DJ is viewing
            currentlyPlayingLoopIndex: 0,       // Which loop is actually audible
            currentlyPlayingSetName: '',         // Set name actually playing
            currentlyPlayingReasoning: '',       // Reasoning actually playing
            nextQueuedStems: [],                // Next queued stems (planned)
        };

        this.recordingStartTime = null;
        this.recordingTimer = null;
        this.pollTimer = null;
        this.audioContext = null;
        this.analyser = null;
        this.source = null;
        this.gainNode = null;
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
        this.bindVibeSectionToggle();
        this.bindInfoPanelCollapsibles();
        this.bindKeyPicker();
        this.bindShowModeToggle();
        this.resizeVisualizer();
        this.bindVibePresets();
        this.bindTempoPresets();
        this.populateCustomStemInstruments();
        this.populateCustomStemModels();
        this.bindBroadcastEvents();
        // Hide generating indicator initially
        this.hideGeneratingIndicator();
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
        this.loopSelector = document.getElementById('loop-selector');
        this.loopNum = document.getElementById('loop-num');
        this.loopTotal = document.getElementById('loop-total');
        this.nowPlayingBadge = document.getElementById('now-playing-badge');
        this.loopPrevBtn = document.getElementById('loop-prev');
        this.loopNextBtn = document.getElementById('loop-next');
        this.jumpToCurrentBtn = document.getElementById('jump-current');
        this.cfgScale = document.getElementById('cfg-scale');
        this.cfgVal = document.getElementById('cfg-val');
        this.stepsRange = document.getElementById('steps-range');
        this.stepsVal = document.getElementById('steps-val');

        // New UI Elements
        this.toastContainer = document.getElementById('toast-container');
        this.generatingIndicator = document.getElementById('generating-indicator');
        this.generatingLabel = document.getElementById('generating-label');
        this.generationProgressBar = document.getElementById('generation-progress-bar');
        this.rotaryKeySelector = document.getElementById('rotary-key-selector');
        this.rotaryKeyDisplay = document.getElementById('rotary-key-display');
        this.rotaryKeyType = document.getElementById('rotary-key-type');
        this.keyPickerFlyout = document.getElementById('key-picker-flyout');



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

        // Loop selector navigation
        if (this.loopPrevBtn) {
            this.loopPrevBtn.addEventListener('click', () => this.navigateToLoop(this.state.currentVisibleLoopIndex - 1));
        }
        if (this.loopNextBtn) {
            this.loopNextBtn.addEventListener('click', () => this.navigateToLoop(this.state.currentVisibleLoopIndex + 1));
        }
        if (this.jumpToCurrentBtn) {
            this.jumpToCurrentBtn.addEventListener('click', () => this.jumpToCurrent());
        }

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

    bindVibeSectionToggle() {
        const header = document.querySelector('.vibe-section .panel-header');
        if (header) {
            header.addEventListener('click', () => this.toggleVibeSection(header));
            header.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.toggleVibeSection(header);
                }
            });
        }
    }

    toggleVibeSection(header) {
        const section = header.parentElement;
        const isCollapsed = section.classList.toggle('collapsed');
        header.setAttribute('aria-expanded', !isCollapsed);
    }

    bindInfoPanelCollapsibles() {
        const sections = document.querySelectorAll('.info-panel .panel-header.collapsible');
        sections.forEach(header => {
            header.addEventListener('click', () => this.toggleInfoPanelSection(header));
            header.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.toggleInfoPanelSection(header);
                }
            });
        });
    }

    toggleInfoPanelSection(header) {
        const section = header.parentElement;
        const isCollapsed = section.classList.toggle('collapsed');
        header.setAttribute('aria-expanded', !isCollapsed);
    }

    bindKeyPicker() {
        const keyPickBtns = document.querySelectorAll('.key-pick-btn');

        if (this.rotaryKeySelector && this.keyPickerFlyout) {
            // Toggle flyout on click
            this.rotaryKeySelector.addEventListener('click', (e) => {
                e.stopPropagation();
                this.keyPickerFlyout.classList.toggle('visible');
            });

            // Close on outside click
            document.addEventListener('click', (e) => {
                if (!this.keyPickerFlyout.contains(e.target) && e.target !== this.rotaryKeySelector) {
                    this.keyPickerFlyout.classList.remove('visible');
                }
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
                        // Update rotary display with spin animation
                        if (this.rotaryKeySelector) {
                            this.rotaryKeySelector.classList.add('spinning');
                            setTimeout(() => this.rotaryKeySelector.classList.remove('spinning'), 150);
                        }
                        // Update rotary display
                        this.updateKeyDisplay(key);
                        // Trigger apply
                        this.applyAll();
                        // Close picker
                        this.keyPickerFlyout.classList.remove('visible');
                    }
                });
            });
        }
    }

    updateKeyDisplay(key) {
        if (!key) return;
        const isMinor = key.toLowerCase().includes('minor');
        const baseKey = key.replace(' major', '').replace(' minor', '');
        this.rotaryKeyDisplay.textContent = isMinor ? baseKey + 'm' : baseKey;
        this.rotaryKeyType.textContent = isMinor ? 'minor' : 'major';
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
        this.audioPlayer.volume = 1.0; // Fixed - volume controlled via gainNode
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
            // Show reconnect button in error state
            if (this.reconnectBtn) {
                this.reconnectBtn.classList.add('error');
            }
            return;
        }
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * this.reconnectAttempts;

        // Update reconnect button to show attempt
        if (this.reconnectBtn) {
            this.reconnectBtn.classList.add('reconnecting');
            this.reconnectBtn.setAttribute('data-attempt', this.reconnectAttempts);
        }

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
    }


    async loadModelsConfig() {
        try {
            // Fetch models config
            const modelsRes = await fetch("/api/models");
            const modelsData = modelsRes.ok ? await modelsRes.json() : { models: {} };

            const container = document.getElementById("models-list-container");
            if (!container) return;

            container.innerHTML = "";
            const models = modelsData.models || {};

            for (const [id, info] of Object.entries(models)) {
                const div = document.createElement("div");
                div.className = "model-item";

                // Model label
                const label = document.createElement("label");
                const families = info.supported_families ? info.supported_families.join(", ") : "Any";
                label.textContent = id + " (" + (info.engine || 'unknown') + ")";
                label.title = (info.description || '') + " | Supported: " + families;

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
                            this.showToast(id + (e.target.checked ? " enabled" : " disabled") + ". Worker will update.");
                        } else {
                            e.target.checked = !e.target.checked; // revert
                            this.showToast("Failed to update model", "error");
                        }
                    } catch (err) {
                        e.target.checked = !e.target.checked; // revert
                        this.showToast("Error updating model", "error");
                    }
                };

                div.appendChild(label);
                div.appendChild(checkbox);
                container.appendChild(div);
            }
        } catch (e) {
            console.error("Failed to load models config:", e);
        }
    }

    updateUIFromState(data) {
        // Track Overrides and Instruments
        this.state.availableInstruments = data.available_instruments || [];
        this.state.targetBpmOverride = data.target_bpm_override;
        this.state.targetKeyOverride = data.target_key_override;
        this.state.userOverride = data.user_override;

        // Sync Instrument Checkboxes
        if (this.instrumentCategories) {
            this.instrumentCategories.querySelectorAll('.instrument-toggle').forEach(toggle => {
                const label = toggle.querySelector('.toggle-label').textContent.trim();
                if (this.state.availableInstruments.includes(label)) {
                    toggle.classList.add('active');
                } else {
                    toggle.classList.remove('active');
                }
            });
        }

        // Sync BPM Override Input
        if (this.state.targetBpmOverride !== null && this.state.targetBpmOverride !== undefined) {
            if (this.bpmOverride && document.activeElement !== this.bpmOverride) {
                this.bpmOverride.value = this.state.targetBpmOverride;
                this.bpmOverride.classList.add('override-active');
            }
        } else {
            if (this.bpmOverride && document.activeElement !== this.bpmOverride) {
                this.bpmOverride.value = '';
                this.bpmOverride.classList.remove('override-active');
            }
        }

        // Sync Key Override Input
        if (this.state.targetKeyOverride) {
            if (this.keyOverride && document.activeElement !== this.keyOverride) {
                this.keyOverride.value = this.state.targetKeyOverride;
                this.keyOverride.classList.add('override-active');
            }
        } else {
            if (this.keyOverride && document.activeElement !== this.keyOverride) {
                this.keyOverride.value = '';
                this.keyOverride.classList.remove('override-active');
            }
        }

        // Sync Vibe Override Input
        if (this.state.userOverride) {
            if (this.vibeInput && document.activeElement !== this.vibeInput) {
                this.vibeInput.value = this.state.userOverride;
            }
            this.state.vibeActive = true;
            if (this.vibeActiveIndicator) this.vibeActiveIndicator.classList.add('active');
            if (this.activeVibeText) this.activeVibeText.textContent = this.state.userOverride;
            if (this.activeVibeDisplay) this.activeVibeDisplay.classList.add('visible');
        } else {
            if (this.vibeInput && document.activeElement !== this.vibeInput) {
                this.vibeInput.value = '';
            }
            this.state.vibeActive = false;
            if (this.vibeActiveIndicator) this.vibeActiveIndicator.classList.remove('active');
            if (this.activeVibeText) this.activeVibeText.textContent = '';
            if (this.activeVibeDisplay) this.activeVibeDisplay.classList.remove('visible');
        }

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
        const shortKey = this.shortenKey(this.state.key);
        if (this.keyDisplay) {
            this.keyDisplay.textContent = shortKey;
        }
        if (this.keyFull) {
            this.keyFull.textContent = this.state.key;
        }
        // Update rotary display if available
        if (this.rotaryKeyDisplay) {
            this.updateKeyDisplay(this.state.key);
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

        // Generating indicator - show when is_generating is true
        if (data.is_generating) {
            const stemCount = this.state.nextStems.length || this.state.currentStems.length;
            this.showGeneratingIndicator(`Generating ${stemCount} stem${stemCount !== 1 ? 's' : ''}...`);
        } else {
            this.hideGeneratingIndicator();
        }

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

        // Loop sync fields — currently playing (authoritative "now audible")
        if (data.currently_playing_loop_index !== undefined) {
            const prevPlaying = this.state.currentlyPlayingLoopIndex;
            this.state.currentlyPlayingLoopIndex = data.currently_playing_loop_index;
            // Auto-jump to current when a new loop becomes audible (if DJ is viewing the previous loop)
            if (data.currently_playing_loop_index > prevPlaying &&
                this.state.currentVisibleLoopIndex < data.currently_playing_loop_index) {
                this.state.currentVisibleLoopIndex = data.currently_playing_loop_index;
            }
        }
        if (data.currently_playing_set_name !== undefined) {
            this.state.currentlyPlayingSetName = data.currently_playing_set_name;
        }
        if (data.currently_playing_reasoning !== undefined) {
            this.state.currentlyPlayingReasoning = data.currently_playing_reasoning;
        }
        if (data.loop_history) {
            this.state.loopHistory = data.loop_history;
        }
        if (data.next_queued_stems) {
            this.state.nextQueuedStems = data.next_queued_stems;
        }
        // Initialize visible loop index if not set
        if (this.state.currentVisibleLoopIndex === 0) {
            this.state.currentVisibleLoopIndex = this.state.currentlyPlayingLoopIndex || 1;
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

    // ========== LOOP NAVIGATION ==========

    jumpToCurrent() {
        this.state.currentVisibleLoopIndex = this.state.currentlyPlayingLoopIndex;
        this.updateLoopSelectorUI();
        this.renderStemDeck();
    }

    navigateToLoop(loopIdx) {
        const maxLoop = this.state.currentlyPlayingLoopIndex + 1; // Can view current + next
        if (loopIdx < 1) return;
        if (loopIdx > maxLoop) return;
        this.state.currentVisibleLoopIndex = loopIdx;
        this.updateLoopSelectorUI();
        this.renderStemDeck();
    }

    updateLoopSelectorUI() {
        if (!this.loopSelector) return;
        const visible = this.state.currentVisibleLoopIndex;
        const audible = this.state.currentlyPlayingLoopIndex;
        const total = audible + 1; // current + next

        if (this.loopNum) this.loopNum.textContent = visible;
        if (this.loopTotal) this.loopTotal.textContent = total;

        // Show "NOW PLAYING" badge if viewing the currently audible loop
        if (this.nowPlayingBadge) {
            this.nowPlayingBadge.style.display = visible === audible ? 'inline' : 'none';
        }
    }

    getDisplayStems() {
        const visible = this.state.currentVisibleLoopIndex;
        const audible = this.state.currentlyPlayingLoopIndex;

        // If viewing the currently playing loop, use loopHistory if available,
        // otherwise fall back to currentStems (handles first loop before history exists)
        if (visible === audible) {
            const historyEntry = this.state.loopHistory.find(h => h.loop_index === visible);
            return {
                stems: historyEntry?.stems || this.state.currentStems || [],
                position: 'current'
            };
        }

        // If viewing the next (queued) loop
        if (visible === audible + 1) {
            return {
                stems: this.state.nextQueuedStems,
                position: 'next'
            };
        }

        // If viewing a history loop
        const historyEntry = this.state.loopHistory.find(h => h.loop_index === visible);
        if (historyEntry) {
            return {
                stems: historyEntry.stems,
                position: 'previous'
            };
        }

        return { stems: [], position: 'current' };
    }

    renderStemDeck() {
        // Update loop selector UI
        this.updateLoopSelectorUI();

        // Current track name — use "now playing" name for accuracy
        this.currentTrackName.textContent = this.state.currentlyPlayingSetName
            || this.state.current_set_name
            || 'Waiting for track...';

        if (!this.stemDeck) return;

        const { stems: displayStems, position } = this.getDisplayStems();
        const mixerData = this.state.stemMixerData || [];

        if (displayStems.length === 0) {
            this.stemDeck.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">
                        <div class="vinyl-record"></div>
                        <div class="vinyl-tonearm"></div>
                    </div>
                    <span class="empty-state-title">No Stems</span>
                    <span class="empty-state-desc">Press play to start generating stems</span>
                    <span class="empty-state-hint">Ready to Create</span>
                </div>
            `;
            return;
        }

        // Build unified list with position tracking
        const allStems = displayStems.map((stem, idx) => {
            const mixer = mixerData.find(m => m.index === idx) || {};
            return {
                ...stem,
                position,
                index: idx,
                volume: mixer.volume ?? 1.0,
                is_muted: mixer.is_muted ?? false,
                is_soloed: mixer.is_soloed ?? false,
                hasMixerControls: position === 'current'
            };
        });

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
        const age = stem._age || 0;

        // Determine family class from major_family or prompt analysis
        let familyClass = 'family-default';
        const majorFamily = stem.major_family || '';
        const majorFamilyLower = majorFamily.toLowerCase();
        if (majorFamilyLower.includes('drum') || majorFamilyLower.includes('percussion')) familyClass = 'family-drums';
        else if (majorFamilyLower.includes('bass')) familyClass = 'family-bass';
        else if (majorFamilyLower.includes('synth') || majorFamilyLower.includes('pad')) familyClass = 'family-synth';
        else if (majorFamilyLower.includes('keys') || majorFamilyLower.includes('piano')) familyClass = 'family-keys';
        else if (majorFamilyLower.includes('vocal') || majorFamilyLower.includes('voice')) familyClass = 'family-vocal';
        else if (majorFamilyLower.includes('texture') || majorFamilyLower.includes('ambient')) familyClass = 'family-texture';

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

        // Age indicator styling - calculate fill percentage (max at 10 loops)
        let ageClass = 'fresh';
        let ageFillPercent = Math.min(age * 10, 100);
        let ageLabel = 'NEW';
        if (age > 3) { ageClass = 'aging'; ageLabel = `${age}`; }
        if (age > 6) { ageClass = 'old'; ageLabel = `${age}`; }

        return `
            <div class="stem-row ${position} ${familyClass}">
                <div class="stem-row-header">
                    <div class="stem-label-group">
                        <span class="stem-position-label">${position.toUpperCase()}</span>
                        <div class="stem-age" title="${age} loops old">
                            <div class="stem-age-bar">
                                <div class="stem-age-fill ${ageClass}" style="width: ${ageFillPercent}%"></div>
                            </div>
                            <span class="stem-age-text">${ageLabel}</span>
                        </div>
                    </div>
                    <div class="stem-header-right">
                        ${timeInfo ? `<span class="stem-time">${timeInfo}</span>` : ''}
                        ${position === 'next' ? `
                        <button class="stem-remove-btn" data-action="remove-next" data-stem-index="${stem.index}" title="Remove from next loop" aria-label="Remove from next loop">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <line x1="18" y1="6" x2="6" y2="18"/>
                                <line x1="6" y1="6" x2="18" y2="18"/>
                            </svg>
                        </button>
                        ` : ''}
                    </div>
                </div>
                <div class="stem-tags">${tags}</div>
                ${hasControls ? `
                <div class="stem-controls">
                    <input type="range" class="stem-vol-slider" min="0" max="2" step="0.05"
                           value="${stem.volume ?? 1.0}"
                           data-stem-index="${stem.index}"
                           aria-label="Stem volume">
                    <div class="stem-btns">
                        <button class="stem-btn ${stem.is_muted ? 'active-mute' : ''}" data-action="mute" data-stem-index="${stem.index}" aria-label="Mute stem" title="Mute">M</button>
                        <button class="stem-btn ${stem.is_soloed ? 'active-solo' : ''}" data-action="solo" data-stem-index="${stem.index}" aria-label="Solo stem" title="Solo">S</button>
                        <button class="stem-btn stem-btn-dl" data-action="download" data-stem-index="${stem.index}" data-stem-set="${position}" aria-label="Download stem" title="Download">↓</button>
                    </div>
                </div>
                ` : ''}
                ${hasDownload && !hasControls ? `
                <div class="stem-controls">
                    <div class="stem-btns">
                        <button class="stem-btn stem-btn-dl" data-action="download" data-stem-index="${stem.index}" data-stem-set="${position}" aria-label="Download stem" title="Download">↓</button>
                    </div>
                </div>
                ` : ''}
            </div>
        `;
    }

    renderInstruments(instruments) {
        this.instrumentCategories.innerHTML = '';
        this.state.instruments = instruments;

        // Category icons mapping
        const categoryIcons = {
            'Electronic & Dance': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
            'Bass & 808s': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
            'Synth & Pads': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10v4M10 10v4M14 10v4M18 10v4"/></svg>',
            'Keys & Piano': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M6 4v16M10 4v16M14 4v16M18 4v16"/></svg>',
            'Strings & Orchestral': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4"/></svg>',
            'Vocals': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/></svg>',
            'Drums': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4"/></svg>',
            'Custom': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/></svg>'
        };

        for (const [category, items] of Object.entries(instruments)) {
            if (category === 'Custom' && items.length === 0) continue;

            const categoryEl = document.createElement('div');
            categoryEl.className = 'instrument-category';

            const icon = categoryIcons[category] || categoryIcons['Custom'];

            const header = document.createElement('div');
            header.className = 'category-header';
            header.innerHTML = `
                <div class="category-icon">${icon}</div>
                <span class="category-name">${category}</span>
                <span class="category-count">${items.length}</span>
            `;

            const togglesGrid = document.createElement('div');
            togglesGrid.className = 'instrument-toggles';

            items.forEach(item => {
                const toggleEl = document.createElement('div');
                toggleEl.className = 'instrument-toggle active';
                toggleEl.innerHTML = `
                    <div class="pad-led"></div>
                    <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
                    </svg>
                    <span class="toggle-label">${item}</span>
                `;
                togglesGrid.appendChild(toggleEl);

                toggleEl.addEventListener('click', () => {
                    const isActive = toggleEl.classList.toggle('active');
                    this.showToast(`${item} ${isActive ? 'enabled' : 'disabled'}`, 'info', 2000);
                    // Auto-collapse category if all instruments disabled
                    if (!isActive) {
                        const anyActive = Array.from(togglesGrid.querySelectorAll('.instrument-toggle.active')).some(el => el !== toggleEl);
                        if (!anyActive) {
                            categoryEl.classList.add('collapsed');
                            header.classList.remove('expanded');
                        }
                    }
                });
            });

            categoryEl.appendChild(header);
            categoryEl.appendChild(togglesGrid);
            this.instrumentCategories.appendChild(categoryEl);

            // Add collapse toggle to category header
            header.addEventListener('click', (e) => {
                const isCollapsed = categoryEl.classList.toggle('collapsed');
                header.classList.toggle('expanded', !isCollapsed);
            });
        }
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
        console.log('[DJ-UI] play() called, isGenerating will be set to true');
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
        console.log('[DJ-UI] pause() called, isGenerating will be set to false');
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

            await fetch('/api/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_generating: true, is_show_started: true }) });
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
            await fetch('/api/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_generating: false, is_show_started: false }) });
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
        const isPlaying = this.state.isPlaying;
        this.playBtn.classList.toggle('playing', isPlaying);
        this.playBtn.classList.toggle('play', !isPlaying);
        this.playBtn.setAttribute('aria-pressed', isPlaying.toString());

        // Update screen reader label
        const label = document.getElementById('play-btn-label');
        if (label) {
            label.textContent = isPlaying ? 'Pause' : 'Play';
        }

        // Update aria-label for better accessibility
        this.playBtn.setAttribute('aria-label', isPlaying ? 'Pause playback' : 'Start playback');
    }

    reconnectStream() {
        this.showToast('Reconnecting stream...');
        this.audioPlayer.src = '/stream.mp3?t=' + Date.now();
        this.audioPlayer.load();

        // Add reconnecting visual state
        if (this.reconnectBtn) {
            this.reconnectBtn.classList.remove('error');
            this.reconnectBtn.classList.add('reconnecting');
        }

        if (this.state.isPlaying) {
            this.audioPlayer.play().then(() => {
                this.reconnectAttempts = 0; // Reset on successful reconnect
                // Reset button state
                if (this.reconnectBtn) {
                    this.reconnectBtn.classList.remove('reconnecting', 'error');
                }
            }).catch(e => {
                console.error('Reconnect play error:', e);
                this.showToast('Reconnect failed', 'error');
                if (this.reconnectBtn) {
                    this.reconnectBtn.classList.remove('reconnecting');
                    this.reconnectBtn.classList.add('error');
                }
            });
        }
    }

    setVolume(value) {
        this.state.volume = value / 100;
        this.audioPlayer.volume = 1.0; // Fixed - volume controlled via gainNode
        if (this.gainNode) this.gainNode.gain.value = this.state.volume;
        this.volumeValue.textContent = `${value}%`;
    }

    // DJ Controls - Unified Apply
    applyAll() {
        const selectedInstruments = [];
        this.instrumentCategories.querySelectorAll('.instrument-toggle.active .toggle-label').forEach(label => {
            selectedInstruments.push(label.textContent.trim());
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
                    this.bpmDisplay.textContent = bpmVal;
                    this.bpmOverride.classList.add('override-active');
                    this.bpmOverride.value = '';
                }
                if (keyVal) {
                    if (this.keyDisplay) this.keyDisplay.textContent = keyVal;
                    if (this.rotaryKeyDisplay) this.updateKeyDisplay(keyVal);
                    this.keyOverride.classList.add('override-active');
                    this.keyOverride.value = '';
                }
                if (vibeVal) {
                    this.state.vibeActive = true;
                    this.vibeActiveIndicator.classList.add('active');
                    this.activeVibeText.textContent = vibeVal;
                    this.activeVibeDisplay.classList.add('visible');
                    this.vibeInput.value = '';
                    if (this.vibeCharCount) this.vibeCharCount.textContent = '0/200';
                }

                let msg = 'Settings applied';
                if (bpmVal) msg += ` (BPM: ${bpmVal})`;
                if (keyVal) msg += ` (Key: ${keyVal})`;
                if (vibeVal) msg += ` - "${vibeVal}"`;
                this.showToast(msg);

                // Show success state on button
                this.applyAllBtn.classList.remove('loading');
                this.applyAllBtn.classList.add('success');
                setTimeout(() => {
                    this.applyAllBtn.classList.remove('success');
                }, 1500);
            })
            .catch((e) => {
                console.error('Apply failed:', e);
                this.showToast('Failed to apply settings', 'error');
            })
            .finally(() => {
                if (!this.applyAllBtn.classList.contains('success')) {
                    this.applyAllBtn.disabled = false;
                    this.applyAllBtn.classList.remove('loading');
                    const btnText = this.applyAllBtn.querySelector('.btn-text');
                    const btnIcon = this.applyAllBtn.querySelector('.btn-icon');
                    if (btnText) btnText.textContent = 'Apply Settings';
                    if (btnIcon) btnIcon.style.display = '';
                }
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

    showToast(message, type = 'success', duration = 4000) {
        if (!this.toastContainer) return;

        const toast = document.createElement('div');
        toast.className = `toast ${type}`;

        const iconSvg = this.getToastIcon(type);

        toast.innerHTML = `
            <div class="toast-icon">${iconSvg}</div>
            <div class="toast-content">
                <div class="toast-title">${this.getToastTitle(type)}</div>
                <div class="toast-message">${message}</div>
            </div>
            <button class="toast-close">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <line x1="18" y1="6" x2="6" y2="18"/>
                    <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
            </button>
        `;

        // Close button handler
        toast.querySelector('.toast-close').addEventListener('click', () => {
            this.dismissToast(toast);
        });

        this.toastContainer.appendChild(toast);

        // Trigger animation
        requestAnimationFrame(() => {
            toast.classList.add('visible');
        });

        // Auto-remove after duration
        if (duration > 0) {
            setTimeout(() => {
                this.dismissToast(toast);
            }, duration);
        }
    }

    getToastIcon(type) {
        const icons = {
            success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>',
            error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
            warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
            info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
        };
        return icons[type] || icons.success;
    }

    getToastTitle(type) {
        const titles = {
            success: 'Success',
            error: 'Error',
            warning: 'Warning',
            info: 'Info'
        };
        return titles[type] || 'Notice';
    }

    dismissToast(toast) {
        if (!toast || toast.classList.contains('toast-exit')) return;
        toast.classList.add('toast-exit');
        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 300);
    }

    // Show/hide generating indicator
    showGeneratingIndicator(label = 'Creating stems...', progress = null) {
        if (!this.generatingIndicator) return;
        this.generatingLabel.textContent = label;
        if (progress !== null && this.generationProgressBar) {
            this.generationProgressBar.style.width = `${progress}%`;
        }
        this.generatingIndicator.classList.add('visible');
    }

    updateGeneratingProgress(progress, label = null) {
        if (!this.generatingIndicator) return;
        if (progress !== null && this.generationProgressBar) {
            this.generationProgressBar.style.width = `${progress}%`;
        }
        if (label && this.generatingLabel) {
            this.generatingLabel.textContent = label;
        }
    }

    hideGeneratingIndicator() {
        if (!this.generatingIndicator) return;
        this.generatingIndicator.classList.remove('visible');
        if (this.generationProgressBar) {
            this.generationProgressBar.style.width = '0%';
        }
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
    }

    async pollState() {
        try {
            const response = await fetch('/api/state');
            if (response.ok) {
                const data = await response.json();
                const prevGenerating = this.state.isPlaying;
                const nowGenerating = data.is_generating || false;

                // Log state changes with timestamps
                if (prevGenerating !== nowGenerating) {
                    console.log(`[DJ-UI] pollState: is_generating changed ${prevGenerating} -> ${nowGenerating} at ${Date.now()}`);
                }

                // Check if anything meaningful changed before updating UI
                const hasChanged =
                    data.current_set_name !== this.state.current_set_name ||
                    data.current_bpm !== this.state.bpm ||
                    data.current_key !== this.state.key ||
                    data.target_bpm_override !== this.state.targetBpmOverride ||
                    data.target_key_override !== this.state.targetKeyOverride ||
                    data.user_override !== this.state.userOverride ||
                    JSON.stringify(data.available_instruments || []) !== JSON.stringify(this.state.availableInstruments || []) ||
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
                    // Mixer data (volumes, mute, solo) is included in /api/state response
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

    // pollVRAM removed as vram and status are managed by worker service.

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

    resizeVisualizer() {
        const container = this.visualizer?.parentElement;
        if (!container) return;
        this.visualizer.width = container.clientWidth;
        this.visualizer.height = container.clientHeight;
    }

    // Initialize segmented VU meter with LED segments
    initVUSegments(vuBar) {
        if (vuBar.dataset.initialized) return;
        vuBar.dataset.initialized = 'true';

        // Clear existing content
        vuBar.innerHTML = '';

        // Create 12 segments (bottom to top: 5 green, 3 amber, 4 red)
        const segmentCounts = { green: 5, amber: 3, red: 4 };
        const colors = ['seg-green', 'seg-amber', 'seg-red'];
        let colorIndex = 0;
        let count = 0;

        for (let i = 0; i < 12; i++) {
            const segment = document.createElement('div');
            segment.className = `vu-segment ${colors[colorIndex]}`;
            vuBar.appendChild(segment);
            count++;
            if (count >= segmentCounts[colors[colorIndex]]) {
                count = 0;
                colorIndex++;
            }
        }

        // Add peak hold indicator
        const peak = document.createElement('div');
        peak.className = 'vu-peak';
        vuBar.appendChild(peak);
    }

    // Update VU meter with segmented LED display
    updateVUMeter(vuBarEl, level) {
        if (!vuBarEl) return;

        // Initialize segments if needed
        this.initVUSegments(vuBarEl);

        const segments = vuBarEl.querySelectorAll('.vu-segment');
        const peak = vuBarEl.querySelector('.vu-peak');
        const activeCount = Math.round((level / 100) * segments.length);

        // Update segments
        segments.forEach((seg, i) => {
            // Segments are added bottom to top, so index 0 is at bottom
            const isActive = i < activeCount;
            seg.classList.toggle('active', isActive);
        });

        // Update peak hold
        if (peak) {
            const peakLevel = vuBarEl.dataset.peakLevel || 0;
            if (level > peakLevel) {
                vuBarEl.dataset.peakLevel = level;
                peak.classList.add('held');
                peak.style.bottom = (3 + (level / 100) * (vuBarEl.clientHeight - 9)) + 'px';

                // Clear peak after delay
                clearTimeout(vuBarEl.dataset.peakTimeout);
                vuBarEl.dataset.peakTimeout = setTimeout(() => {
                    vuBarEl.dataset.peakLevel = 0;
                    peak.classList.remove('held');
                }, 1500);
            }
        }
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
                this.updateVUMeter(this.vuLeftEl, vuLevel);
                this.updateVUMeter(this.vuRightEl, vuLevel + Math.sin(time * 1.5) * 8);
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

        // Update VU meters with segmented display
        if (this.vuLeftEl) this.updateVUMeter(this.vuLeftEl, leftAvg);
        if (this.vuRightEl) this.updateVUMeter(this.vuRightEl, rightAvg);

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
