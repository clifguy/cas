import { describe, it, expect } from 'vitest';
import { pickEdgeEndpoint } from '../../utils/graph';

/**
 * Tests for the edge-click traversal helper.
 *
 * When the user clicks an edge in the graph, we need to decide which of the
 * edge's two endpoints becomes the new center node. The rule:
 *  - If the edge touches the current center, choose the opposite endpoint.
 *  - Otherwise (possible at depth >= 2), default to the edge's target
 *    so the direction cue carries the user "forward."
 *  - Self-loops return the same id (caller should treat this as no-op).
 */
describe('pickEdgeEndpoint', () => {
  it('returns the target when the current center is the source', () => {
    expect(pickEdgeEndpoint({ from: 'A', to: 'B' }, 'A')).toBe('B');
  });

  it('returns the source when the current center is the target', () => {
    expect(pickEdgeEndpoint({ from: 'A', to: 'B' }, 'B')).toBe('A');
  });

  it('returns the target when the current center is neither endpoint', () => {
    expect(pickEdgeEndpoint({ from: 'A', to: 'B' }, 'C')).toBe('B');
  });

  it('returns the target when the current center id is undefined', () => {
    expect(pickEdgeEndpoint({ from: 'A', to: 'B' }, undefined)).toBe('B');
  });

  it('returns the same id for a self-loop (caller should no-op)', () => {
    expect(pickEdgeEndpoint({ from: 'A', to: 'A' }, 'A')).toBe('A');
  });
});
