/**
 * API client for the Home Energy Optimizer backend.
 *
 * Priority order for base URL:
 *  1. VITE_API_URL env var (set in .env.local for deployed API Gateway)
 *  2. /api  (proxied by Vite dev server to localhost:3001 for SAM local)
 */
const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface HourlyReading {
  timestamp: string;
  hour: number;
  production_wh: number;
  power_w?: number;
  source: "enphase" | "mock" | "no_data";
}

export interface TouPeriod {
  label: string;
  start: string;
  end: string;
  rate_kwh: number;
  color: string;
}

export interface Weather {
  cloud_cover_pct: number;
  temp_c: number;
  weather_condition: string;
}

export interface SolarTodayResponse {
  date: string;
  system_id: string;
  total_production_wh: number;
  total_production_kwh: number;
  hourly_readings: HourlyReading[];
  tou_schedule: TouPeriod[];
  data_source: "enphase" | "mock";
  home_battery_soc_pct: number | null;
  home_battery_capacity_wh: number;
  weather: Weather | null;
}

export interface ChargingWindow {
  start: string;
  end: string;
  rate_period: string;
  estimated_cost_usd: number;
  solar_coverage_pct: number;
}

export interface RecommendationResponse {
  date: string;
  ev_model: string;
  battery_kwh: number;
  charge_rate_kw: number;
  current_soc_pct: number;
  target_soc_pct: number;
  energy_needed_kwh: number;
  hours_needed: number;
  best_window: ChargingWindow;
  summary: string;
  all_candidates: Array<{
    start_hour: number;
    end_hour: number;
    duration_hours: number;
    estimated_cost_usd: number;
    solar_coverage_pct: number;
    score: number;
  }>;
  data_source: "enphase" | "mock";
  home_battery_soc_pct: number | null;
  charging_source: string;
  charging_source_label: string;
}

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(`${BASE_URL}${path}`, window.location.origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  }
  const res = await fetch(url.toString());
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

export const fetchSolarToday = (date?: string) =>
  get<SolarTodayResponse>("/solar/today", date ? { date } : undefined);

export const fetchRecommendation = (params?: { current_soc?: number; target_soc?: number; date?: string }) => {
  const qp: Record<string, string> = {};
  if (params?.current_soc !== undefined) qp.current_soc = String(params.current_soc);
  if (params?.target_soc !== undefined) qp.target_soc = String(params.target_soc);
  if (params?.date !== undefined) qp.date = params.date;
  return get<RecommendationResponse>("/recommendation", Object.keys(qp).length ? qp : undefined);
};
