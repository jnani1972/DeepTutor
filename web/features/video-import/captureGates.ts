/**
 * Small pure gates that keep ad content out of a capture.
 *
 * `PrerollGate` keeps recording off until the main video clock has advanced
 * while playing: pre-roll ads hold `currentTime` at ~0. `FrameLookback` holds
 * grabbed frames long enough that, when `AdDetector` flips to "ad" after its
 * 2 s stall window, the frames captured inside that window can be dropped.
 */

const PLAYING = 1;
const PREROLL_ADVANCE_SECONDS = 0.25;
export const FRAME_LOOKBACK_MS = 2500;

export class PrerollGate {
  private baseline: number | null = null;
  private opened = false;

  get open(): boolean {
    return this.opened;
  }

  update(sample: { state: number; currentTime: number }): boolean {
    if (this.opened || sample.state !== PLAYING) return this.opened;
    if (this.baseline === null) {
      this.baseline = sample.currentTime;
      return false;
    }
    if (sample.currentTime - this.baseline >= PREROLL_ADVANCE_SECONDS) {
      this.opened = true;
    }
    return this.opened;
  }
}

export class FrameLookback<T> {
  private items: Array<{ wallMs: number; item: T }> = [];

  constructor(private readonly holdMs = FRAME_LOOKBACK_MS) {}

  get size(): number {
    return this.items.length;
  }

  push(wallMs: number, item: T): void {
    this.items.push({ wallMs, item });
  }

  /** Frames held at least `holdMs`, oldest first; they leave the buffer. */
  release(nowMs: number): T[] {
    const ready: T[] = [];
    while (this.items.length && nowMs - this.items[0].wallMs >= this.holdMs) {
      ready.push(this.items.shift()!.item);
    }
    return ready;
  }

  /** Discard frames captured at or after `wallMs` (the ad's stall window). */
  dropSince(wallMs: number): void {
    this.items = this.items.filter((entry) => entry.wallMs < wallMs);
  }

  drain(): T[] {
    const all = this.items.map((entry) => entry.item);
    this.items = [];
    return all;
  }
}
