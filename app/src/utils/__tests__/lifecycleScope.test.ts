import { describe, expect, it } from 'vitest';
import {
  actionsForDocTypes,
  appliesTo,
  formatScope,
  parseScope,
  selectedDocTypes,
} from '../lifecycleScope';
import type { LifecycleTransitionConfig } from '../../api/types';

const t = (action: string, doc_types?: string[] | null): LifecycleTransitionConfig => ({
  from_state: 'active',
  action,
  to_state: 'archived',
  ...(doc_types === undefined ? {} : { doc_types }),
});

describe('parseScope / formatScope', () => {
  it('reads a blank field as unscoped, never as an empty list', () => {
    expect(parseScope('')).toBeNull();
    expect(parseScope('  ,  ')).toBeNull();
  });

  it('trims, drops empties and de-duplicates in first-seen order', () => {
    expect(parseScope(' ticket, adr ,ticket,, ')).toEqual(['ticket', 'adr']);
  });

  it('round-trips through formatScope', () => {
    expect(parseScope(formatScope(['ticket', 'adr']))).toEqual(['ticket', 'adr']);
    expect(formatScope(null)).toBe('');
    expect(formatScope(undefined)).toBe('');
  });
});

describe('appliesTo', () => {
  it('applies an unscoped entry to every doc_type, including none', () => {
    expect(appliesTo(undefined, 'adr')).toBe(true);
    expect(appliesTo(null, null)).toBe(true);
  });

  it('applies a scoped entry only to its doc_types', () => {
    expect(appliesTo(['ticket'], 'ticket')).toBe(true);
    expect(appliesTo(['ticket'], 'adr')).toBe(false);
    expect(appliesTo(['ticket'], null)).toBe(false);
  });
});

describe('actionsForDocTypes', () => {
  it('still excludes the actions the bulk dialog cannot collect arguments for', () => {
    const { offered } = actionsForDocTypes(
      [t('archive'), t('supersede'), t('relocate'), t('complete')],
      ['ticket'],
    );
    expect(offered).toEqual(['archive', 'complete']);
  });

  // The `(new)` row is the ingestion transition, which no lifecycle call can
  // invoke; the surviving action keeps an exclude-everything filter from passing.
  it('does not offer the ingestion transition', () => {
    const ingest: LifecycleTransitionConfig = { from_state: '(new)', action: 'ingest', to_state: 'active' };
    expect(actionsForDocTypes([ingest, t('archive')], ['ticket'])).toEqual({
      offered: ['archive'],
      hidden: [],
    });
  });

  it('offers an action when any transition carrying it applies to every selected doc_type', () => {
    // `complete` is scoped differently on two rows; together they cover both types.
    const transitions = [t('complete', ['ticket']), t('complete', ['adr']), t('archive')];
    expect(actionsForDocTypes(transitions, ['ticket', 'adr']).offered).toEqual([
      'complete',
      'archive',
    ]);
  });

  it('lists what it leaves out', () => {
    const result = actionsForDocTypes([t('archive'), t('complete', ['ticket'])], ['adr']);
    expect(result).toEqual({ offered: ['archive'], hidden: ['complete'] });
  });
});

describe('selectedDocTypes', () => {
  const docs = [
    { id: 'A', doc_type: 'ticket' },
    { id: 'B', doc_type: 'ticket' },
    { id: 'C', doc_type: 'adr' },
    { id: 'D', doc_type: null },
  ];

  it('reads the distinct doc_types of the selected rows only', () => {
    expect(selectedDocTypes(new Set(['A', 'B']), docs)).toEqual(['ticket']);
    expect(selectedDocTypes(['A', 'C', 'D'], docs)).toEqual(['ticket', 'adr', null]);
  });

  it('counts an id with no row as carrying no doc_type', () => {
    expect(selectedDocTypes(['A', 'gone'], docs)).toEqual(['ticket', null]);
  });
});
