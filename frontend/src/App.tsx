import { useEffect, useState } from "react";
import { fetchSolarToday, fetchRecommendation } from "./api/solar";
import type { SolarTodayResponse, RecommendationResponse } from "./api/solar";
import { SolarChart } from "./components/SolarChart";
import { TouTimeline } from "./components/TouTimeline";
import { RecommendationCard } from "./components/RecommendationCard";

type Status = "idle" | "loading" | "success" | "error";

interface AppState {
  solar: SolarTodayResponse | null;
  recommendation: RecommendationResponse | null;
  status: Status;
  error: string | null;
}

export default function App() {
  const [state, setState] = useState<AppState>({
    solar: null,
    recommendation: null,
    status: "idle",
    error: null,
  });

  useEffect(() => {
    setState((s) => ({ ...s, status: "loading" }));

    // Use the local date so the requested day matches the user's clock,
    // not UTC (which can be a day ahead after ~4 pm Pacific).
    const localToday = new Date().toLocaleDateString("en-CA"); // → YYYY-MM-DD
    Promise.all([fetchSolarToday(localToday), fetchRecommendation({ current_soc: 0.3, target_soc: 0.8 })])
      .then(([solar, recommendation]) => {
        setState({ solar, recommendation, status: "success", error: null });
      })
      .catch((err: Error) => {
        setState((s) => ({ ...s, status: "error", error: err.message }));
      });
  }, []);

  const { solar, recommendation, status, error } = state;

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
            <div className="text-right">
              <p className="text-xs text-gray-500">Today's production</p>
              <p className="text-xl font-bold text-yellow-400">{solar.total_production_kwh} kWh</p>
              {currentPowerW !== null && (
                <p className="text-xs text-green-400 mt-0.5">{currentPowerW} W now</p>
              )}
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

            {/* Recommendation */}
            <RecommendationCard data={recommendation} />

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
