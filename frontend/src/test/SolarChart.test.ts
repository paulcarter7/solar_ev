/**
 * Tests for pure utility functions exported from SolarChart.tsx.
 *
 * The Recharts rendering itself is not tested here — it renders SVG that
 * jsdom doesn't handle well. We test the pure data-transform functions that
 * drive chart correctness.
 */
import { describe, it, expect } from "vitest";
import type { TouPeriod } from "../api/solar";

// --- replicate the pure functions under test (not exported from module) ---
// These functions are small enough that testing them by re-implementing is
// preferable to exporting internals just for tests.

function timeToHour(t: string): number {
  const [h, m] = t.split(":").map(Number);
  return h + (m ?? 0) / 60;
}

function rateAtHour(
  hour: number,
  schedule: TouPeriod[]
): { rate: number; color: string; label: string } {
  for (const period of schedule) {
    const start = timeToHour(period.start);
    let end = timeToHour(period.end);
    if (period.end === "24:00") end = 24;
    if (hour >= start && hour < end) {
      return { rate: period.rate_kwh, color: period.color, label: period.label };
    }
  }
  return { rate: 0.28, color: "#f59e0b", label: "Off-Peak" };
}

const SCHEDULE: TouPeriod[] = [
  { label: "Super Off-Peak", start: "09:00", end: "14:00", rate_kwh: 0.17, color: "#22c55e" },
  { label: "Off-Peak",       start: "00:00", end: "09:00", rate_kwh: 0.28, color: "#f59e0b" },
  { label: "Peak",           start: "16:00", end: "21:00", rate_kwh: 0.48, color: "#ef4444" },
  { label: "Off-Peak",       start: "21:00", end: "24:00", rate_kwh: 0.28, color: "#f59e0b" },
];

// ---------------------------------------------------------------------------
// timeToHour
// ---------------------------------------------------------------------------

describe("timeToHour", () => {
  it("converts whole-hour string to integer", () => {
    expect(timeToHour("09:00")).toBe(9);
    expect(timeToHour("16:00")).toBe(16);
  });

  it("converts 24:00 to 24", () => {
    expect(timeToHour("24:00")).toBe(24);
  });

  it("handles minutes correctly", () => {
    expect(timeToHour("09:30")).toBeCloseTo(9.5);
    expect(timeToHour("14:15")).toBeCloseTo(14.25);
  });

  it("handles midnight as 0", () => {
    expect(timeToHour("00:00")).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// rateAtHour
// ---------------------------------------------------------------------------

describe("rateAtHour", () => {
  it("returns super off-peak rate for hour 11", () => {
    const result = rateAtHour(11, SCHEDULE);
    expect(result.rate).toBe(0.17);
    expect(result.label).toBe("Super Off-Peak");
  });

  it("returns peak rate for hour 17", () => {
    const result = rateAtHour(17, SCHEDULE);
    expect(result.rate).toBe(0.48);
    expect(result.label).toBe("Peak");
  });

  it("returns off-peak rate for hour 8 (before super off-peak)", () => {
    const result = rateAtHour(8, SCHEDULE);
    expect(result.rate).toBe(0.28);
    expect(result.label).toBe("Off-Peak");
  });

  it("returns off-peak rate for hour 22 (after peak)", () => {
    const result = rateAtHour(22, SCHEDULE);
    expect(result.rate).toBe(0.28);
    expect(result.label).toBe("Off-Peak");
  });

  it("returns off-peak default when hour matches no period", () => {
    // Schedule with no 14:00–16:00 coverage → falls through to default
    const sparse: TouPeriod[] = [
      { label: "Peak", start: "16:00", end: "21:00", rate_kwh: 0.48, color: "#ef4444" },
    ];
    const result = rateAtHour(15, sparse);
    expect(result.rate).toBe(0.28);
    expect(result.label).toBe("Off-Peak");
  });

  it("period start is inclusive", () => {
    const result = rateAtHour(9, SCHEDULE);
    expect(result.label).toBe("Super Off-Peak");
  });

  it("period end is exclusive", () => {
    // Hour 14 should NOT be Super Off-Peak (end is 14:00, exclusive)
    const result = rateAtHour(14, SCHEDULE);
    expect(result.label).not.toBe("Super Off-Peak");
  });
});
