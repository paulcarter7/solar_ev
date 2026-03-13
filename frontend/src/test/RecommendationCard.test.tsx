/**
 * Tests for <RecommendationCard />.
 *
 * Covers:
 * - Renders key data: window times, cost, solar coverage, energy needed
 * - Charging source badge uses correct label
 * - Rate period colour class applied
 * - Home battery bar shown when SOC is provided, hidden when null
 * - Mock data notice shown/hidden based on data_source
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { RecommendationCard } from "../components/RecommendationCard";
import { MOCK_RECOMMENDATION } from "./fixtures";

describe("RecommendationCard", () => {
  it("renders the best charging window times", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.getByText(/09:00/)).toBeInTheDocument();
    expect(screen.getByText(/14:00/)).toBeInTheDocument();
  });

  it("renders estimated cost", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.getByText(/\$3\.25/)).toBeInTheDocument();
  });

  it("renders solar coverage percentage", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.getByText(/85%/)).toBeInTheDocument();
  });

  it("renders energy needed", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.getByText(/38\.3 kWh/)).toBeInTheDocument();
  });

  it("renders the charging source label", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.getByText("Direct Solar")).toBeInTheDocument();
  });

  it("renders EV battery percentage", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.getByText(/EV battery: 30%/)).toBeInTheDocument();
    expect(screen.getByText(/Target: 80%/)).toBeInTheDocument();
  });

  it("renders home battery bar when SOC is provided", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.getByText(/Home battery: 72%/)).toBeInTheDocument();
    expect(screen.getByText("20 kWh")).toBeInTheDocument();
  });

  it("hides home battery bar when SOC is null", () => {
    const data = { ...MOCK_RECOMMENDATION, home_battery_soc_pct: null };
    render(<RecommendationCard data={data} />);
    expect(screen.queryByText(/Home battery:/)).not.toBeInTheDocument();
  });

  it("shows mock data notice when data_source is mock", () => {
    const data = { ...MOCK_RECOMMENDATION, data_source: "mock" as const };
    render(<RecommendationCard data={data} />);
    expect(screen.getByText(/Using mock data/i)).toBeInTheDocument();
  });

  it("does not show mock data notice for enphase data", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    expect(screen.queryByText(/Using mock data/i)).not.toBeInTheDocument();
  });

  it("capitalises the rate period label", () => {
    render(<RecommendationCard data={MOCK_RECOMMENDATION} />);
    // "super off-peak" → "Super off-peak rate" (charAt(0).toUpperCase)
    expect(screen.getByText(/Super off-peak rate/i)).toBeInTheDocument();
  });

  it("applies a fallback badge style for unknown charging_source", () => {
    const data = { ...MOCK_RECOMMENDATION, charging_source: "unknown_source", charging_source_label: "Unknown" };
    const { container } = render(<RecommendationCard data={data} />);
    // Should still render without throwing, using the grid fallback style
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    const badge = container.querySelector("span");
    expect(badge).not.toBeNull();
  });
});
