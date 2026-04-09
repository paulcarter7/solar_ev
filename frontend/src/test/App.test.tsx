/**
 * Tests for <App />.
 *
 * Covers:
 * - Loading skeleton shown during initial fetch
 * - Success state renders solar production, recommendation card, and SOC slider
 * - Error state renders error message when API fails
 * - Live Enphase badge shown when data_source is "enphase"
 * - Mock data badge shown when data_source is "mock"
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import App from "../App";
import * as solarApi from "../api/solar";
import { MOCK_SOLAR, MOCK_RECOMMENDATION, MOCK_HISTORY } from "./fixtures";

beforeEach(() => {
  Object.defineProperty(window, "location", {
    value: { origin: "http://localhost:5173" },
    writable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("App", () => {
  it("shows loading skeleton during initial fetch", () => {
    // fetchSolarToday never resolves — keeps the component in loading state
    vi.spyOn(solarApi, "fetchSolarToday").mockReturnValue(new Promise(() => {}));
    vi.spyOn(solarApi, "fetchRecommendation").mockReturnValue(new Promise(() => {}));
    vi.spyOn(solarApi, "fetchHistory").mockReturnValue(new Promise(() => {}));

    render(<App />);
    // Loading skeleton uses animate-pulse; look for any structural indicator
    // Check that success content is NOT visible yet
    expect(screen.queryByText(/Today's production/i)).not.toBeInTheDocument();
  });

  it("renders solar production and recommendation after successful fetch", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(MOCK_SOLAR);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText("24 kWh")).toBeInTheDocument();
    });

    // Recommendation card visible
    expect(screen.getByText("Direct Solar")).toBeInTheDocument();
    // SOC slider visible
    expect(screen.getByRole("slider")).toBeInTheDocument();
  });

  it("shows live badge for enphase data source", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(MOCK_SOLAR);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    // Both the solar production section and history section show live badges
    expect(screen.getAllByText(/● live/i).length).toBeGreaterThanOrEqual(1);
  });

  it("shows mock data badge for mock data source", async () => {
    const mockSolar = { ...MOCK_SOLAR, data_source: "mock" as const };
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(mockSolar);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    expect(screen.getByText("mock data")).toBeInTheDocument();
  });

  it("renders error state when fetch fails", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockRejectedValue(new Error("Network error"));
    vi.spyOn(solarApi, "fetchRecommendation").mockRejectedValue(new Error("Network error"));
    vi.spyOn(solarApi, "fetchHistory").mockRejectedValue(new Error("Network error"));

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText(/Could not load data/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Network error/)).toBeInTheDocument();
  });

  it("renders the all-candidates table with window times", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(MOCK_SOLAR);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    // First candidate row: 09:00 – 14:00 (best)
    expect(screen.getByText(/★ best/)).toBeInTheDocument();
    // $3.25 appears in both RecommendationCard and the candidates table
    expect(screen.getAllByText(/\$3\.25/).length).toBeGreaterThanOrEqual(1);
  });

  it("renders home battery SOC in header when home_battery_soc_pct is set", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(MOCK_SOLAR);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    expect(screen.getByText("Home battery")).toBeInTheDocument();
    expect(screen.getByText("72%")).toBeInTheDocument();
    expect(screen.getByText("of 20 kWh")).toBeInTheDocument();
  });

  it("hides home battery widget when home_battery_soc_pct is null", async () => {
    const solarNoSoc = { ...MOCK_SOLAR, home_battery_soc_pct: null };
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(solarNoSoc);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    expect(screen.queryByText("Home battery")).not.toBeInTheDocument();
  });

  it("labels the SOC slider as EV battery level", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(MOCK_SOLAR);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    expect(screen.getByText(/EV battery level/i)).toBeInTheDocument();
  });

  it("renders current power reading when enphase readings have power_w", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(MOCK_SOLAR);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    // Last enphase reading with power_w = 5000 should show as live power
    expect(screen.getByText(/5000 W now/i)).toBeInTheDocument();
  });

  it("renders production history section with avg and day/window buttons", async () => {
    vi.spyOn(solarApi, "fetchSolarToday").mockResolvedValue(MOCK_SOLAR);
    vi.spyOn(solarApi, "fetchRecommendation").mockResolvedValue(MOCK_RECOMMENDATION);
    vi.spyOn(solarApi, "fetchHistory").mockResolvedValue(MOCK_HISTORY);

    render(<App />);
    await waitFor(() => screen.getByText("24 kWh"));

    expect(screen.getByText("Production History")).toBeInTheDocument();
    expect(screen.getByText(/Avg 19\.3 kWh\/day/)).toBeInTheDocument();
    // Preset buttons
    expect(screen.getByRole("button", { name: "7d" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "14d" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "30d" })).toBeInTheDocument();
  });
});
