import type { RecommendationResponse } from "../api/solar";

interface Props {
  data: RecommendationResponse;
}

const RATE_COLOR: Record<string, string> = {
  "super off-peak": "text-green-400",
  "off-peak": "text-amber-400",
  "peak": "text-red-400",
};

export function RecommendationCard({ data }: Props) {
  const { best_window, energy_needed_kwh, current_soc_pct, target_soc_pct, ev_model } = data;
  const rateColor = RATE_COLOR[best_window.rate_period] ?? "text-gray-300";

  return (
    <div className="rounded-2xl bg-gray-800 border border-gray-700 p-5 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wider">EV Charging Recommendation</p>
          <p className="text-sm text-gray-400 mt-0.5">{ev_model}</p>
        </div>
        <span className="text-2xl">⚡</span>
      </div>

      {/* Best window */}
      <div className="rounded-xl bg-gray-900 px-4 py-3 space-y-1">
        <p className="text-xs text-gray-500">Best charging window today</p>
        <p className="text-3xl font-bold text-white tracking-tight">
          {best_window.start} – {best_window.end}
        </p>
        <p className={`text-sm font-medium ${rateColor}`}>
          {best_window.rate_period.charAt(0).toUpperCase() + best_window.rate_period.slice(1)} rate
        </p>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-3 gap-3 text-center">
        <Stat label="Est. Cost" value={`$${best_window.estimated_cost_usd.toFixed(2)}`} />
        <Stat
          label="Solar Cover"
          value={`${best_window.solar_coverage_pct.toFixed(0)}%`}
          valueClass={best_window.solar_coverage_pct > 50 ? "text-green-400" : "text-gray-200"}
        />
        <Stat label="Energy Needed" value={`${energy_needed_kwh.toFixed(1)} kWh`} />
      </div>

      {/* SOC bar */}
      <div className="space-y-1">
        <div className="flex justify-between text-xs text-gray-500">
          <span>Current: {current_soc_pct}%</span>
          <span>Target: {target_soc_pct}%</span>
        </div>
        <div className="relative h-2.5 rounded-full bg-gray-700 overflow-hidden">
          <div
            className="absolute left-0 top-0 h-full bg-blue-500 rounded-full"
            style={{ width: `${current_soc_pct}%` }}
          />
          <div
            className="absolute top-0 h-full w-0.5 bg-green-400"
            style={{ left: `${target_soc_pct}%` }}
          />
        </div>
      </div>

      {data.data_source === "mock" && (
        <p className="text-xs text-gray-600 italic">
          Using mock data — configure Enphase API key to use live solar production.
        </p>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  valueClass = "text-gray-200",
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-lg bg-gray-900 px-2 py-2">
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`text-base font-semibold mt-0.5 ${valueClass}`}>{value}</p>
    </div>
  );
}
