// MC Clanker - Audience Interface - Analog Warmth Meets Neon Pulse

class AudienceApp {
    constructor() {
        this.audioPlayer = document.getElementById('audio-player');
        this.playBtn = document.getElementById('play-btn');
        this.volumeSlider = document.getElementById('volume-slider');
        this.volumeValue = document.getElementById('volume-value');
        this.volumeFill = document.getElementById('volume-fill');
        this.statusIndicator = document.getElementById('status-indicator');
        this.signalBars = document.getElementById('signal-bars');
        this.trackName = document.getElementById('current-track-name');
        this.visualizer = document.getElementById('visualizer');
        this.ctx = this.visualizer.getContext('2d');
        this.particlesCanvas = document.getElementById('particles-bg');
        this.particlesCtx = this.particlesCanvas ? this.particlesCanvas.getContext('2d') : null;
        this.particles = [];
        if (this.particlesCanvas) this.initParticles();
        this.vuFill = document.getElementById('vu-fill');
        this.vuStatus = document.getElementById('vu-status');
        this.waitingOverlay = document.getElementById('waiting-overlay');
        this.goingLiveOverlay = document.getElementById('going-live-overlay');
        this.waitingText = document.getElementById('waiting-text');
        this.waitingSubtext = document.getElementById('waiting-subtext');
        this.waitingTimer = document.getElementById('waiting-timer');
        this.waitingHint = document.getElementById('waiting-hint');

        // DJ Message Banner
        this.djMessageBanner = document.getElementById('dj-message-banner');
        this.djMessageText = document.getElementById('dj-message-text');
        this.djMessageClose = document.getElementById('dj-message-close');
        this.currentDjMessage = '';
        this.djMessageTimeout = null;

        if (this.djMessageClose) {
            this.djMessageClose.addEventListener('click', () => this.hideDjMessage());
        }

        // Visualizer Mode Selection
        this.vizSelect = document.getElementById('visualizer-mode-select');
        this.currentVizMode = 'pulse';
        if (this.vizSelect) {
            this.vizSelect.addEventListener('change', (e) => {
                this.currentVizMode = e.target.value;
                if (this.currentVizMode === 'starfield') this.initStarfield();
            });
        }

        this.isPlaying = false;
        this.isShowStarted = false;
        this.isTransitioning = false;
        this.waitingRotationInterval = null;
        this.waitingStartTime = null;
        this.waitingTimerInterval = null;
        this.audioContext = null;
        this.analyser = null;
        this.gainNode = null;
        this.source = null;
        this.lastVolume = 80;

        this.init();
    }

    init() {
        // Initialize audio player
        this.audioPlayer.volume = 1.0; // Fixed - volume controlled via gainNode
        // Don't set src here - only request stream when show is playing
        // This prevents browser from connecting to stream before audio is actually available
        this.updateVolumeFill(this.volumeSlider.value);

        // Event listeners
        this.playBtn.addEventListener('click', () => this.togglePlay());
        this.volumeSlider.addEventListener('input', (e) => {
            const vol = e.target.value / 100;
            this.audioPlayer.volume = 1.0; // Always max on element
            if (this.gainNode) this.gainNode.gain.value = vol;
            this.volumeValue.textContent = e.target.value;
            this.updateVolumeFill(e.target.value);
        });

        this.audioPlayer.addEventListener('playing', () => this.setStatus('connected'));
        this.audioPlayer.addEventListener('waiting', () => this.setStatus('connecting'));
        this.audioPlayer.addEventListener('error', () => this.setStatus('disconnected'));
        this.audioPlayer.addEventListener('pause', () => {
            if (this.isPlaying) this.setStatus('paused');
        });

        // Polling - faster for show state changes
        setInterval(() => this.pollState(), 1000);
        window.addEventListener('resize', () => this.resizeCanvas());
        this.resizeCanvas();
        this.animate();

        // Initial state - show waiting overlay
        this.updateShowState(false);
        this.startWaitingSubtextRotation();
    }

    updateVolumeFill(value) {
        this.volumeFill.style.width = `${value}%`;
    }

    togglePlay() {
        if (!this.isShowStarted) return;

        if (this.isPlaying) {
            this.audioPlayer.pause();
            this.isPlaying = false;
            this.playBtn.classList.remove('playing');
            this.setStatus('paused');
        } else {
            // Set up audio chain BEFORE playing to ensure analyser is ready
            this.setupAudio();

            // Resume AudioContext if suspended (Chrome autoplay policy)
            if (this.audioContext) {
                if (this.audioContext.state === 'suspended') {
                    this.audioContext.resume();
                }
                // If analyser was set up with a suspended context, reconnect the audio chain
                // This is the same robust pattern used in the DJ interface
                if (this.source && this.analyser && this.gainNode) {
                    try { this.source.disconnect(); } catch (e) {}
                    try { this.analyser.disconnect(); } catch (e) {}
                    try {
                        this.source.connect(this.analyser);
                        this.analyser.connect(this.gainNode);
                        this.gainNode.connect(this.audioContext.destination);
                    } catch (e) {}
                }
            }

            this.audioPlayer.src = '/stream.mp3?t=' + Date.now();
            this.audioPlayer.play().catch(e => console.error("Play error:", e));
            this.isPlaying = true;
            this.playBtn.classList.add('playing');
        }
    }

