// Vitest specs for the content-store bloat health indicator.
//
// Covers the two self-calibrating signals (retained versions + small
// fragments) composed worst-of: the pure threshold helper (per-signal
// boundaries and worst-of composition) and the card's two observable states —
// healthy (no remediation prompt) and flagged (links to the Maintenance
// page's optimize action), including the fragment-only flag.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import BloatIndicator, { bloatState } from '../BloatIndicator';

describe('bloatState version-count boundaries (small fragments clean)', () => {
  it('returns ok below 20', () => {
    expect(bloatState(0, 0)).toBe('ok');
    expect(bloatState(19, 0)).toBe('ok');
  });

  it('returns warn from 20 through 49', () => {
    expect(bloatState(20, 0)).toBe('warn');
    expect(bloatState(49, 0)).toBe('warn');
  });

  it('returns red at 50 and above', () => {
    expect(bloatState(50, 0)).toBe('red');
    expect(bloatState(200, 0)).toBe('red');
  });
});

describe('bloatState small-fragment boundaries (versions clean)', () => {
  it('returns ok below 10', () => {
    expect(bloatState(0, 9)).toBe('ok');
  });

  it('returns warn from 10 through 24', () => {
    expect(bloatState(0, 10)).toBe('warn');
    expect(bloatState(0, 24)).toBe('warn');
  });

  it('returns red at 25 and above', () => {
    expect(bloatState(0, 25)).toBe('red');
    expect(bloatState(0, 50)).toBe('red');
  });
});

describe('bloatState worst-of composition', () => {
  it('takes the worse signal regardless of which one is elevated', () => {
    expect(bloatState(0, 25)).toBe('red'); // (ok, red) -> red
    expect(bloatState(50, 0)).toBe('red'); // (red, ok) -> red
    expect(bloatState(20, 0)).toBe('warn'); // (warn, ok) -> warn
    expect(bloatState(0, 10)).toBe('warn'); // (ok, warn) -> warn
    expect(bloatState(0, 0)).toBe('ok'); // (ok, ok) -> ok
  });
});

function renderCard(versionCount: number, smallFragmentCount: number) {
  return render(
    <MemoryRouter>
      <BloatIndicator versionCount={versionCount} smallFragmentCount={smallFragmentCount} />
    </MemoryRouter>,
  );
}

describe('BloatIndicator card', () => {
  it('renders a healthy card with both counts and no remediation link', () => {
    renderCard(3, 2);
    expect(screen.getByTestId('bloat-card')).toHaveAttribute('data-bloat-state', 'ok');
    expect(screen.getByTestId('bloat-version-count')).toHaveTextContent('3');
    expect(screen.getByTestId('bloat-small-fragment-count')).toHaveTextContent('2');
    expect(screen.queryByRole('link', { name: /optimize/i })).toBeNull();
  });

  it('flags red on the fragment signal alone (versions still healthy)', () => {
    renderCard(3, 30);
    expect(screen.getByTestId('bloat-card')).toHaveAttribute('data-bloat-state', 'red');
    expect(screen.getByTestId('bloat-version-count')).toHaveTextContent('3');
    expect(screen.getByTestId('bloat-small-fragment-count')).toHaveTextContent('30');
    const link = screen.getByRole('link', { name: /optimize/i });
    expect(link).toHaveAttribute('href', '/maintenance');
  });
});
