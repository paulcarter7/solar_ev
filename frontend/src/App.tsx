import { useEffect, useRef, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { fetchSolarToday, fetchRecommendation, fetchHistory } from "./api/solar";
import type { SolarTodayResponse, RecommendationResponse, HistoryResponse, Weather } from "./api/solar";
import { SolarChart } from "./components/SolarChart";
import { TouTimeline } from "./components/TouTimeline";
import { RecommendationCard } from "./components/RecommendationCard";

function weatherIcon(condition: string): string {
  switch (condition) {
    case "Clear":       return "☀️";
    case "Clouds":      return "☁️";
    case "Rain":
    case "Drizzle":     return "🌧️";
    case "Thunderstorm":return "⛈️";
    case "Snow":        return "❄️";
    case "Fog":
    case "Mist":
    case "Haze":        return "🌫️";
    default:            return "🌤️";
  }
}

type Status = "idle" | "loading" | "success" | "error";

interface AppState {
  solar: SolarTodayResponse | null;
  recommendation: RecommendationResponse | null;
  history: HistoryResponse | null;
  status: Status;
  error: string | null;
}

function WeatherWidget({ weather }: { weather: Weather }) {
  return (
    <div className="text-right">
      <p className="text-xs text-gray-500">Current weather</p>
      <p className="text-lg font-semibold text-gray-200">
        {weatherIcon(weather.weather_condition)} {weather.temp_c}°C / {Math.round(weather.temp_c * 9 / 5 + 32)}°F
      </p>
      <p className="text-xs text-gray-400">{weather.cloud_cover_pct}% cloud cover</p>
    </div>
  );
}

export default function App() {
  const [state, setState] = useState<AppState>({
    solar: null,
    recommendation: null,
    history: null,
    status: "idle",
    error: null,
  });

  const [historyDays, setHistoryDays] = useState(14);
  const [historyLoading, setHistoryLoading] = useState(false);

  // SOC = State of Charge (battery %). Default 30%, target 80%.
  const [currentSoc, setCurrentSoc] = useState(30);
  const [recLoading, setRecLoading] = useState(false);

  // Prevents the SOC-change effect from firing before the initial load completes.
  const initialized = useRef(false);

  // Initial load — fetch solar data and first recommendation in parallel.
  useEffect(() => {
    setState((s) => ({ ...s, status: "loading" }));

    // Use the local date so the requested day matches the user's clock,
    // not UTC (which can be a day ahead after ~4 pm Pacific).
    const localToday = new Date().toLocaleDateString("en-CA"); // → YYYY-MM-DD
    Promise.all([
      fetchSolarToday(localToday),
      fetchRecommendation({ current_soc: currentSoc / 100, target_soc: 0.8, date: localToday }),
      fetchHistory(14),
    ])
      .then(([solar, recommendation, history]) => {
        initialized.current = true;
        setState({ solar, recommendation, history, status: "success", error: null });
      })
      .catch((err: Error) => {
        setState((s) => ({ ...s, status: "error", error: err.message }));
      });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Re-fetch recommendation whenever SOC slider changes (debounced 400 ms).
  useEffect(() => {
    if (!initialized.current) return;
    setRecLoading(true);
    const localToday = new Date().toLocaleDateString("en-CA");
    const timer = setTimeout(() => {
      fetchRecommendation({ current_soc: currentSoc / 100, target_soc: 0.8, date: localToday })
        .then((rec) => {
          setState((s) => ({ ...s, recommendation: rec }));
          setRecLoading(false);
        })
        .catch(() => setRecLoading(false));
    }, 400);
    return () => clearTimeout(timer);
  }, [currentSoc]);

  const handleHistoryDaysChange = (days: number) => {
    setHistoryDays(days);
    setHistoryLoading(true);
    fetchHistory(days)
      .then((history) => {
        setState((s) => ({ ...s, history }));
        setHistoryLoading(false);
      })
      .catch(() => setHistoryLoading(false));
  };

  const { solar, recommendation, history, status, error } = state;

  // Most-recent instantaneous power reading (W) from real Enphase data
  const liveReadings = solar?.hourly_readings.filter(
    (r) => r.source === "enphase" && (r.power_w ?? 0) > 0
  ) ?? [];
  const currentPowerW: number | null =
    liveReadings.length > 0
      ? (liveReadings[liveReadings.length - 1]?.power_w ?? null)
      : null;

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* Header */}
      <header className="border-b border-gray-800 px-6 py-4">
        <div className="max-w-4xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-2xl">☀️</span>
            <div>
              <h1 className="text-lg font-semibold leading-tight">Home Energy Optimizer</h1>
              <p className="text-xs text-gray-500">Enphase · MCE/PG&E E-TOU-C · BMW iX 45</p>
            </div>
          </div>
          {solar && (
            <div className="flex items-center gap-6">
              {solar.weather && (
                <WeatherWidget weather={solar.weather} />
              )}
              <div className="text-right">
                <p className="text-xs text-gray-500">Today's production</p>
                <p className="text-xl font-bold text-yellow-400">{solar.total_production_kwh} kWh</p>
                {currentPowerW !== null && (
                  <p className="text-xs text-green-400 mt-0.5">{currentPowerW} W now</p>
                )}
              </div>
            </div>
          )}
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-8 space-y-8">
        {/* Error state */}
        {status === "error" && (
          <div className="rounded-xl bg-red-950 border border-red-800 p-4 text-red-300 text-sm">
            <strong>Could not load data.</strong> {error}
            <br />
            <span className="text-red-400 text-xs mt-1 block">
              Make sure the backend is running (see README for local dev instructions).
            </span>
          </div>
        )}

        {/* Loading skeleton */}
        {status === "loading" && (
          <div className="space-y-4 animate-pulse">
            <div className="h-64 rounded-2xl bg-gray-800" />
            <div className="h-20 rounded-2xl bg-gray-800" />
            <div className="h-48 rounded-2xl bg-gray-800" />
          </div>
        )}

        {status === "success" && solar && recommendation && (
          <>
            {/* Solar production chart */}
            <section className="rounded-2xl bg-gray-800 border border-gray-700 p-5 space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-gray-200">Solar Production — {solar.date}</h2>
                {solar.data_source === "enphase" ? (
                  <span className="text-xs bg-green-900/50 text-green-400 border border-green-800 px-2 py-0.5 rounded-full">● live</span>
                ) : (
                  <span className="text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded-full">mock data</span>
                )}
              </div>
              <SolarChart readings={solar.hourly_readings} touSchedule={solar.tou_schedule} />
            </section>

            {/* TOU timeline */}
            <section className="rounded-2xl bg-gray-800 border border-gray-700 p-5 space-y-3">
              <h2 className="text-sm font-semibold text-gray-200">Time-of-Use Rate Schedule</h2>
              <TouTimeline schedule={solar.tou_schedule} />
            </section>

            {/* SOC input + Recommendation */}
            <section className="rounded-2xl bg-gray-800 border border-gray-700 p-5 space-y-4">
              {/* Battery slider */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <label className="text-sm font-semibold text-gray-200">
                    🔋 Current battery level
                  </label>
                  <span className="text-lg font-bold text-blue-400">{currentSoc}%</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={5}
                  value={currentSoc}
                  onChange={(e) => setCurrentSoc(Number(e.target.value))}
                  className="w-full h-2 rounded-full appearance-none cursor-pointer
                    bg-gray-700 accent-blue-500"
                />
                <div className="flex justify-between text-xs text-gray-600">
                  <span>0%</span>
                  <span className="text-gray-500">Target: 80%</span>
                  <span>100%</span>
                </div>
              </div>

              {/* Recommendation (dims while re-fetching) */}
              <div className={recLoading ? "opacity-50 pointer-events-none transition-opacity" : "transition-opacity"}>
                <RecommendationCard data={recommendation} />
              </div>
              {recLoading && (
                <p className="text-xs text-center text-gray-500 animate-pulse">Updating recommendation…</p>
              )}
            </section>

            {/* Production history chart */}
            {history && (
              <section className="rounded-2xl bg-gray-800 border border-gray-700 p-5 space-y-3">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div>
                    <h2 className="text-sm font-semibold text-gray-200">Production History</h2>
                    <p className="text-xs text-gray-500 mt-0.5">
                      Avg {history.avg_production_kwh} kWh/day
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {[7, 14, 30].map((d) => (
                      <button
                        key={d}
                        onClick={() => handleHistoryDaysChange(d)}
                        className={`text-xs px-3 py-1 rounded-full border transition-colors ${
                          historyDays === d
                            ? "bg-blue-600 border-blue-500 text-white"
                            : "border-gray-600 text-gray-400 hover:border-gray-400 hover:text-gray-200"
                        }`}
                      >
                        {d}d
                      </button>
                    ))}
                    {history.data_source === "enphase" ? (
                      <span className="text-xs bg-green-900/50 text-green-400 border border-green-800 px-2 py-0.5 rounded-full">● live</span>
                    ) : (
                      <span className="text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded-full">mock data</span>
                    )}
                  </div>
                </div>
                <div className={historyLoading ? "opacity-50 transition-opacity" : "transition-opacity"}>
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart
                      data={history.days}
                      margin={{ top: 4, right: 16, left: 0, bottom: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                      <XAxis
                        dataKey="date"
                        tick={{ fill: "#9ca3af", fontSize: 10 }}
                        tickFormatter={(d: string) => {
                          const dt = new Date(d + "T12:00:00");
                          return dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });
                        }}
                        interval={Math.floor(history.days.length / 7)}
                        tickLine={false}
                      />
                      <YAxis
                        tick={{ fill: "#9ca3af", fontSize: 11 }}
                        tickFormatter={(v: number) => `${v}`}
                        unit=" kWh"
                        width={56}
                      />
                      <Tooltip
                        contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", borderRadius: 8 }}
                        labelStyle={{ color: "#f9fafb" }}
                        labelFormatter={(d: string) =>
                          new Date(d + "T12:00:00").toLocaleDateString("en-US", {
                            weekday: "short", month: "short", day: "numeric",
                          })
                        }
                        formatter={(value: number, _name: string, props: { payload?: { peak_power_w: number } }) => {
                          const peak = props.payload?.peak_power_w;
                          const peakStr = peak && peak > 0 ? `  ·  peak ${peak} W` : "";
                          return [`${value.toFixed(2)} kWh${peakStr}`, "Production"];
                        }}
                      />
                      <Bar
                        dataKey="total_production_kwh"
                        fill="#3b82f6"
                        radius={[3, 3, 0, 0]}
                        maxBarSize={40}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
            )}

            {/* Alternate windows table */}
            <section className="rounded-2xl bg-gray-800 border border-gray-700 p-5 space-y-3">
              <h2 className="text-sm font-semibold text-gray-200">Other charging options today</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-gray-500 border-b border-gray-700">
                      <th className="text-left pb-2 font-normal">Window</th>
                      <th className="text-right pb-2 font-normal">Est. Cost</th>
                      <th className="text-right pb-2 font-normal">Solar Cover</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recommendation.all_candidates.map((c, i) => (
                      <tr
                        key={i}
                        className={`border-b border-gray-700/50 ${i === 0 ? "text-yellow-400 font-medium" : "text-gray-300"}`}
                      >
                        <td className="py-2">
                          {String(c.start_hour).padStart(2, "0")}:00 –{" "}
                          {String(c.end_hour).padStart(2, "0")}:00
                          {i === 0 && <span className="ml-2 text-xs text-yellow-500">★ best</span>}
                        </td>
                        <td className="text-right py-2">${c.estimated_cost_usd.toFixed(2)}</td>
                        <td className="text-right py-2">{c.solar_coverage_pct.toFixed(0)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}
      </main>

      <footer className="border-t border-gray-800 mt-16 px-6 py-4">
        <p className="text-xs text-gray-600 text-center">
          Home Energy Optimizer · built with React + AWS Lambda + CDK
        </p>
      </footer>
    </div>
  );
}
