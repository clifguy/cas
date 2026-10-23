import { render, screen } from '@testing-library/react';
import { ProcessingCoverage } from '../ProcessingCoverage';
import type { ProcessingCoverage as Coverage, SelectionSummary } from '../../api/types';

it('shows historical uncertainty and limited denominators without a percentage', () => {
  const unknown = { scope: 'unknown', generation: null, limitations: [], counts: [] } as const;
  const coverage = {
    attempt: 4,
    source_retention: { ...unknown, scope: 'complete', generation: 1 },
    inspection: unknown,
    projection: { scope: 'limited', generation: 3, limitations: ['budget'], counts: [
      { unit: 'characters', observed: 12, represented: 8, total: null },
    ] },
    keyword_indexing: unknown, passage_embeddings: unknown, abstraction_evidence: unknown,
  } as unknown as Coverage;
  const { container } = render(<ProcessingCoverage coverage={coverage} />);
  expect(screen.getByText('Processing coverage: complete, unknown, limited')).toBeInTheDocument();
  expect(screen.getByText(/limited · generation 3 · budget · 8\/\? characters/)).toBeInTheDocument();
  expect(screen.getByText(/complete · generation 1/)).toBeInTheDocument();
  expect(screen.getAllByText('unknown')).toHaveLength(4);
  expect(container.textContent).not.toContain('%');
});

it('labels missing historical evidence unknown', () => {
  render(<ProcessingCoverage compact />);
  expect(screen.getByText('Processing coverage: unknown')).toBeInTheDocument();
  expect(screen.getAllByText('unknown')).toHaveLength(6);
  expect(screen.queryByText(/^Selection:/)).not.toBeInTheDocument();
});
function inspectionCoverage(summary: SelectionSummary, tokenization = 'utf8-byte-tokens:1'): Coverage {
  const unknown = { scope: 'unknown' } as const;
  return {
    attempt: 5,
    source_retention: unknown,
    inspection: {
      scope: 'complete', generation: 5, unknown_remainder: false,
      counts: [{ unit: 'occurrences', observed: 10, represented: 10, total: 10 }],
      identity: { source_hash: `sha256:${'0'.repeat(64)}`, adapter: 'csv:1', policy: 'bounded-discovery:1', configuration: {}, tokenization },
      selection_summary: summary,
    },
    projection: unknown, keyword_indexing: unknown,
    passage_embeddings: unknown, abstraction_evidence: unknown,
  };
}

it('keeps complete inspection distinct from limited excerpt selection and uses byte sizing', () => {
  const coverage = inspectionCoverage({ outcome: 'limited', selected_inputs: 2, selected_tokens: 18, established_occurrences: 7, associations_complete: true, boundary: '/rows/9' });
  const { container } = render(<ProcessingCoverage coverage={coverage} />);
  expect(screen.getByText('Inspection').nextElementSibling).toHaveTextContent(/^complete/);
  expect(screen.getByText('Selection: limited')).toBeInTheDocument();
  expect(screen.getByText('2 excerpts selected')).toBeInTheDocument();
  expect(screen.getByText('18 UTF-8 bytes')).toBeInTheDocument();
  expect(screen.getByText('7 established occurrences')).toBeInTheDocument();
  expect(screen.getByText('Last inspected location: /rows/9')).toBeInTheDocument();
  expect(container.textContent).not.toContain('18 tokens');
  expect(container.textContent).not.toContain('%');
});

it('qualifies incomplete occurrence accounting and an unknown inspection boundary', () => {
  const coverage = inspectionCoverage({ outcome: 'limited', selected_inputs: 1, selected_tokens: 4, established_occurrences: 3, associations_complete: false, boundary: null });
  render(<ProcessingCoverage coverage={coverage} compact />);
  expect(screen.getByText('Selection: limited')).toBeInTheDocument();
  expect(screen.getByText('At least 3 established occurrences (count incomplete)')).toBeInTheDocument();
  expect(screen.getByText('Last inspected location: unknown')).toBeInTheDocument();
});


it('preserves the specified counting rule for other selection sizing', () => {
  const coverage = inspectionCoverage({ outcome: 'complete', selected_inputs: 1, selected_tokens: 4, established_occurrences: 1, associations_complete: true, boundary: '/body/0' }, 'words:1');
  render(<ProcessingCoverage coverage={coverage} />);
  expect(screen.getByText('Selection: complete')).toBeInTheDocument();
  expect(screen.getByText('4 size units (counting rule: words:1)')).toBeInTheDocument();
  expect(screen.queryByText(/UTF-8 bytes/)).not.toBeInTheDocument();
});


it('shows an interrupted selection without invented selected evidence', () => {
  const coverage = inspectionCoverage({ outcome: 'interrupted', selected_inputs: 0, selected_tokens: 0, established_occurrences: 0, associations_complete: true });
  render(<ProcessingCoverage coverage={coverage} />);
  expect(screen.getByText('Selection: interrupted')).toBeInTheDocument();
  expect(screen.getByText('0 excerpts selected')).toBeInTheDocument();
  expect(screen.getByText('0 UTF-8 bytes')).toBeInTheDocument();
  expect(screen.getByText('0 established occurrences')).toBeInTheDocument();
  expect(screen.getByText('Last inspected location: unknown')).toBeInTheDocument();
});