    setupAudio() {
        try {
            // Always close existing context to start fresh - avoids state confusion
            if (this.audioContext) {
                this.audioContext.close().catch(() => {});
            }

            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;
            this.analyser.smoothingTimeConstant = 0.8;
            this.gainNode = this.audioContext.createGain();
            this.gainNode.gain.value = this.volumeSlider.value / 100;
            this.source = this.audioContext.createMediaElementSource(this.audioPlayer);

            // Chain: source -> analyser -> gainNode -> destination
            // Analyser processes before gain so visualizer sees full-level audio
            this.source.connect(this.analyser);
            this.analyser.connect(this.gainNode);
            this.gainNode.connect(this.audioContext.destination);

            // Explicitly resume context
            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume();
            }
        } catch (e) {
            console.error("Analyser setup failed:", e);
            if (this.audioContext) {
                this.audioContext.close().catch(() => {});
            }
            this.audioContext = null;
            this.analyser = null;
            this.gainNode = null;
            this.source = null;
        }
    }

    setStatus(status) {
        this.statusIndicator.className = `status-pill ${status}`;
        const labels = {
            'connected': 'LIVE',
            'disconnected': 'OFFLINE',
            'connecting': 'SYNC',
            'paused': 'PAUSED',
            'waiting': 'STANDBY'
        };
        this.statusIndicator.querySelector('.status-label').textContent = labels[status] || status.toUpperCase();

        // Update signal bars
        if (status === 'connected') {
            this.signalBars.classList.add('active');
        } else {
            this.signalBars.classList.remove('active');
        }

        // Update VU status
        if (status === 'connected' && this.isPlaying) {
            this.vuStatus.textContent = 'LIVE';
        } else if (status === 'paused') {
            this.vuStatus.textContent = 'HOLD';
        } else if (status === 'connecting') {
            this.vuStatus.textContent = 'SYNC';
        } else {
            this.vuStatus.textContent = '---';
        }
    }

    async pollState() {
        try {
            const res = await fetch('/api/state');
            if (res.ok) {
                const data = await res.json();
                this.updateTrackInfo(data);
                this.updateShowState(data.is_show_started || false);

                // Check for new DJ message
                if (data.audience_message && data.audience_message_ts) {
                    this.showDjMessage(data.audience_message, data.audience_message_ts);
                } else if (!data.audience_message) {
                    // Message cleared on server
                    this.hideDjMessage();
                }
            }
        } catch (e) {
            console.error("State fetch error:", e);
        }
    }

    showDjMessage(message, timestamp) {
        if (!this.djMessageBanner || !this.djMessageText) return;

        // Check if this specific message was already dismissed locally
        const lastDismissed = sessionStorage.getItem('last_dismissed_msg_ts');
        if (lastDismissed && parseInt(lastDismissed) >= timestamp) {
            return;
        }

        // Clear any existing timeout
        if (this.djMessageTimeout) {
            clearTimeout(this.djMessageTimeout);
        }

        // Set message and show banner
        this.currentDjMessage = message;
        this.djMessageBanner.dataset.timestamp = timestamp;
        this.djMessageText.textContent = message;
        this.djMessageBanner.classList.add('active');

        // Auto-hide after 10 seconds
        this.djMessageTimeout = setTimeout(() => {
            this.hideDjMessage();
        }, 10000);
    }

    hideDjMessage() {
        if (!this.djMessageBanner) return;

        this.djMessageBanner.classList.remove('active');
        this.currentDjMessage = '';

        if (this.djMessageTimeout) {
            clearTimeout(this.djMessageTimeout);
            this.djMessageTimeout = null;
        }

        // Locally mark this message as seen so it doesn't reappear until a NEW message is sent
        const ts = this.djMessageBanner.dataset.timestamp;
        if (ts) {
            sessionStorage.setItem('last_dismissed_msg_ts', ts);
        }
    }

    updateShowState(isShowStarted) {
        const wasStarted = this.isShowStarted;
        this.isShowStarted = isShowStarted;

        if (!isShowStarted) {
            // Show ended - trigger graceful ending transition
            this.stopWaitingTimer();

            if (wasStarted && this.isPlaying) {
                // Show was running - play ending animation first
                this.triggerGoingOffline().then(() => {
                    this.waitingOverlay.classList.add('active');
                    this.playBtn.classList.add('disabled');
                    if (this.waitingText) this.waitingText.textContent = 'STANDBY MODE';
                    if (this.waitingSubtext) this.waitingSubtext.textContent = 'DJ is warming up the decks';
                    if (this.waitingHint) this.waitingHint.textContent = 'Tune in soon for an AI-generated experience';
                    this.startWaitingTimer();
                    // Reset so next show start will trigger animation again
                    this.wasShowStarted = false;
                });
            } else {
                // Initial state or already stopped
                this.waitingOverlay.classList.add('active');
                this.playBtn.classList.add('disabled');
                if (this.waitingText) this.waitingText.textContent = 'STANDBY MODE';
                if (this.waitingSubtext) this.waitingSubtext.textContent = 'DJ is warming up the decks';
                if (this.waitingHint) this.waitingHint.textContent = 'Tune in soon for an AI-generated experience';
                this.startWaitingTimer();
                this.wasShowStarted = false;
            }

            // Pause and reset audio
            if (this.isPlaying) {
                this.audioPlayer.pause();
                this.isPlaying = false;
                this.playBtn.classList.remove('playing');
            }
            this.setStatus('waiting');
        } else {
            // Show started!
            this.stopWaitingTimer();

            if (!wasStarted) {
                // Transitioning from waiting to live - trigger going live animation
                this.triggerGoingLive();
            } else {
                // Already was started (e.g., reconnection) - just remove overlay
                this.waitingOverlay.classList.remove('active');
                this.playBtn.classList.remove('disabled');
            }
        }
    }

    triggerGoingLive() {
        this.isTransitioning = true;

        // Stop the waiting subtext rotation
        if (this.waitingRotationInterval) {
            clearInterval(this.waitingRotationInterval);
            this.waitingRotationInterval = null;
        }

        // Stop the waiting timer
        this.stopWaitingTimer();

        // Hide waiting overlay immediately
        this.waitingOverlay.classList.remove('active');

        // Show going live overlay with animation
        if (this.goingLiveOverlay) {
            this.goingLiveOverlay.classList.add('active');
        }

        // Countdown sequence
        const countdownEl = document.getElementById('going-live-countdown');
        const text2El = this.goingLiveOverlay?.querySelector('.going-live-text-2');
        let count = 3;

        // Initial state
        if (countdownEl) {
            countdownEl.textContent = count;
        }
        if (text2El) {
            text2El.textContent = 'IN 3';
        }

        const countdownInterval = setInterval(() => {
            if (count > 1) {
                count--;
                if (countdownEl) {
                    countdownEl.textContent = count;
                    // Re-trigger animation
                    countdownEl.style.animation = 'none';
                    countdownEl.offsetHeight; // Trigger reflow
                    countdownEl.style.animation = 'countdownPop 1s ease-out forwards';
                }
                if (text2El) {
                    text2El.textContent = `IN ${count}`;
                    text2El.style.animation = 'none';
                    text2El.offsetHeight;
                    text2El.style.animation = 'goingLiveTextSlide 0.6s ease-out forwards';
                }
            } else {
                clearInterval(countdownInterval);
            }
        }, 800);

        // After animation completes, remove overlays and start playing
        setTimeout(() => {
            if (this.goingLiveOverlay) {
                this.goingLiveOverlay.classList.remove('active');
            }

            // Set audio source BEFORE setupAudio - createMediaElementSource needs the element to have a src
            this.audioPlayer.src = '/stream.mp3?t=' + Date.now();

            // Set up audio chain to ensure analyser is ready
            this.setupAudio();

            // Resume AudioContext if suspended (Chrome autoplay policy)
            if (this.audioContext && this.audioContext.state === 'suspended') {
                this.audioContext.resume();
            }

            // Auto-start playing when show begins
            this.audioPlayer.play().catch(e => console.error("Auto-play error:", e));
            this.isPlaying = true;
            this.playBtn.classList.remove('disabled');
            this.playBtn.classList.add('playing');
            this.setStatus('connected');

            this.isTransitioning = false;
            this.wasShowStarted = true;
        }, 2500);
    }

    triggerGoingOffline() {
        return new Promise((resolve) => {
            this.isTransitioning = true;

            // Add signing-off class for visual effect
            this.waitingOverlay.classList.add('signing-off');

            // Update UI to show ending state
            if (this.waitingText) this.waitingText.textContent = 'SIGNING OFF';

            // Rotating subtext during sign-off
            const subtexts = ['Broadcast ending...', 'Wrapping up...', 'Final transmission...', 'Thank you for listening...'];
            let subtextIndex = 0;
            if (this.waitingSubtext) this.waitingSubtext.textContent = subtexts[subtextIndex];
            const subtextInterval = setInterval(() => {
                subtextIndex = (subtextIndex + 1) % subtexts.length;
                if (this.waitingSubtext) this.waitingSubtext.textContent = subtexts[subtextIndex];
            }, 600);

            // Fade out the visualizer
            const vizWrapper = document.querySelector('.visualizer-wrapper');
            if (vizWrapper) vizWrapper.classList.add('ending');

            // After transition, clean up and resolve
            setTimeout(() => {
                clearInterval(subtextInterval);
                this.waitingOverlay.classList.remove('signing-off');
                if (vizWrapper) vizWrapper.classList.remove('ending');
                this.isTransitioning = false;
                resolve();
            }, 2000);
        });
    }

    startWaitingSubtextRotation() {
        // Rotating subtexts for waiting state - more themed to DJ broadcast
        const waitingSubtexts = [
            'DJ is warming up the decks',
            'Tuning the frequency',
            'Calibrating oscillators',
            'Syncing waveforms',
            'Loading audio buffer',
            'Preparing the mix',
            'Warming up tubes',
            'Setting up the vibe'
        ];
        let index = 0;
        this.waitingRotationInterval = setInterval(() => {
            if (this.waitingSubtext && !this.isShowStarted && !this.isTransitioning) {
                index = (index + 1) % waitingSubtexts.length;
                this.waitingSubtext.textContent = waitingSubtexts[index];
            }
        }, 3500);
    }

    startWaitingTimer() {
        this.waitingStartTime = Date.now();

        // Show the timer element
        if (this.waitingTimer) {
            this.waitingTimer.classList.add('active');
        }

        // Update timer display
        this.waitingTimerInterval = setInterval(() => {
            if (!this.waitingStartTime) return;

            const elapsed = Math.floor((Date.now() - this.waitingStartTime) / 1000);
            const mins = Math.floor(elapsed / 60).toString().padStart(2, '0');
            const secs = (elapsed % 60).toString().padStart(2, '0');

            const timerValue = this.waitingTimer?.querySelector('.timer-value');
            if (timerValue) {
                timerValue.textContent = `${mins}:${secs}`;
            }
        }, 1000);
    }

    stopWaitingTimer() {
        if (this.waitingTimerInterval) {
            clearInterval(this.waitingTimerInterval);
            this.waitingTimerInterval = null;
        }
        if (this.waitingTimer) {
            this.waitingTimer.classList.remove('active');
        }
        this.waitingStartTime = null;
    }

    updateTrackInfo(data) {
        // Use currently_playing_set_name for accurate "what's actually playing"
        this.trackName.textContent = data.currently_playing_set_name || data.current_set_name || 'Waiting for stream...';

        const bpmBadge = document.getElementById('bpm-badge');
        const keyBadge = document.getElementById('key-badge');
        if (bpmBadge && data.current_bpm) {
            bpmBadge.querySelector('.badge-value').textContent = data.current_bpm;
        }
        if (keyBadge && data.current_key) {
            keyBadge.querySelector('.badge-value').textContent = data.current_key;
        }
    }

    resizeCanvas() {
        const parent = this.visualizer.parentElement;
        this.visualizer.width = parent.clientWidth;
        this.visualizer.height = parent.clientHeight;
        
        if (this.particlesCanvas) {
            this.particlesCanvas.width = window.innerWidth;
            this.particlesCanvas.height = window.innerHeight;
            this.initParticles(); // Reinit on resize
        }
    }

    initParticles() {
        if (!this.particlesCanvas) return;
        this.particles = [];
        const numParticles = 80;
        for (let i = 0; i < numParticles; i++) {
            this.particles.push({
                x: Math.random() * this.particlesCanvas.width,
                y: Math.random() * this.particlesCanvas.height,
                size: Math.random() * 2 + 1,
                speedX: (Math.random() - 0.5) * 0.5,
                speedY: (Math.random() - 0.5) * 0.5,
                hue: Math.random() > 0.5 ? 190 : 35, // Mix of cyan and amber
                baseAlpha: Math.random() * 0.5 + 0.1
            });
        }
    }

    drawParticles(bassEnergy) {
        if (!this.particlesCtx || !this.particlesCanvas) return;
        
        const w = this.particlesCanvas.width;
        const h = this.particlesCanvas.height;
        
        this.particlesCtx.clearRect(0, 0, w, h);
        
        // Only draw normal particles if we are not in starfield mode
        if (this.currentVizMode === 'starfield') return;
        
        // Boost particle speed and size based on bass energy
        const speedBoost = 1 + (bassEnergy * 3);
        const sizeBoost = 1 + (bassEnergy * 2);
        
        this.particles.forEach(p => {
            p.x += p.speedX * speedBoost;
            p.y += p.speedY * speedBoost;
            
            // Wrap around
            if (p.x < 0) p.x = w;
            if (p.x > w) p.x = 0;
            if (p.y < 0) p.y = h;
            if (p.y > h) p.y = 0;
            
            const pulseAlpha = Math.min(1, p.baseAlpha + (bassEnergy * 0.5));
            
            this.particlesCtx.beginPath();
            this.particlesCtx.arc(p.x, p.y, p.size * sizeBoost, 0, Math.PI * 2);
            this.particlesCtx.fillStyle = `hsla(${p.hue}, 100%, 60%, ${pulseAlpha})`;
            this.particlesCtx.shadowBlur = p.size * 3 * sizeBoost;
            this.particlesCtx.shadowColor = `hsla(${p.hue}, 100%, 50%, 0.8)`;
            this.particlesCtx.fill();
        });
    }

    initStarfield() {
        if (!this.particlesCanvas) return;
        this.stars = [];
        const numStars = 200;
        for (let i = 0; i < numStars; i++) {
            this.stars.push({
                x: Math.random() * 2000 - 1000,
                y: Math.random() * 2000 - 1000,
                z: Math.random() * 2000
            });
        }
    }

    animate() {
        requestAnimationFrame(() => this.animate());

        const width = this.visualizer.width;
        const height = this.visualizer.height;
        const cx = width / 2;
        const cy = height / 2;

        // Clear with fade trail effect
        this.ctx.fillStyle = 'rgba(5, 5, 8, 0.15)';
        this.ctx.fillRect(0, 0, width, height);

        if (!this.analyser || !this.isPlaying) {
            // Ambient mode router for when audio isn't playing
            if (this.currentVizMode === 'dual_spectrum') {
                const emptyData = new Uint8Array(64);
                this.drawDualSpectrum(emptyData, width, height, 0);
            } else if (this.currentVizMode === 'mirror_wave') {
                const emptyTimeData = new Uint8Array(128);
                emptyTimeData.fill(128);
                this.drawMirrorWave(emptyTimeData, cx, cy, width, height, 0);
            } else if (this.currentVizMode === 'inferno') {
                const emptyData = new Uint8Array(64);
                this.drawInferno(emptyData, width, height, 0);
            } else if (this.currentVizMode === 'pixel_grid') {
                const emptyData = new Uint8Array(64);
                this.drawPixelGrid(emptyData, width, height, 0);
            } else if (this.currentVizMode === 'starfield') {
                const emptyData = new Uint8Array(64);
                this.drawStarfieldVisualizer(emptyData, cx, cy, width, height, 0);
            } else if (this.currentVizMode === 'osc') {
                const emptyTimeData = new Uint8Array(128);
                emptyTimeData.fill(128); // 128 is the center flatline
                this.drawOscilloscope(emptyTimeData, cx, cy, width, height, 0);
            } else if (this.currentVizMode === 'bars') {
                const emptyData = new Uint8Array(64);
                this.drawRetroBars(emptyData, width, height, 0);
            } else {
                this.drawAmbientRadial(cx, cy, Math.min(width, height) / 2);
            }
            return;
        }

        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        this.analyser.getByteFrequencyData(dataArray);

        // Calculate bass energy (first ~5 bins for sub/kick)
        let bassSum = 0;
        for (let i = 0; i < 5; i++) {
            bassSum += dataArray[i];
        }
        const currentBass = (bassSum / 5) / 255;

        // Smooth the bass energy so it pulses to the beat instead of jittering
        this.lastBass = (this.lastBass && !isNaN(this.lastBass)) ? this.lastBass : 0;
        this.lastBass = this.lastBass * 0.85 + currentBass * 0.15;
        let bassEnergy = this.lastBass;

        // Guard against NaN or invalid bassEnergy
        if (isNaN(bassEnergy) || bassEnergy < 0) {
            bassEnergy = 0;
        }
        
        // Draw particles with smoothed bass energy
        this.drawParticles(bassEnergy);

        const timeData = new Uint8Array(this.analyser.frequencyBinCount);
        this.analyser.getByteTimeDomainData(timeData);

        // Visualizer Mode Router
        switch(this.currentVizMode) {
            case 'pulse':
                this.drawNeonPulse(dataArray, cx, cy, width, height, bassEnergy);
                break;
            case 'osc':
                this.drawOscilloscope(timeData, cx, cy, width, height, bassEnergy);
                break;
            case 'bars':
                this.drawRetroBars(dataArray, width, height, bassEnergy);
                break;
            case 'starfield':
                this.drawStarfieldVisualizer(dataArray, cx, cy, width, height, bassEnergy);
                break;
            case 'dual_spectrum':
                this.drawDualSpectrum(dataArray, width, height, bassEnergy);
                break;
            case 'mirror_wave':
                this.drawMirrorWave(timeData, cx, cy, width, height, bassEnergy);
                break;
            case 'inferno':
                this.drawInferno(dataArray, width, height, bassEnergy);
                break;
            case 'pixel_grid':
                this.drawPixelGrid(dataArray, width, height, bassEnergy);
                break;
            default:
                this.drawNeonPulse(dataArray, cx, cy, width, height, bassEnergy);
        }

        // Update VU meter (remains consistent across modes)
        const avgValue = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        const vuPercent = avgValue / 255;
        const circumference = 400;
        const offset = circumference - (vuPercent * circumference * 0.75);
        if (this.vuFill) this.vuFill.style.strokeDashoffset = offset;

        // Update VU label based on level
        if (this.vuStatus) {
            if (vuPercent > 0.85) {
                this.vuStatus.textContent = 'HOT';
            } else if (vuPercent > 0.5) {
                this.vuStatus.textContent = 'OK';
            } else if (vuPercent > 0.05) {
                this.vuStatus.textContent = 'LOW';
            }
        }

        this.ctx.shadowBlur = 0;
    }

    drawNeonPulse(dataArray, cx, cy, width, height, bassEnergy) {
        // Clear the canvas to prevent state bleed from previous visualizers
        this.ctx.clearRect(0, 0, width, height);

        const centerRadius = 60 + (bassEnergy * 15);
        const maxRadius = Math.min(width, height) / 2 - 40;
        const barCount = 128; // Increased density for pulse

        for (let i = 0; i < barCount; i++) {
            const dataIndex = Math.floor(i * dataArray.length / barCount);
            const value = dataArray[dataIndex];
            const percent = value / 255;

            const angle = (i / barCount) * Math.PI * 2 - Math.PI / 2;
            const barLength = (maxRadius - centerRadius) * Math.pow(percent, 1.2);

            const x1 = cx + Math.cos(angle) * centerRadius;
            const y1 = cy + Math.sin(angle) * centerRadius;
            const x2 = cx + Math.cos(angle) * (centerRadius + barLength);
            const y2 = cy + Math.sin(angle) * (centerRadius + barLength);

            const hue = i < barCount/2 ? 35 + (i / (barCount/2)) * 30 : 190;
            const lightness = 50 + percent * 30;
            const alpha = 0.5 + percent * 0.5;

            this.ctx.strokeStyle = `hsla(${hue}, 100%, ${lightness}%, ${alpha})`;
            this.ctx.lineWidth = 3 + (percent * 3);
            this.ctx.lineCap = 'round';

            this.ctx.shadowBlur = 15 + (percent * 15);
            this.ctx.shadowColor = `hsla(${hue}, 100%, 50%, 0.8)`;

            this.ctx.beginPath();
            this.ctx.moveTo(x1, y1);
            this.ctx.lineTo(x2, y2);
            this.ctx.stroke();
        }
    }

    drawOscilloscope(timeData, cx, cy, width, height, bassEnergy) {
        // Clear the canvas to prevent state bleed from previous visualizers
        this.ctx.clearRect(0, 0, width, height);

        this.ctx.lineWidth = 4 + (bassEnergy * 4);
        this.ctx.lineJoin = 'round';
        this.ctx.lineCap = 'round';
        this.ctx.strokeStyle = `hsla(190, 100%, 60%, ${0.8 + bassEnergy * 0.2})`;
        this.ctx.shadowBlur = 20 + (bassEnergy * 20);
        this.ctx.shadowColor = 'rgba(0, 212, 255, 0.8)';

        this.ctx.beginPath();
        const sliceWidth = width * 1.0 / timeData.length;
        let x = 0;

        for (let i = 0; i < timeData.length; i++) {
            const v = timeData[i] / 128.0;
            const y = v * cy;

            if (i === 0) {
                this.ctx.moveTo(x, y);
            } else {
                this.ctx.lineTo(x, y);
            }
            x += sliceWidth;
        }

        this.ctx.lineTo(width, cy);
        this.ctx.stroke();
        
        // Add a secondary faint echo line
        this.ctx.lineWidth = 2;
        this.ctx.strokeStyle = `hsla(35, 100%, 60%, ${0.4 + bassEnergy * 0.4})`;
        this.ctx.shadowColor = 'rgba(255, 149, 0, 0.5)';
        this.ctx.beginPath();
        x = 0;
        for (let i = 0; i < timeData.length; i++) {
            const v = timeData[timeData.length - 1 - i] / 128.0; // Reversed
            const y = v * cy;
            if (i === 0) this.ctx.moveTo(x, y);
            else this.ctx.lineTo(x, y);
            x += sliceWidth;
        }
        this.ctx.stroke();
    }

    drawRetroBars(dataArray, width, height, bassEnergy) {
        // Clear the canvas to prevent state bleed from previous visualizers
        this.ctx.clearRect(0, 0, width, height);

        const barWidth = 12;
        const gap = 4;
        const numBars = Math.floor(width / (barWidth + gap));
        
        const startX = (width - (numBars * (barWidth + gap))) / 2;
        const baseY = height - 40;
        
        for (let i = 0; i < numBars; i++) {
            const dataIndex = Math.floor(i * dataArray.length / numBars);
            const value = dataArray[dataIndex];
            const percent = value / 255;
            
            const maxBarHeight = height * 0.7;
            const barHeight = Math.max(4, maxBarHeight * percent);
            
            const x = startX + i * (barWidth + gap);
            const y = baseY - barHeight;
            
            // Draw digital segments
            const numSegments = Math.floor(barHeight / 8);
            for(let s = 0; s < numSegments; s++) {
                const segPercent = s / (maxBarHeight / 8);
                const hue = 120 - (segPercent * 120); // Green to Yellow to Red
                
                const segY = baseY - (s * 8) - 6;
                
                this.ctx.fillStyle = `hsla(${hue}, 100%, 50%, 0.8)`;
                this.ctx.shadowBlur = 5;
                this.ctx.shadowColor = `hsla(${hue}, 100%, 50%, 0.5)`;
                this.ctx.fillRect(x, segY, barWidth, 6);
            }
        }
    }

    drawStarfieldVisualizer(dataArray, cx, cy, width, height, bassEnergy) {
        if (!this.stars) this.initStarfield();

        // Always clear the main canvas first, even if particlesCtx is missing
        this.ctx.clearRect(0, 0, width, height);

        if (!this.particlesCtx) return;

        const w = this.particlesCanvas.width;
        const h = this.particlesCanvas.height;

        this.particlesCtx.clearRect(0, 0, w, h);

        // Draw deep space frequency rings on main canvas
        this.ctx.shadowBlur = 30;
        const ringCount = 8;
        for(let r=0; r<ringCount; r++) {
            const val = dataArray[r * 4] / 255;
            this.ctx.beginPath();
            this.ctx.arc(cx, cy, 40 + (r * 30 * (1 + bassEnergy)), 0, Math.PI * 2);
            this.ctx.strokeStyle = `rgba(0, ${150 + (val*105)}, 255, ${0.1 + val*0.2})`;
            this.ctx.lineWidth = 2 + (val * 5);
            this.ctx.stroke();
        }

        // Move and draw stars on particle canvas corresponding to music intensity
        const speed = 4 + (bassEnergy * 20);
        
        this.particlesCtx.fillStyle = 'white';
        this.stars.forEach(star => {
            star.z -= speed;
            if (star.z <= 0) {
                star.x = Math.random() * 2000 - 1000;
                star.y = Math.random() * 2000 - 1000;
                star.z = 2000;
            }
            
            const sx = (star.x / star.z) * w + (w / 2);
            const sy = (star.y / star.z) * h + (h / 2);
            
            const size = (1 - star.z / 2000) * 3;
            const alpha = 1 - (star.z / 2000);
            
            // Color stars based on their quadrant
            let hue = 190;
            if (sx > w/2 && sy > h/2) hue = 35; // Amber
            if (sx < w/2 && sy < h/2) hue = 280; // Purple
            
            this.particlesCtx.beginPath();
            this.particlesCtx.arc(sx, sy, size, 0, Math.PI * 2);
            this.particlesCtx.fillStyle = `hsla(${hue}, 100%, 80%, ${alpha})`;
            this.particlesCtx.shadowBlur = size * 4;
            this.particlesCtx.shadowColor = `hsla(${hue}, 100%, 60%, ${alpha})`;
            this.particlesCtx.fill();
        });
    }

    // Dual Spectrum - L/R channel bar graph with peak indicators
    drawDualSpectrum(dataArray, width, height, bassEnergy) {
        // Clear the canvas to prevent state bleed from previous visualizers
        this.ctx.clearRect(0, 0, width, height);

        const numBars = 32;
        const barWidth = Math.floor(width / (numBars * 2 + 1));
        const gap = barWidth;
        const maxHeight = height - 20;

        // Initialize peak holders - always reset to correct size in case canvas was resized
        if (!this.leftPeaks || this.leftPeaks.length !== numBars) {
            this.leftPeaks = new Array(numBars).fill(0);
        }
        if (!this.rightPeaks || this.rightPeaks.length !== numBars) {
            this.rightPeaks = new Array(numBars).fill(0);
        }
        if (!this.peakDecay || this.peakDecay.length !== numBars) {
            this.peakDecay = new Array(numBars).fill(0);
        }

        // Left channel (lower half of spectrum)
        for (let i = 0; i < numBars; i++) {
            const dataIndex = Math.floor(i * dataArray.length / (numBars * 2));
            const value = dataArray[dataIndex];
            const percent = value / 255;
            const barHeight = Math.max(4, maxHeight * 0.5 * percent);

            const x = gap + i * (barWidth + gap);
            const y = height / 2 - barHeight;

            // Color gradient from green to yellow to red
            const hue = 120 - (percent * 120);
            this.ctx.fillStyle = `hsl(${hue}, 100%, 50%)`;
            this.ctx.shadowBlur = 8;
            this.ctx.shadowColor = `hsl(${hue}, 100%, 50%)`;
            this.ctx.fillRect(x, y, barWidth, barHeight);

            // Peak indicator
            if (barHeight > this.leftPeaks[i]) {
                this.leftPeaks[i] = barHeight;
                this.peakDecay[i] = 0;
            }
            if (this.leftPeaks[i] > 0) {
                this.peakDecay[i] += 0.5;
                if (this.peakDecay[i] > 30) {
                    this.leftPeaks[i] -= 2;
                }
            }
            this.ctx.fillStyle = `hsl(${hue}, 100%, 70%)`;
            this.ctx.shadowBlur = 4;
            this.ctx.fillRect(x, height / 2 - this.leftPeaks[i] - 3, barWidth, 3);
        }

        // Right channel (upper half of spectrum, mirrored)
        for (let i = 0; i < numBars; i++) {
            const dataIndex = Math.floor((i + numBars) * dataArray.length / (numBars * 2));
            const value = dataArray[dataIndex];
            const percent = value / 255;
            const barHeight = Math.max(4, maxHeight * 0.5 * percent);

            const x = gap + i * (barWidth + gap);
            const y = height / 2;

            const hue = 120 - (percent * 120);
            this.ctx.fillStyle = `hsl(${hue}, 100%, 50%)`;
            this.ctx.shadowBlur = 8;
            this.ctx.shadowColor = `hsl(${hue}, 100%, 50%)`;
            this.ctx.fillRect(x, y, barWidth, barHeight);

            // Peak indicator
            if (barHeight > this.rightPeaks[i]) {
                this.rightPeaks[i] = barHeight;
                this.peakDecay[i + numBars] = 0;
            }
            if (this.rightPeaks[i] > 0) {
                this.peakDecay[i + numBars] += 0.5;
                if (this.peakDecay[i + numBars] > 30) {
                    this.rightPeaks[i] -= 2;
                }
            }
            this.ctx.fillStyle = `hsl(${hue}, 100%, 70%)`;
            this.ctx.shadowBlur = 4;
            this.ctx.fillRect(x, height / 2 + this.rightPeaks[i], barWidth, 3);
        }

        // Center line
        this.ctx.strokeStyle = 'rgba(255,255,255,0.2)';
        this.ctx.lineWidth = 1;
        this.ctx.beginPath();
        this.ctx.moveTo(0, height / 2);
        this.ctx.lineTo(width, height / 2);
        this.ctx.stroke();
    }

    // Mirror Wave - mirrored oscilloscope with glow
    drawMirrorWave(timeData, cx, cy, width, height, bassEnergy) {
        // Clear the canvas to prevent state bleed from previous visualizers
        this.ctx.clearRect(0, 0, width, height);

        // Top half - forward
        this.ctx.lineWidth = 2;
        this.ctx.lineJoin = 'round';
        this.ctx.lineCap = 'round';

        // Glow layers
        const layers = [
            { blur: 30, alpha: 0.15, width: 12, color: '0, 255, 136' },
            { blur: 20, alpha: 0.2, width: 8, color: '0, 255, 136' },
            { blur: 10, alpha: 0.3, width: 4, color: '0, 255, 200' },
            { blur: 0, alpha: 0.9, width: 2, color: '255, 255, 255' }
        ];

        const midY = height * 0.3;

        layers.forEach(layer => {
            this.ctx.shadowBlur = layer.blur;
            this.ctx.shadowColor = `rgba(${layer.color}, ${layer.alpha})`;
            this.ctx.strokeStyle = `rgba(${layer.color}, ${layer.alpha})`;
            this.ctx.lineWidth = layer.width;

            this.ctx.beginPath();
            const sliceWidth = width / timeData.length;
            let x = 0;

            for (let i = 0; i < timeData.length; i++) {
                const v = timeData[i] / 128.0;
                const y = midY + (v - 1) * midY * 0.8;

                if (i === 0) this.ctx.moveTo(x, y);
                else this.ctx.lineTo(x, y);
                x += sliceWidth;
            }
            this.ctx.stroke();
        });

        // Bottom half - mirrored
        const midY2 = height * 0.7;

        layers.forEach(layer => {
            this.ctx.shadowBlur = layer.blur;
            this.ctx.shadowColor = `rgba(${layer.color}, ${layer.alpha})`;
            this.ctx.strokeStyle = `rgba(${layer.color}, ${layer.alpha})`;
            this.ctx.lineWidth = layer.width;

            this.ctx.beginPath();
            const sliceWidth = width / timeData.length;
            let x = 0;

            for (let i = 0; i < timeData.length; i++) {
                const v = timeData[i] / 128.0;
                const y = midY2 + (1 - v) * midY * 0.8;

                if (i === 0) this.ctx.moveTo(x, y);
                else this.ctx.lineTo(x, y);
                x += sliceWidth;
            }
            this.ctx.stroke();
        });

        // Baseline indicators
        this.ctx.strokeStyle = 'rgba(0, 255, 136, 0.1)';
        this.ctx.lineWidth = 1;
        this.ctx.setLineDash([5, 5]);
        this.ctx.beginPath();
        this.ctx.moveTo(0, midY);
        this.ctx.lineTo(width, midY);
        this.ctx.moveTo(0, midY2);
        this.ctx.lineTo(width, midY2);
        this.ctx.stroke();
        this.ctx.setLineDash([]);
    }

    // Inferno - flame-like visualization rising from bottom
    drawInferno(dataArray, width, height, bassEnergy) {
        // Clear the canvas to prevent state bleed from previous visualizers
        this.ctx.clearRect(0, 0, width, height);

        // Seed the fire array if needed
        this.fireSeed = this.fireSeed || (() => {
            const arr = [];
            for (let i = 0; i < 64; i++) arr.push(Math.random());
            return arr;
        })();

        // Propagate fire upward
        const cols = 64;
        const rows = 32;
        // Always reset fire grid to correct size in case canvas was resized
        if (!this.fireGrid || this.fireGrid.length !== cols || (this.fireGrid[0] && this.fireGrid[0].length !== rows)) {
            this.fireGrid = new Array(cols).fill(0).map(() => new Array(rows).fill(0));
        }

        // Map frequency data to fire intensity at base
        for (let i = 0; i < cols; i++) {
            const dataIndex = Math.floor(i * dataArray.length / cols);
            const intensity = dataArray[dataIndex] / 255;
            this.fireGrid[i][0] = Math.floor(intensity * 28) + Math.floor(bassEnergy * 10);
        }

        // Spread fire upward
        for (let y = 1; y < rows; y++) {
            for (let x = 0; x < cols; x++) {
                const left = this.fireGrid[(x - 1 + cols) % cols][y - 1];
                const center = this.fireGrid[x][y - 1];
                const right = this.fireGrid[(x + 1) % cols][y - 1];
                const below = this.fireGrid[x][y - 1];

                let avg = (left + center + right + below) / 4;
                avg -= 0.1 + Math.random() * 0.2;
                avg = Math.max(0, Math.min(30, avg));
                this.fireGrid[x][y] = avg;
            }
        }

        // Draw fire as pixels
        const pixelW = width / cols;
        const pixelH = height / rows;

        for (let y = 0; y < rows; y++) {
            for (let x = 0; x < cols; x++) {
                const intensity = this.fireGrid[x][y];
                if (intensity < 1) continue;

                const t = intensity / 30;
                // Fire gradient: black -> red -> orange -> yellow -> white
                let r, g, b;
                if (t < 0.25) {
                    r = 255; g = 0; b = 0;
                } else if (t < 0.5) {
                    r = 255; g = Math.floor((t - 0.25) * 4 * 165); b = 0;
                } else if (t < 0.75) {
                    r = 255; g = 165 + Math.floor((t - 0.5) * 4 * 90); b = Math.floor((t - 0.5) * 4 * 50);
                } else {
                    r = 255; g = 255; b = Math.floor((t - 0.75) * 4 * 200);
                }

                this.ctx.fillStyle = `rgb(${r},${g},${b})`;
                this.ctx.shadowBlur = pixelW * 0.5;
                this.ctx.shadowColor = `rgb(${r},${g},${b})`;
                this.ctx.fillRect(x * pixelW, height - (y + 1) * pixelH, pixelW + 1, pixelH + 1);
            }
        }
    }

    // Pixel Grid - blocky waveform grid
    drawPixelGrid(dataArray, width, height, bassEnergy) {
        // Clear the canvas to prevent state bleed from previous visualizers
        this.ctx.clearRect(0, 0, width, height);

        const gridX = 24;
        const gridY = 16;
        const cellW = width / gridX;
        const cellH = height / gridY;
        const time = Date.now() * 0.002;

        // Map frequency data to grid
        for (let y = 0; y < gridY; y++) {
            for (let x = 0; x < gridX; x++) {
                const freqIndex = Math.floor((x / gridX) * dataArray.length);
                const freqValue = dataArray[freqIndex] / 255;

                // Create wave pattern based on time and position
                const wave = Math.sin(time + x * 0.3 + y * 0.2) * 0.5 + 0.5;
                const threshold = freqValue * 0.7 + wave * 0.3;
                const isActive = freqValue > (1 - y / gridY) * 0.3;

                if (isActive) {
                    const hue = 180 + (x / gridX) * 60; // Cyan to green
                    const lightness = 30 + freqValue * 50;
                    const alpha = 0.5 + freqValue * 0.5;

                    this.ctx.fillStyle = `hsla(${hue}, 100%, ${lightness}%, ${alpha})`;
                    this.ctx.shadowBlur = 10;
                    this.ctx.shadowColor = `hsla(${hue}, 100%, 50%, 0.8)`;
                    this.ctx.fillRect(x * cellW + 1, y * cellH + 1, cellW - 2, cellH - 2);

                    // Inner glow for active cells
                    if (freqValue > 0.5) {
                        this.ctx.fillStyle = `hsla(${hue}, 100%, 80%, 0.3)`;
                        this.ctx.fillRect(x * cellW + cellW * 0.2, y * cellH + cellH * 0.2, cellW * 0.6, cellH * 0.6);
                    }
                } else {
                    // Dim inactive cells
                    this.ctx.fillStyle = 'rgba(20, 30, 40, 0.3)';
                    this.ctx.shadowBlur = 0;
                    this.ctx.fillRect(x * cellW + 1, y * cellH + 1, cellW - 2, cellH - 2);
                }
            }
        }

    }

    drawAmbientRadial(cx, cy, radius) {
        const time = Date.now() * 0.001;
        const barCount = 48;
        
        // Call drawParticles with 0 bass energy
        this.drawParticles(0);

        for (let i = 0; i < barCount; i++) {
            const angle = (i / barCount) * Math.PI * 2 - Math.PI / 2;
            const phase = Math.sin(time * 0.5 + i * 0.2); // Slower, more organic
            const barLength = 10 + phase * 12;

            const x1 = cx + Math.cos(angle) * 55;
            const y1 = cy + Math.sin(angle) * 55;
            const x2 = cx + Math.cos(angle) * (55 + barLength);
            const y2 = cy + Math.sin(angle) * (55 + barLength);

            const alpha = 0.1 + phase * 0.15;
            this.ctx.strokeStyle = `rgba(0, 212, 255, ${alpha})`; // Cool cyan idle
            this.ctx.lineWidth = 3;
            this.ctx.lineCap = 'round';
            
            this.ctx.shadowBlur = 8;
            this.ctx.shadowColor = `rgba(0, 212, 255, ${alpha * 2})`;

            this.ctx.beginPath();
            this.ctx.moveTo(x1, y1);
            this.ctx.lineTo(x2, y2);
            this.ctx.stroke();
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.app = new AudienceApp();
});
