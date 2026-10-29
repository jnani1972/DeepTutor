/**
 * Detects pre/mid-roll ads in the embedded YouTube player.
 *
 * Ads in the embed do not advance the main video's clock, so "playing (state 1)
 * while currentTime moved < 0.25 s over >= 2 s of wall time" means an ad is
 * showing. Paused or buffering never counts as an ad.
 */
export type AdVerdict = "record" | "ad";

export interface AdSample {
  wallMs: number;
  state: number;
  currentTime: number;
}

const PLAYING = 1;
const MIN_ADVANCE_SECONDS = 0.25;
const STALL_WINDOW_MS = 2000;

export class AdDetector {
  adSeconds = 0;
  private mode: AdVerdict = "record";
  private anchor: { wallMs: number; currentTime: number } | null = null;
  private lastWallMs: number | null = null;

  update(sample: AdSample): AdVerdict {
    const previousWall = this.lastWallMs;
    this.lastWallMs = sample.wallMs;
    if (this.mode === "ad" && previousWall !== null) {
      this.adSeconds += Math.max(0, sample.wallMs - previousWall) / 1000;
    }

    if (sample.state !== PLAYING) {
      this.anchor = null;
      this.mode = "record";
      return this.mode;
    }
    if (!this.anchor) {
      this.anchor = { wallMs: sample.wallMs, currentTime: sample.currentTime };
      return this.mode;
    }
    if (sample.currentTime - this.anchor.currentTime >= MIN_ADVANCE_SECONDS) {
      this.anchor = { wallMs: sample.wallMs, currentTime: sample.currentTime };
      this.mode = "record";
      return this.mode;
    }
    if (this.mode === "record" && sample.wallMs - this.anchor.wallMs >= STALL_WINDOW_MS) {
      // The stall window itself was ad time too.
      this.adSeconds += (sample.wallMs - this.anchor.wallMs) / 1000;
      this.mode = "ad";
    }
    return this.mode;
  }
}
