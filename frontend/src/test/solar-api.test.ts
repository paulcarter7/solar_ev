/**
 * Tests for src/api/solar.ts
 *
 * Covers:
 * - fetchSolarToday: with and without date param, error handling
 * - fetchRecommendation: parameter encoding, partial params, error handling
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fetchSolarToday, fetchRecommendation, fetchHistory } from "../api/solar";
import { MOCK_SOLAR, MOCK_RECOMMENDATION, MOCK_HISTORY } from "./fixtures";

function mockFetch(data: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: () => Promise.resolve(data),
    text: () => Promise.resolve(JSON.stringify(data)),
  });
}

beforeEach(() => {
  // jsdom provides window.location — set a stable origin
  Object.defineProperty(window, "location", {
    value: { origin: "http://localhost:5173" },
    writable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// fetchSolarToday
// ---------------------------------------------------------------------------

describe("fetchSolarToday", () => {
  it("calls /solar/today with no params when date is omitted", async () => {
    const mock = mockFetch(MOCK_SOLAR);
    vi.stubGlobal("fetch", mock);

    await fetchSolarToday();

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("/solar/today");
    expect(calledUrl).not.toContain("date=");
  });

  it("appends date query param when provided", async () => {
    const mock = mockFetch(MOCK_SOLAR);
    vi.stubGlobal("fetch", mock);

    await fetchSolarToday("2026-03-11");

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("date=2026-03-11");
  });

  it("returns parsed JSON on success", async () => {
    vi.stubGlobal("fetch", mockFetch(MOCK_SOLAR));
    const result = await fetchSolarToday("2026-03-11");
    expect(result.date).toBe("2026-03-11");
    expect(result.total_production_kwh).toBe(24);
    expect(result.data_source).toBe("enphase");
  });

  it("throws with status code on non-ok response", async () => {
    vi.stubGlobal("fetch", mockFetch({ error: "not found" }, false, 404));
    await expect(fetchSolarToday("2026-03-11")).rejects.toThrow("API 404");
  });
});

// ---------------------------------------------------------------------------
// fetchRecommendation
// ---------------------------------------------------------------------------

describe("fetchRecommendation", () => {
  it("calls /recommendation with no params when called with no args", async () => {
    const mock = mockFetch(MOCK_RECOMMENDATION);
    vi.stubGlobal("fetch", mock);

    await fetchRecommendation();

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("/recommendation");
    expect(calledUrl).not.toContain("current_soc");
  });

  it("encodes current_soc and target_soc as decimals", async () => {
    const mock = mockFetch(MOCK_RECOMMENDATION);
    vi.stubGlobal("fetch", mock);

    await fetchRecommendation({ current_soc: 0.3, target_soc: 0.8 });

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("current_soc=0.3");
    expect(calledUrl).toContain("target_soc=0.8");
  });

  it("encodes date param when provided", async () => {
    const mock = mockFetch(MOCK_RECOMMENDATION);
    vi.stubGlobal("fetch", mock);

    await fetchRecommendation({ date: "2026-03-11" });

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("date=2026-03-11");
  });

  it("omits params whose values are undefined", async () => {
    const mock = mockFetch(MOCK_RECOMMENDATION);
    vi.stubGlobal("fetch", mock);

    await fetchRecommendation({ current_soc: 0.5 });

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).not.toContain("target_soc");
    expect(calledUrl).not.toContain("date=");
  });

  it("returns parsed recommendation on success", async () => {
    vi.stubGlobal("fetch", mockFetch(MOCK_RECOMMENDATION));
    const result = await fetchRecommendation({ current_soc: 0.3, target_soc: 0.8 });
    expect(result.charging_source).toBe("solar_direct");
    expect(result.best_window.rate_period).toBe("super off-peak");
  });

  it("throws with status code on non-ok response", async () => {
    vi.stubGlobal("fetch", mockFetch({}, false, 500));
    await expect(fetchRecommendation()).rejects.toThrow("API 500");
  });
});

// ---------------------------------------------------------------------------
// fetchHistory
// ---------------------------------------------------------------------------

describe("fetchHistory", () => {
  it("calls /solar/history with no params when days is omitted", async () => {
    const mock = mockFetch(MOCK_HISTORY);
    vi.stubGlobal("fetch", mock);

    await fetchHistory();

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("/solar/history");
    expect(calledUrl).not.toContain("days=");
  });

  it("appends days query param when provided", async () => {
    const mock = mockFetch(MOCK_HISTORY);
    vi.stubGlobal("fetch", mock);

    await fetchHistory(30);

    const calledUrl = mock.mock.calls[0][0] as string;
    expect(calledUrl).toContain("days=30");
  });

  it("returns parsed history on success", async () => {
    vi.stubGlobal("fetch", mockFetch(MOCK_HISTORY));
    const result = await fetchHistory(7);
    expect(result.days_requested).toBe(7);
    expect(result.days).toHaveLength(7);
    expect(result.avg_production_kwh).toBe(19.3);
  });

  it("throws with status code on non-ok response", async () => {
    vi.stubGlobal("fetch", mockFetch({}, false, 500));
    await expect(fetchHistory(7)).rejects.toThrow("API 500");
  });
});
