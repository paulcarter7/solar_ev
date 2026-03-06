import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import type { HourlyReading, TouPeriod } from "../api/solar";

interface Props {
  readings: HourlyReading[];
  touSchedule: TouPeriod[];
}

// Convert "HH:MM" string to decimal hours for comparison
function timeToHour(t: string): number {
  const [h, m] = t.split(":").map(Number);
  return h + (m ?? 0) / 60;
}

// Return the local (browser) hour (0-23) for a UTC ISO-8601 timestamp
function localHourOf(timestamp: string): number {
  return new Date(timestamp).getHours();
}

function rateAtHour(hour: number, schedule: TouPeriod[]): { rate: number; color: string; label: string } {
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

interface ChartRow {
  hour: string;       // "HH:00" in local time — used as Recharts dataKey
  localHour: number;  // 0-23, used for sorting
  production_kwh: number;
  rate: number;
  rateColor: string;
  rateLabel: string;
  power_w: number;
  source: string;
}

export function SolarChart({ readings, touSchedule }: Props) {
  // Convert each UTC timestamp to the browser's local hour so the chart
  // always runs 00:00→23:00 in the user's timezone (not UTC).
  const data: ChartRow[] = readings
    .map((r) => {
      const lh = localHourOf(r.timestamp);
      const { rate, color, label } = rateAtHour(lh, touSchedule);
      return {
        hour: `${lh.toString().padStart(2, "0")}:00`,
        localHour: lh,
        production_kwh: r.production_wh / 1000,
        rate,
        rateColor: color,
        rateLabel: label,
        power_w: r.power_w ?? 0,
        source: r.source,
      };
    })
    .sort((a, b) => a.localHour - b.localHour);

  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="solarGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#facc15" stopOpacity={0.4} />
            <stop offset="95%" stopColor="#facc15" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
        <XAxis
          dataKey="hour"
          tick={{ fill: "#9ca3af", fontSize: 11 }}
          interval={2}
          tickLine={false}
        />
        <YAxis
          tick={{ fill: "#9ca3af", fontSize: 11 }}
          tickFormatter={(v: number) => `${v.toFixed(1)}`}
          unit=" kW"
          width={52}
        />
        <Tooltip
          contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", borderRadius: 8 }}
          labelStyle={{ color: "#f9fafb" }}
          formatter={(value: number, _name: string, props: { payload?: ChartRow }) => {
            const row = props.payload;
            if (!row) return [`${value.toFixed(2)} kWh`, "Solar"];
            const powerStr = row.power_w > 0 ? `  ·  ${row.power_w} W` : "";
            const sourceStr = row.source === "no_data" ? "  ·  no data" : "";
            return [
              `${value.toFixed(2)} kWh${powerStr}${sourceStr}  (${row.rateLabel} @ $${row.rate.toFixed(2)}/kWh)`,
              "Solar",
            ];
          }}
        />
        {/* Shade peak hours */}
        <ReferenceLine x="16:00" stroke="#ef4444" strokeDasharray="4 2" label={{ value: "Peak →", fill: "#ef4444", fontSize: 10 }} />
        <ReferenceLine x="21:00" stroke="#f59e0b" strokeDasharray="4 2" />
        <Area
          type="monotone"
          dataKey="production_kwh"
          stroke="#facc15"
          strokeWidth={2}
          fill="url(#solarGrad)"
          dot={false}
          activeDot={{ r: 4, fill: "#facc15" }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
