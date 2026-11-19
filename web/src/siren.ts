// Storm siren on Web Audio. Silent until the user switches it on (browsers require a gesture anyway).
// Two detuned oscillators wail between a low and a high pitch; loudness and wail rate follow the score.

export class Siren {
  private ctx: AudioContext | null = null;
  private gain: GainNode | null = null;
  private lfo: OscillatorNode | null = null;
  private lfoDepth: GainNode | null = null;
  private oscs: OscillatorNode[] = [];
  /** last loudness requested, 0..1; exposed for tests and the UI meter */
  level = 0;

  get running(): boolean {
    return this.ctx !== null && this.ctx.state === "running";
  }

  async start(): Promise<boolean> {
    const AC: typeof AudioContext | undefined =
      window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return false;
    if (!this.ctx) {
      const ctx = new AC();
      const gain = ctx.createGain();
      gain.gain.value = 0;
      const filter = ctx.createBiquadFilter();
      filter.type = "lowpass";
      filter.frequency.value = 1800;
      filter.connect(gain).connect(ctx.destination);
      const lfo = ctx.createOscillator();
      lfo.frequency.value = 0.25;
      const depth = ctx.createGain();
      depth.gain.value = 0;
      lfo.connect(depth);
      for (const detune of [0, 7]) {
        const o = ctx.createOscillator();
        o.type = "sawtooth";
        o.frequency.value = 440;
        o.detune.value = detune;
        depth.connect(o.frequency);
        o.connect(filter);
        o.start();
        this.oscs.push(o);
      }
      lfo.start();
      this.ctx = ctx;
      this.gain = gain;
      this.lfo = lfo;
      this.lfoDepth = depth;
    }
    try {
      await this.ctx.resume();
    } catch {
      return false;
    }
    return this.ctx.state === "running";
  }

  /** score 0..100, or null for no trustworthy reading (silent). Below `watch` (25) the siren is silent. */
  setScore(score: number | null): void {
    const x = score === null ? 0 : Math.min(1, Math.max(0, (score - 25) / 75));
    this.level = x;
    if (!this.ctx || !this.gain || !this.lfo || !this.lfoDepth) return;
    const t = this.ctx.currentTime;
    this.gain.gain.setTargetAtTime(x === 0 ? 0 : 0.03 + 0.17 * x, t, 0.4);
    this.lfo.frequency.setTargetAtTime(0.15 + 0.55 * x, t, 0.4);
    this.lfoDepth.gain.setTargetAtTime(60 + 200 * x, t, 0.4);
    for (const o of this.oscs) o.frequency.setTargetAtTime(380 + 160 * x, t, 0.4);
  }

  async stop(): Promise<void> {
    this.level = 0;
    if (!this.ctx || !this.gain) return;
    this.gain.gain.setTargetAtTime(0, this.ctx.currentTime, 0.05);
    try {
      await this.ctx.suspend();
    } catch {
      /* already closed */
    }
  }
}
