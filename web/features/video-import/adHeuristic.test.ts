import { describe, expect, it } from "vitest";

import { AdDetector } from "./adHeuristic";

const PLAYING = 1;
const PAUSED = 2;

describe("AdDetector", () => {
  it("records while the main video clock advances", () => {
    const detector = new AdDetector();
    expect(detector.update({ wallMs: 0, state: PLAYING, currentTime: 10 })).toBe("record");
    expect(detector.update({ wallMs: 1000, state: PLAYING, currentTime: 11 })).toBe("record");
    expect(detector.update({ wallMs: 2000, state: PLAYING, currentTime: 12 })).toBe("record");
    expect(detector.adSeconds).toBe(0);
  });

  it("reports an ad when playing but the clock stalls for 2 s", () => {
    const detector = new AdDetector();
    detector.update({ wallMs: 0, state: PLAYING, currentTime: 5 });
    expect(detector.update({ wallMs: 1000, state: PLAYING, currentTime: 5.1 })).toBe("record");
    expect(detector.update({ wallMs: 1999, state: PLAYING, currentTime: 5.2 })).toBe("record");
    expect(detector.update({ wallMs: 2000, state: PLAYING, currentTime: 5.2 })).toBe("ad");
  });

  it("returns to record once the clock advances again", () => {
    const detector = new AdDetector();
    detector.update({ wallMs: 0, state: PLAYING, currentTime: 0 });
    expect(detector.update({ wallMs: 3000, state: PLAYING, currentTime: 0 })).toBe("ad");
    expect(detector.update({ wallMs: 4000, state: PLAYING, currentTime: 0.1 })).toBe("ad");
    expect(detector.update({ wallMs: 5000, state: PLAYING, currentTime: 1 })).toBe("record");
  });

  it("never counts a paused player as an ad", () => {
    const detector = new AdDetector();
    detector.update({ wallMs: 0, state: PAUSED, currentTime: 30 });
    expect(detector.update({ wallMs: 5000, state: PAUSED, currentTime: 30 })).toBe("record");
    expect(detector.update({ wallMs: 10000, state: PAUSED, currentTime: 30 })).toBe("record");
    expect(detector.adSeconds).toBe(0);
  });

  it("does not carry paused time into a later playing stall", () => {
    const detector = new AdDetector();
    detector.update({ wallMs: 0, state: PAUSED, currentTime: 30 });
    expect(detector.update({ wallMs: 9000, state: PLAYING, currentTime: 30 })).toBe("record");
    expect(detector.update({ wallMs: 10000, state: PLAYING, currentTime: 31 })).toBe("record");
  });

  it("accumulates adSeconds across ad breaks", () => {
    const detector = new AdDetector();
    detector.update({ wallMs: 0, state: PLAYING, currentTime: 0 });
    detector.update({ wallMs: 2000, state: PLAYING, currentTime: 0 }); // ad detected (2 s)
    detector.update({ wallMs: 4000, state: PLAYING, currentTime: 0 }); // still ad (+2 s)
    detector.update({ wallMs: 5000, state: PLAYING, currentTime: 1 }); // back (+1 s)
    expect(detector.adSeconds).toBeCloseTo(5);
    detector.update({ wallMs: 6000, state: PLAYING, currentTime: 2 });
    detector.update({ wallMs: 8000, state: PLAYING, currentTime: 2 }); // ad (2 s)
    detector.update({ wallMs: 9000, state: PLAYING, currentTime: 3 }); // back (+1 s)
    expect(detector.adSeconds).toBeCloseTo(8);
  });
});
