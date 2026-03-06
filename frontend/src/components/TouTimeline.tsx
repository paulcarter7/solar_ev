import type { TouPeriod } from "../api/solar";

interface Props {
  schedule: TouPeriod[];
}

function hourToPercent(timeStr: string): number {
  if (timeStr === "24:00") return 100;
  const [h, m] = timeStr.split(":").map(Number);
  return ((h + (m ?? 0) / 60) / 24) * 100;
}

export function TouTimeline({ schedule }: Props) {
  // Get the current hour as a percentage for the "now" marker
  const nowPct = (new Date().getHours() + new Date().getMinutes() / 60) / 24 * 100;

  return (
    <div className="space-y-3">
      {/* Bar */}
      <div className="relative h-8 rounded-lg overflow-hidden bg-gray-800 flex">
        {schedule.map((period, i) => {
          const left = hourToPercent(period.start);
          const right = hourToPercent(period.end);
          const width = right - left;
          return (
            <div
              key={i}
              className="absolute top-0 h-full flex items-center justify-center text-xs font-semibold text-gray-900 overflow-hidden"
              style={{ left: `${left}%`, width: `${width}%`, backgroundColor: period.color }}
              title={`${period.label}: ${period.start}–${period.end} @ $${period.rate_kwh}/kWh`}
            >
              {width > 6 ? period.label.split(" ")[0] : ""}
            </div>
          );
        })}
        {/* Now marker */}
        <div
          className="absolute top-0 h-full w-0.5 bg-white opacity-90 z-10"
          style={{ left: `${nowPct}%` }}
          title={`Now (${new Date().getHours()}:${String(new Date().getMinutes()).padStart(2, "0")})`}
        />
      </div>

      {/* Hour labels */}
      <div className="relative h-4">
        {[0, 6, 9, 12, 14, 16, 21, 24].map((h) => (
          <span
            key={h}
            className="absolute text-xs text-gray-500 -translate-x-1/2"
            style={{ left: `${(h / 24) * 100}%` }}
          >
            {h === 24 ? "12a" : h === 0 ? "12a" : h < 12 ? `${h}a` : h === 12 ? "12p" : `${h - 12}p`}
          </span>
        ))}
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-4 mt-1">
        {[
          { label: "Super Off-Peak", color: "#22c55e", rate: "$0.17" },
          { label: "Off-Peak", color: "#f59e0b", rate: "$0.28" },
          { label: "Peak", color: "#ef4444", rate: "$0.48" },
        ].map((item) => (
          <div key={item.label} className="flex items-center gap-1.5 text-xs text-gray-400">
            <span className="inline-block w-3 h-3 rounded-sm" style={{ backgroundColor: item.color }} />
            {item.label}
            <span className="text-gray-500">{item.rate}/kWh</span>
          </div>
        ))}
      </div>
    </div>
  );
}
