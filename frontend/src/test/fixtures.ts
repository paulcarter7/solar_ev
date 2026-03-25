/**
 * Shared test fixtures — minimal valid API response shapes.
 */
import type { SolarTodayResponse, RecommendationResponse, HistoryResponse } from "../api/solar";

export const MOCK_SOLAR: SolarTodayResponse = {
  date: "2026-03-11",
  system_id: "test-system",
  total_production_wh: 24000,
  total_production_kwh: 24,
  hourly_readings: [
    { timestamp: "2026-03-11T17:00:00Z", hour: 9,  production_wh: 3000, power_w: 3000, source: "enphase" },
    { timestamp: "2026-03-11T18:00:00Z", hour: 10, production_wh: 4500, power_w: 4500, source: "enphase" },
    { timestamp: "2026-03-11T19:00:00Z", hour: 11, production_wh: 5000, power_w: 5000, source: "enphase" },
  ],
  tou_schedule: [
    { label: "Super Off-Peak", start: "09:00", end: "14:00", rate_kwh: 0.17, color: "#22c55e" },
    { label: "Off-Peak",       start: "00:00", end: "09:00", rate_kwh: 0.28, color: "#f59e0b" },
    { label: "Peak",           start: "16:00", end: "21:00", rate_kwh: 0.48, color: "#ef4444" },
  ],
  data_source: "enphase",
  home_battery_soc_pct: 72,
  home_battery_capacity_wh: 20000,
  weather: { cloud_cover_pct: 20, temp_c: 18, weather_condition: "Clear" },
};

export const MOCK_RECOMMENDATION: RecommendationResponse = {
  date: "2026-03-11",
  ev_model: "BMW iX 45",
  battery_kwh: 76.6,
  charge_rate_kw: 7.2,
  current_soc_pct: 30,
  target_soc_pct: 80,
  energy_needed_kwh: 38.3,
  hours_needed: 5.3,
  best_window: {
    start: "09:00",
    end: "14:00",
    rate_period: "super off-peak",
    estimated_cost_usd: 3.25,
    solar_coverage_pct: 85,
  },
  summary: "Charge during Super Off-Peak (09:00–14:00) for lowest cost.",
  all_candidates: [
    { start_hour: 9,  end_hour: 14, duration_hours: 5, estimated_cost_usd: 3.25,  solar_coverage_pct: 85, score: 0.12 },
    { start_hour: 14, end_hour: 19, duration_hours: 5, estimated_cost_usd: 12.50, solar_coverage_pct: 20, score: 0.65 },
  ],
  data_source: "enphase",
  home_battery_soc_pct: 72,
  charging_source: "solar_direct",
  charging_source_label: "Direct Solar",
};

export const MOCK_HISTORY: HistoryResponse = {
  start_date: "2026-03-10",
  end_date: "2026-03-16",
  days_requested: 7,
  days: [
    { date: "2026-03-10", total_production_kwh: 22.5, peak_power_w: 4200, data_source: "enphase" },
    { date: "2026-03-11", total_production_kwh: 24.0, peak_power_w: 4500, data_source: "enphase" },
    { date: "2026-03-12", total_production_kwh: 18.3, peak_power_w: 3800, data_source: "enphase" },
    { date: "2026-03-13", total_production_kwh: 0.0,  peak_power_w: 0,    data_source: "no_data" },
    { date: "2026-03-14", total_production_kwh: 25.1, peak_power_w: 4700, data_source: "enphase" },
    { date: "2026-03-15", total_production_kwh: 23.8, peak_power_w: 4300, data_source: "enphase" },
    { date: "2026-03-16", total_production_kwh: 21.2, peak_power_w: 4100, data_source: "enphase" },
  ],
  avg_production_kwh: 19.3,
  data_source: "enphase",
};
