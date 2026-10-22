import { render, screen } from '@testing-library/react';
import { ProcessingCoverage } from '../ProcessingCoverage';
import type { ProcessingCoverage as Coverage } from '../../api/types';

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
});
