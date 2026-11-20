export type AmbientSoundMode = 'off' | 'rain' | 'hearth';

const STORAGE_MUTE_KEY = 'pixel_office_sound_muted_v1';
const STORAGE_AMBIENT_KEY = 'pixel_office_ambient_mode_v1';

class SoundFxEngine {
  private ctx: AudioContext | null = null;
  private muted = false;
  private ambientMode: AmbientSoundMode = 'off';
  private ambientSource: AudioBufferSourceNode | null = null;
  private ambientGain: GainNode | null = null;
  private crackleTimer: number | null = null;

  constructor() {
    if (typeof window !== 'undefined') {
      try {
        this.muted = window.localStorage.getItem(STORAGE_MUTE_KEY) === '1';
        const saved = window.localStorage.getItem(STORAGE_AMBIENT_KEY) as AmbientSoundMode | null;
        if (saved === 'off' || saved === 'rain' || saved === 'hearth') {
          this.ambientMode = saved;
        }
      } catch {
        // ignore storage errors
      }
    }
  }

  public setMuted(muted: boolean) {
    this.muted = muted;
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.setItem(STORAGE_MUTE_KEY, muted ? '1' : '0');
      } catch {
        // ignore
      }
    }
    if (muted) {
      this.stopAmbientNodes();
    } else if (this.ambientMode !== 'off') {
      this.startAmbientNodes(this.ambientMode);
    }
  }

  public isMuted(): boolean {
    return this.muted;
  }

  public getAmbientMode(): AmbientSoundMode {
    return this.ambientMode;
  }

  public setAmbientMode(mode: AmbientSoundMode) {
    this.ambientMode = mode;
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.setItem(STORAGE_AMBIENT_KEY, mode);
      } catch {
        // ignore
      }
    }
    this.stopAmbientNodes();
    if (!this.muted && mode !== 'off') {
      this.startAmbientNodes(mode);
    }
  }

  private ensureContext(): AudioContext | null {
    if (this.muted || typeof window === 'undefined') return null;
    if (!this.ctx) {
      const AudioCtx =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) return null;
      this.ctx = new AudioCtx();
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume().catch(() => {});
    }
    return this.ctx;
  }

  private stopAmbientNodes() {
    if (this.crackleTimer !== null && typeof window !== 'undefined') {
      window.clearInterval(this.crackleTimer);
      this.crackleTimer = null;
    }
    try {
      this.ambientSource?.stop();
      this.ambientSource?.disconnect();
    } catch {
      // ignore
    }
    try {
      this.ambientGain?.disconnect();
    } catch {
      // ignore
    }
    this.ambientSource = null;
    this.ambientGain = null;
  }

  private startAmbientNodes(mode: 'rain' | 'hearth') {
    const ctx = this.ensureContext();
    if (!ctx) return;

    // 產生 3 秒循環柔和噪聲緩衝（零外部音檔依賴）
    const seconds = 3;
    const len = ctx.sampleRate * seconds;
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const data = buf.getChannelData(0);
    let b0 = 0;
    let b1 = 0;
    let b2 = 0;
    for (let i = 0; i < len; i++) {
      const white = Math.random() * 2 - 1;
      // 粉紅噪聲近似，聽感溫潤不刺耳
      b0 = 0.997 * b0 + white * 0.029;
      b1 = 0.985 * b1 + white * 0.032;
      b2 = 0.95 * b2 + white * 0.048;
      data[i] = (b0 + b1 + b2 + white * 0.02) * 0.45;
    }

    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.loop = true;

    const filter = ctx.createBiquadFilter();
    const gain = ctx.createGain();
    if (mode === 'rain') {
      filter.type = 'bandpass';
      filter.frequency.value = 1150;
      filter.Q.value = 0.65;
      gain.gain.value = 0.028;
    } else {
      filter.type = 'lowpass';
      filter.frequency.value = 360;
      filter.Q.value = 0.9;
      gain.gain.value = 0.042;
    }

    src.connect(filter);
    filter.connect(gain);
    gain.connect(ctx.destination);
    src.start();

    this.ambientSource = src;
    this.ambientGain = gain;

    if (mode === 'hearth' && typeof window !== 'undefined') {
      // 偶發輕柔柴火劈啪聲
      this.crackleTimer = window.setInterval(() => {
        if (this.muted || this.ambientMode !== 'hearth') return;
        if (Math.random() > 0.38) return;
        this.playHearthCrackle();
      }, 900);
    }
  }

  private playHearthCrackle() {
    const ctx = this.ensureContext();
    if (!ctx) return;
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(900 + Math.random() * 1400, now);
    gain.gain.setValueAtTime(0.001, now);
    gain.gain.exponentialRampToValueAtTime(0.016, now + 0.004);
    gain.gain.exponentialRampToValueAtTime(0.0005, now + 0.028);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.032);
  }

  public playKeyClick() {
    const ctx = this.ensureContext();
    if (!ctx) return;
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(680 + Math.random() * 240, now);
    gain.gain.setValueAtTime(0.001, now);
    gain.gain.exponentialRampToValueAtTime(0.012, now + 0.003);
    gain.gain.exponentialRampToValueAtTime(0.0005, now + 0.022);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.026);
  }

  public playHelpAlert() {
    const ctx = this.ensureContext();
    if (!ctx) return;

    const now = ctx.currentTime;
    const notes = [523.25, 659.25]; // C5 -> E5 gentle chiptune ping
    notes.forEach((freq, idx) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(freq, now + idx * 0.09);
      gain.gain.setValueAtTime(0.001, now + idx * 0.09);
      gain.gain.exponentialRampToValueAtTime(0.08, now + idx * 0.09 + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.001, now + idx * 0.09 + 0.14);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + idx * 0.09);
      osc.stop(now + idx * 0.09 + 0.15);
    });
  }

  public playAssistConfirm() {
    const ctx = this.ensureContext();
    if (!ctx) return;

    const now = ctx.currentTime;
    const notes = [587.33, 783.99, 1046.5]; // D5 -> G5 -> C6 bright resolve chime
    notes.forEach((freq, idx) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, now + idx * 0.065);
      gain.gain.setValueAtTime(0.001, now + idx * 0.065);
      gain.gain.exponentialRampToValueAtTime(0.09, now + idx * 0.065 + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.001, now + idx * 0.065 + 0.18);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + idx * 0.065);
      osc.stop(now + idx * 0.065 + 0.2);
    });
  }

  /** 任務完成清脆風鈴叮噹聲 */
  public playTaskCompleteChime() {
    const ctx = this.ensureContext();
    if (!ctx) return;
    const now = ctx.currentTime;
    const notes = [1046.5, 1318.5, 1567.98]; // C6 -> E6 -> G6 wind chime
    notes.forEach((freq, idx) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, now + idx * 0.11);
      gain.gain.setValueAtTime(0.001, now + idx * 0.11);
      gain.gain.exponentialRampToValueAtTime(0.065, now + idx * 0.11 + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0008, now + idx * 0.11 + 0.45);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + idx * 0.11);
      osc.stop(now + idx * 0.11 + 0.48);
    });
  }
}

export const soundFx = new SoundFxEngine();
