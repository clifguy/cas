// Names the constraints a search request applies, for display.

import type { DiscoverRequest } from '../api/types';

type Filters = NonNullable<DiscoverRequest['filters']>;

export interface Constraint {
  key: string;
  label: string;
  value: string;
}

// Wording only. A key missing from this table is still named, under the
// key itself -- membership comes from the request, never from here.
const LABELS: Record<string, string> = {
  doc_type: 'Document type',
  lifecycle_status: 'Lifecycle state',
  project: 'Project',
  tags: 'Tags',
  pipeline_status: 'Pipeline status',
  exclude_terminal_lifecycle: 'Excluding terminal lifecycle states',
};

// Identifier-valued keys render with underscores as spaces, the form the
// doc-type heading and the project select already use. Tags are literal
// values and an unknown key has no display convention, so both render as
// given.
const SPACED_VALUES = new Set(['doc_type', 'lifecycle_status', 'project', 'pipeline_status']);

export function describeConstraints(filters: Filters | undefined): Constraint[] {
  if (!filters) return [];
  const out: Constraint[] = [];
  for (const [key, raw] of Object.entries(filters)) {
    // Values that narrow nothing are not constraints.
    if (raw === undefined || raw === null || raw === false || raw === '') continue;
    if (Array.isArray(raw) && raw.length === 0) continue;
    let value = Array.isArray(raw) ? raw.join(', ') : raw === true ? '' : String(raw);
    if (SPACED_VALUES.has(key)) value = value.replace(/_/g, ' ');
    out.push({ key, label: LABELS[key] ?? key, value });
  }
  return out;
}
