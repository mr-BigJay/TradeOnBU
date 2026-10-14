/** Prefer server-rendered Asia/Tehran strings; avoid re-parsing them in the browser. */
export function displayTehranTime(
  tehranLabel?: string | null,
  utcIso?: string | null,
): string {
  if (tehranLabel && tehranLabel.trim()) return tehranLabel;
  if (!utcIso) return "—";
  return new Date(utcIso).toLocaleString("fa-IR", {
    timeZone: "Asia/Tehran",
    dateStyle: "medium",
    timeStyle: "short",
  });
}
