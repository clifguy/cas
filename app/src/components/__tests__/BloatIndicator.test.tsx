// Vitest specs for the content-store bloat health indicator.
//
// Covers the self-calibrating retained-version signal: the pure threshold
// helper (ok/warn/red boundaries) and the card's two observable states —
// healthy (no remediation prompt) and flagged (links to the Maintenance
// page's optimize action).

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import BloatIndicator, { bloatState } from '../BloatIndicator';

describe('bloatState thresholds', () => {
  it('returns ok below 20', () => {
    expect(bloatState(0)).toBe('ok');
    expect(bloatState(19)).toBe('ok');
  });

  it('returns warn from 20 through 49', () => {
    expect(bloatState(20)).toBe('warn');
    expect(bloatState(49)).toBe('warn');
  });

  it('returns red at 50 and above', () => {
    expect(bloatState(50)).toBe('red');
    expect(bloatState(200)).toBe('red');
  });
});

function renderCard(versionCount: number) {
  return render(
    <MemoryRouter>
      <BloatIndicator versionCount={versionCount} />
    </MemoryRouter>,
  );
}

describe('BloatIndicator card', () => {
  it('renders a healthy card with no remediation link', () => {
    renderCard(3);
    expect(screen.getByTestId('bloat-card')).toHaveAttribute('data-bloat-state', 'ok');
    expect(screen.getByTestId('bloat-count')).toHaveTextContent('3');
    expect(screen.queryByRole('link', { name: /optimize/i })).toBeNull();
  });

  it('renders a flagged card linking to the maintenance optimize action', () => {
    renderCard(60);
    expect(screen.getByTestId('bloat-card')).toHaveAttribute('data-bloat-state', 'red');
    expect(screen.getByTestId('bloat-count')).toHaveTextContent('60');
    const link = screen.getByRole('link', { name: /optimize/i });
    expect(link).toHaveAttribute('href', '/maintenance');
  });
});
