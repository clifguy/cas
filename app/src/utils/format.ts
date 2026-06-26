/** Format a byte count into a human-readable string (B / KB / MB). */
export function formatBytes(bytes: number): string {
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(1)} KB`;
  return `${bytes} B`;
}

// Parse YYYY-MM-DD as local calendar components, never via `new Date(str)`.
// `new Date('YYYY-MM-DD')` interprets the bare date as UTC midnight per the
// ECMAScript spec; rendering that instant via `.toLocaleDateString()` shifts
// the day for users in any zone west of UTC. Constructing the Date from
// numeric Y/M/D arguments builds it in the local zone, so the rendered date
// matches the authored calendar date in every timezone.
export function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  if (!m) return '-';
  const [, y, mo, d] = m;
  return new Date(Number(y), Number(mo) - 1, Number(d)).toLocaleDateString();
}
