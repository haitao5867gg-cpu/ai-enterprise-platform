import type { CSSProperties } from "react";

type IconName =
  | "grid"
  | "runs"
  | "plus"
  | "arrow"
  | "back"
  | "check"
  | "close"
  | "spark"
  | "refresh"
  | "download"
  | "alert"
  | "plug"
  | "folder"
  | "chip"
  | "user"
  | "log"
  | "bell"
  | "chart";
const paths: Record<IconName, string> = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  runs: "M5 4h14v16H5z M9 8h6 M9 12h6 M9 16h3",
  plus: "M12 5v14 M5 12h14",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  back: "M19 12H5 M11 6l-6 6 6 6",
  check: "m5 12 4 4L19 6",
  close: "m6 6 12 12 M18 6 6 18",
  spark: "m12 3 2.8 6.2L21 12l-6.2 2.8L12 21l-2.8-6.2L3 12l6.2-2.8z",
  refresh:
    "M20 7v5h-5 M4 17v-5h5 M6.1 7a7 7 0 0 1 11.6-1L20 9 M4 15l2.3 3A7 7 0 0 0 17.9 17",
  alert: "M12 3 2 21h20z M12 9v5 M12 17v1",
  download: "M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5",
  plug: "M9 3v5 M15 3v5 M6 8h12v4a6 6 0 0 1-12 0z M12 18v3",
  folder: "M3 6h6l2 2h10v11H3z",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4 21a8 8 0 0 1 16 0",
  log: "M6 3h9l4 4v14H6z M15 3v4h4 M9 12h7 M9 16h7",
  bell: "M6 17V11a6 6 0 0 1 12 0v6l2 2H4z M10 21h4",
  chart: "M4 20h16 M7 16v-5 M12 16V6 M17 16v-8",
  chip: "M7 7h10v10H7z M10 3v4 M14 3v4 M10 17v4 M14 17v4 M3 10h4 M3 14h4 M17 10h4 M17 14h4",
};

export function Icon({
  name,
  size = 18,
  style,
}: {
  name: IconName;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  );
}
