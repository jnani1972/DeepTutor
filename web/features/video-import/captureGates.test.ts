import { describe, expect, it } from "vitest";

import { FrameLookback, PrerollGate } from "./captureGates";

const PLAYING = 1;
const BUFFERING = 3;

describe("PrerollGate", () => {
  it("stays closed while a pre-roll ad holds the main clock at ~0", () => {
    const gate = new PrerollGate();
    expect(gate.update({ state: PLAYING, currentTime: 0 })).toBe(false);
    expect(gate.update({ state: PLAYING, currentTime: 0.1 })).toBe(false);
    expect(gate.update({ state: PLAYING, currentTime: 0.2 })).toBe(false);
  });

  it("opens once the clock advances past 0.25 s while playing, and stays open", () => {
    const gate = new PrerollGate();
    gate.update({ state: PLAYING, currentTime: 0 });
    expect(gate.update({ state: PLAYING, currentTime: 0.3 })).toBe(true);
    expect(gate.update({ state: BUFFERING, currentTime: 0.3 })).toBe(true);
  });

  it("ignores clock movement while not playing", () => {
    const gate = new PrerollGate();
    expect(gate.update({ state: BUFFERING, currentTime: 0 })).toBe(false);
    expect(gate.update({ state: BUFFERING, currentTime: 5 })).toBe(false);
    // The baseline is the first playing sample, not the buffering one.
    expect(gate.update({ state: PLAYING, currentTime: 5 })).toBe(false);
    expect(gate.update({ state: PLAYING, currentTime: 5.3 })).toBe(true);
  });
});

describe("FrameLookback", () => {
  it("holds frames for 2.5 s before releasing them in order", () => {
    const buffer = new FrameLookback<string>();
    buffer.push(0, "a");
    buffer.push(1000, "b");
    expect(buffer.release(2499)).toEqual([]);
    expect(buffer.release(2500)).toEqual(["a"]);
    expect(buffer.release(3500)).toEqual(["b"]);
    expect(buffer.size).toBe(0);
  });

  it("drops frames captured within the ad stall window", () => {
    const buffer = new FrameLookback<string>();
    buffer.push(0, "before");
    buffer.push(1000, "stall-1");
    buffer.push(2000, "stall-2");
    buffer.dropSince(1000);
    expect(buffer.drain()).toEqual(["before"]);
  });

  it("drains everything on demand", () => {
    const buffer = new FrameLookback<number>();
    buffer.push(10, 1);
    buffer.push(20, 2);
    expect(buffer.drain()).toEqual([1, 2]);
    expect(buffer.drain()).toEqual([]);
  });
});
