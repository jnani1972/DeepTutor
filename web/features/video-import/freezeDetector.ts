/**
 * Detects a captured tab that stopped painting while hidden.
 *
 * Returns true (show "keep this window visible") when the tab is hidden, the
 * video clock advanced >= 5 s, and no captured frame changed in that span.
 */
export interface FreezeSample {
  hidden: boolean;
  frameChanged: boolean;
  currentTime: number;
}

const FREEZE_SECONDS = 5;

export class FreezeDetector {
  private anchor: number | null = null;

  update(sample: FreezeSample): boolean {
    const { hidden, frameChanged, currentTime } = sample;
    if (
      !hidden ||
      frameChanged ||
      this.anchor === null ||
      currentTime < this.anchor
    ) {
      this.anchor = currentTime;
      return false;
    }
    return currentTime - this.anchor >= FREEZE_SECONDS;
  }

  reset(): void {
    this.anchor = null;
  }
}
