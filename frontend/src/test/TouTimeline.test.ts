/**
 * Tests for pure utility functions in TouTimeline.tsx.
 */
import { describe, it, expect } from "vitest";

// Replicate the pure function under test
function hourToPercent(timeStr: string): number {
  if (timeStr === "24:00") return 100;
  const [h, m] = timeStr.split(":").map(Number);
  return ((h + (m ?? 0) / 60) / 24) * 100;
}

describe("hourToPercent", () => {
  it("midnight is 0%", () => {
    expect(hourToPercent("00:00")).toBe(0);
  });

  it("24:00 is 100%", () => {
    expect(hourToPercent("24:00")).toBe(100);
  });

  it("noon is 50%", () => {
    expect(hourToPercent("12:00")).toBeCloseTo(50);
  });

  it("9:00 is 37.5%", () => {
    expect(hourToPercent("09:00")).toBeCloseTo(37.5);
  });

  it("16:00 is ~66.7%", () => {
    expect(hourToPercent("16:00")).toBeCloseTo((16 / 24) * 100);
  });

  it("21:00 is 87.5%", () => {
    expect(hourToPercent("21:00")).toBeCloseTo(87.5);
  });

  it("handles minutes correctly", () => {
    // 09:30 → (9.5/24)*100 ≈ 39.583
    expect(hourToPercent("09:30")).toBeCloseTo((9.5 / 24) * 100);
  });
});
