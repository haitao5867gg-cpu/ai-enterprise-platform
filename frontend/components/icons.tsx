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
  | "alert";
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
