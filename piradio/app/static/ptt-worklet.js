/* Push-to-talk audio processor (runs in the browser's audio thread).
 *
 * Microphone (any sample rate) -> 44.1 kHz mono -> FM pre-emphasis (the car
 * radio cuts treble, this boosts it first so the voice stays clear) -> soft
 * limiter (never over-modulate) -> signed 16-bit blocks of ~46 ms posted to
 * the page, which sends them to the Pi.
 */
const OUT_RATE = 44100;
const BLOCK = 2048;

class PttProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.step = sampleRate / OUT_RATE;   // input samples per output sample
    this.t = 0;                          // read position; -1 = last sample of previous block
    this.prev = 0;
    this.k = (o.tau || 0) * OUT_RATE;    // pre-emphasis strength (50 µs -> 2.2)
    this.gain = this.k ? 0.6 : 0.9;      // headroom for the treble boost
    this.last = 0;
    this.out = new Int16Array(BLOCK);
    this.n = 0;
    this.peak = 0;
  }

  emit(x) {
    let y = x + this.k * (x - this.last);
    this.last = x;
    y *= this.gain;
    const a = Math.abs(y);
    if (a > 0.8) y = Math.sign(y) * (0.8 + (a - 0.8) / (1 + (a - 0.8) / 0.2));   // soft knee -> max 1.0
    if (a > this.peak) this.peak = a;
    this.out[this.n++] = Math.max(-32768, Math.min(32767, Math.round(y * 32767)));
    if (this.n === BLOCK) {
      this.port.postMessage({ pcm: this.out.buffer, peak: Math.min(1, this.peak) }, [this.out.buffer]);
      this.out = new Int16Array(BLOCK);
      this.n = 0;
      this.peak = 0;
    }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !ch.length) return true;
    const len = ch.length;
    let t = this.t;
    while (t < len - 1) {                // linear interpolation between neighbours
      const i = Math.floor(t);
      const f = t - i;
      const a = i < 0 ? this.prev : ch[i];
      const b = ch[i + 1];
      this.emit(a + (b - a) * f);
      t += this.step;
    }
    this.t = t - len;
    this.prev = ch[len - 1];
    return true;
  }
}

registerProcessor("ptt-processor", PttProcessor);
