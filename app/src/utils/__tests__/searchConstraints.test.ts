// Vitest specs for naming the constraints a search request applies.

import { describe, it, expect } from 'vitest';
import { describeConstraints } from '../searchConstraints';

describe('describeConstraints', () => {
  it('names a filter it has no label for under its own key', () => {
    // The label table is wording, not membership. A filter added to the
    // request without a matching label has to reach the screen anyway,
    // or the table becomes the hand-maintained list that let a filter
    // narrow results invisibly in the first place.
    expect(describeConstraints({ future_filter: 'x_y' } as never)).toEqual([
      { key: 'future_filter', label: 'future_filter', value: 'x_y' },
    ]);
  });

  it('shapes array and boolean values, and labels known keys', () => {
    // Identifier values read with spaces, as the doc-type heading and the
    // project select show them; a tag is a literal and keeps its underscore.
    expect(
      describeConstraints({
        tags: ['a_b', 'c'],
        exclude_terminal_lifecycle: true,
        doc_type: 'reference_document',
      }),
    ).toEqual([
      { key: 'tags', label: 'Tags', value: 'a_b, c' },
      {
        key: 'exclude_terminal_lifecycle',
        label: 'Excluding terminal lifecycle states',
        value: '',
      },
      { key: 'doc_type', label: 'Document type', value: 'reference document' },
    ]);
  });

  it('omits values that do not narrow the result', () => {
    expect(describeConstraints({ tags: [], exclude_terminal_lifecycle: false })).toEqual([]);
    expect(describeConstraints(undefined)).toEqual([]);
  });
});
