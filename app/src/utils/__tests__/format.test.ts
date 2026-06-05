// Vitest specs for the shared byte formatter.

import { describe, it, expect } from 'vitest';
import { formatBytes } from '../format';

describe('formatBytes', () => {
  it('formats megabytes with one decimal', () => {
    expect(formatBytes(169_379_435)).toBe('169.4 MB');
    expect(formatBytes(2_500_000)).toBe('2.5 MB');
  });

  it('formats kilobytes with one decimal', () => {
    expect(formatBytes(1234)).toBe('1.2 KB');
  });

  it('formats sub-kilobyte values as bytes', () => {
    expect(formatBytes(500)).toBe('500 B');
    expect(formatBytes(0)).toBe('0 B');
  });
});
