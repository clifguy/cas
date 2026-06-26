import { describe, it, expect } from 'vitest';
import { formatDate } from '../../utils/format';

/**
 * Unit tests for the calendar-date formatter.
 *
 * The contract: a YYYY-MM-DD calendar-date string renders as the same
 * authored date in every timezone. Implementations that route the string
 * through `new Date(str)` get this wrong — the ECMAScript spec parses the
 * bare date as UTC midnight, and `toLocaleDateString()` then shifts the
 * day for users in any zone west of UTC. The correct implementation parses
 * the components locally and constructs the Date in the local zone.
 *
 * `setup.ts` pins TZ=America/Chicago so the calendar-date-stability
 * assertions below are anti-coincidental — under TZ=UTC, a UTC-anchored
 * implementation would pass coincidentally.
 */
describe('formatDate', () => {
  it('renders YYYY-MM-DD as the same calendar date — does not UTC-shift', () => {
    // In America/Chicago a UTC-anchored implementation returns '5/14/2026';
    // the correct implementation returns '5/15/2026' in every zone.
    expect(formatDate('2026-05-15')).toBe('5/15/2026');
  });

  it('renders dates near boundaries without zone shift', () => {
    expect(formatDate('2026-01-01')).toBe('1/1/2026');
    expect(formatDate('2026-12-31')).toBe('12/31/2026');
    expect(formatDate('2026-03-01')).toBe('3/1/2026');
  });

  it('returns dash for null, undefined, and empty inputs', () => {
    expect(formatDate(null)).toBe('-');
    expect(formatDate(undefined)).toBe('-');
    expect(formatDate('')).toBe('-');
  });

  it('returns dash for malformed inputs (rejects partial dates and free-form strings)', () => {
    // `new Date('2026-05')` parses as May 1 UTC in JS — a permissive
    // formatter would silently render a misleading date. The strict
    // YYYY-MM-DD regex returns '-' instead.
    expect(formatDate('2026-05')).toBe('-');
    expect(formatDate('not-a-date')).toBe('-');
    expect(formatDate('2026/05/15')).toBe('-');
    expect(formatDate('05-15-2026')).toBe('-');
  });
});
