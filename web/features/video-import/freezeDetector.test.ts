import { describe, expect, it } from "vitest";

import { FreezeDetector } from "./freezeDetector";

describe("FreezeDetector", () => {
  it("flags a hidden tab whose frames stop changing while the clock runs 5 s", () => {
    const detector = new FreezeDetector();
    expect(detector.update({ hidden: true, frameChanged: true, currentTime: 10 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 12 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 14.9 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 15 })).toBe(true);
  });

  it("never flags a visible tab", () => {
    const detector = new FreezeDetector();
    for (let t = 0; t <= 20; t += 1) {
      expect(detector.update({ hidden: false, frameChanged: false, currentTime: t })).toBe(false);
    }
  });

  it("resets when a captured frame changes", () => {
    const detector = new FreezeDetector();
    detector.update({ hidden: true, frameChanged: false, currentTime: 0 });
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 6 })).toBe(true);
    expect(detector.update({ hidden: true, frameChanged: true, currentTime: 7 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 11 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 12 })).toBe(true);
  });

  it("starts the span when the tab becomes hidden", () => {
    const detector = new FreezeDetector();
    detector.update({ hidden: false, frameChanged: false, currentTime: 0 });
    detector.update({ hidden: false, frameChanged: false, currentTime: 20 });
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 21 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 25 })).toBe(true);
  });

  it("resets when the clock jumps backwards (next video)", () => {
    const detector = new FreezeDetector();
    detector.update({ hidden: true, frameChanged: false, currentTime: 100 });
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 1 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 5 })).toBe(false);
    expect(detector.update({ hidden: true, frameChanged: false, currentTime: 6 })).toBe(true);
  });
});
