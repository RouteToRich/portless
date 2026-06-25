import { RadialBarChart, RadialBar, ResponsiveContainer } from "recharts";
import { cn } from "@/lib/utils";

interface ScoreRingProps {
  score: number | null | undefined;
  label: string;
  size?: "sm" | "md" | "lg";
}

function scoreColor(score: number): string {
  if (score >= 70) return "#2ec28a";
  if (score >= 40) return "#f5a623";
  return "#ff5d6c";
}

const SIZE_MAP = {
  sm: { container: 72, inner: "text-lg", sub: "text-[9px]" },
  md: { container: 96, inner: "text-2xl", sub: "text-[10px]" },
  lg: { container: 120, inner: "text-3xl", sub: "text-xs" },
};

export function ScoreRing({ score, label, size = "md" }: ScoreRingProps) {
  const s = SIZE_MAP[size];
  const value = score ?? 0;
  const color = scoreColor(value);

  const data = [
    {
      name: label,
      value,
      fill: color,
    },
  ];

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative" style={{ width: s.container, height: s.container }}>
        <ResponsiveContainer width="100%" height="100%">
          <RadialBarChart
            innerRadius="65%"
            outerRadius="100%"
            data={data}
            startAngle={90}
            endAngle={-270}
            barSize={8}
          >
            <RadialBar
              background={{ fill: "#1c2a47" }}
              dataKey="value"
              cornerRadius={4}
              max={100}
            />
          </RadialBarChart>
        </ResponsiveContainer>

        {/* Center label */}
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span
            className={cn("font-bold leading-none", s.inner)}
            style={{ color }}
          >
            {score == null ? "—" : Math.round(value)}
          </span>
        </div>
      </div>
      <span className={cn("text-muted-foreground font-medium text-center", s.sub)}>
        {label}
      </span>
    </div>
  );
}
