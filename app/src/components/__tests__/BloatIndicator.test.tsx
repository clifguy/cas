// Vitest specs for the Postgres content-store bloat health indicator.
//
// Covers the dead-tuple-percentage signal: the pure helpers (deadTupleRatio,
// bloatState boundaries, reclaimableBytes) and the card's observable states —
// healthy (no remediation prompt), flagged (links to the Maintenance-page
// VACUUM action), and the free-space-does-not-alarm invariant.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import BloatIndicator from '../BloatIndicator';
import { bloatState, deadTupleRatio, reclaimableBytes } from '../../utils/bloat';

describe('deadTupleRatio', () => {
  it('returns 0 for an empty table (no divide-by-zero)', () => {
    expect(deadTupleRatio(0, 0)).toBe(0);
  });

  it('computes dead / (dead + live)', () => {
    expect(deadTupleRatio(20, 80)).toBeCloseTo(0.2, 10);
    expect(deadTupleRatio(50, 8469)).toBeCloseTo(0.00587, 4);
  });
});

describe('bloatState dead-tuple-percentage boundaries', () => {
  it('returns ok below 20%', () => {
    expect(bloatState(0, 100)).toBe('ok');
    expect(bloatState(19, 81)).toBe('ok');
  });

  it('returns warn from 20% through <40%', () => {
    expect(bloatState(20, 80)).toBe('warn');
    expect(bloatState(39, 61)).toBe('warn');
  });

  it('returns red at 40% and above', () => {
    expect(bloatState(40, 60)).toBe('red');
    expect(bloatState(100, 0)).toBe('red');
  });

  it('reads a healthy live-vault snapshot as ok (regression guard)', () => {
    // 50 dead tuples against 8,469 live chunks = 0.6% dead. The LanceDB-era
    // absolute thresholds (>=50 versions) false-flagged this exact case as red.
    expect(bloatState(50, 8469)).toBe('ok');
  });

  it('is ok when the table is empty', () => {
    expect(bloatState(0, 0)).toBe('ok');
  });
});

describe('reclaimableBytes', () => {
  it('multiplies free pages by the Postgres block size', () => {
    expect(reclaimableBytes(161)).toBe(161 * 8192); // 1,318,912
  });
});

function renderCard(deadTuples: number, liveChunks: number, freePages: number) {
  return render(
    <MemoryRouter>
      <BloatIndicator deadTuples={deadTuples} liveChunks={liveChunks} freePages={freePages} />
    </MemoryRouter>,
  );
}

describe('BloatIndicator card', () => {
  it('renders a healthy live-vault snapshot with the percentage, reclaimable figure, and no remediation link', () => {
    renderCard(50, 8469, 161);
    expect(screen.getByTestId('bloat-card')).toHaveAttribute('data-bloat-state', 'ok');
    expect(screen.getByTestId('bloat-dead-pct')).toHaveTextContent('0.6%');
    // 161 pages * 8192 = 1,318,912 bytes -> 1.3 MB humanized.
    expect(screen.getByTestId('bloat-reclaimable')).toHaveTextContent('1.3 MB');
    expect(screen.queryByRole('link', { name: /optimize/i })).toBeNull();
  });

  it('flags red and links to Maintenance when dead tuples reach 40%', () => {
    renderCard(40, 60, 0);
    expect(screen.getByTestId('bloat-card')).toHaveAttribute('data-bloat-state', 'red');
    expect(screen.getByTestId('bloat-dead-pct')).toHaveTextContent('40.0%');
    const link = screen.getByRole('link', { name: /optimize/i });
    expect(link).toHaveAttribute('href', '/maintenance');
  });

  it('does NOT alarm on free space alone (decoupled from the dead-tuple signal)', () => {
    // Tiny dead-tuple ratio (0.1%) but a large reclaimable figure: state stays ok.
    renderCard(1, 999, 100_000);
    expect(screen.getByTestId('bloat-card')).toHaveAttribute('data-bloat-state', 'ok');
    expect(screen.queryByRole('link', { name: /optimize/i })).toBeNull();
    // 100,000 pages * 8192 = 819,200,000 bytes -> 819.2 MB.
    expect(screen.getByTestId('bloat-reclaimable')).toHaveTextContent('819.2 MB');
  });
});
