import type { LifecycleTransitionConfig } from '../api/types';

// A lifecycle state or transition may be scoped to named doc_types
// (CAS-ADR-054). An absent or null scope applies to every doc_type; a
// document carrying no doc_type falls only under unscoped entries.

// Actions the bulk dialog cannot offer: each needs a second argument it has
// no way to collect -- a successor id, and a relocation pointer at another
// vault -- so offering either would present an action that always fails.
const UNCOLLECTABLE_ACTIONS = new Set(['supersede', 'relocate']);

// The from_state of the ingestion transition: the row that places a new
// document in its landing state, which no lifecycle call can invoke.
const INGESTION_PSEUDO_STATE = '(new)';

export function appliesTo(scope: string[] | null | undefined, docType: string | null): boolean {
  if (scope == null) return true;
  return docType != null && scope.includes(docType);
}

// Every action some transition offers, split by whether it applies to all of
// the given doc_types. An action is offered only when each doc_type has a
// transition carrying it, so a mixed selection sees the intersection and no
// document in it meets `invalid_action`.
export function actionsForDocTypes(
  transitions: LifecycleTransitionConfig[],
  docTypes: (string | null)[],
): { offered: string[]; hidden: string[] } {
  const invocable = transitions.filter(
    (t) => t.from_state !== INGESTION_PSEUDO_STATE && !UNCOLLECTABLE_ACTIONS.has(t.action),
  );
  const actions = Array.from(new Set(invocable.map((t) => t.action)));
  const offered: string[] = [];
  const hidden: string[] = [];
  for (const action of actions) {
    const carriers = invocable.filter((t) => t.action === action);
    const everywhere = docTypes.every((dt) => carriers.some((t) => appliesTo(t.doc_types, dt)));
    (everywhere ? offered : hidden).push(action);
  }
  return { offered, hidden };
}

// Edited as a comma-separated list. A blank field means unscoped, never an
// empty list, which would scope the entry to no doc_type at all.
export function parseScope(text: string): string[] | null {
  const names = Array.from(new Set(text.split(',').map((s) => s.trim()).filter(Boolean)));
  return names.length > 0 ? names : null;
}

export function formatScope(scope: string[] | null | undefined): string {
  return scope == null ? '' : scope.join(', ');
}

// The distinct doc_types of the selected documents, read from the rows the
// view holds. An id with no row counts as carrying no doc_type, which admits
// only unscoped actions: an unknown document can narrow the offer, never
// widen it past what it would accept.
export function selectedDocTypes(
  selectedIds: Iterable<string>,
  documents: { id: string; doc_type?: string | null }[],
): (string | null)[] {
  const byId = new Map(documents.map((d) => [d.id, d.doc_type ?? null]));
  return Array.from(new Set(Array.from(selectedIds, (id) => byId.get(id) ?? null)));
}
