// Decide which endpoint becomes the new center when the user clicks an edge.
// If the edge touches the current center, return the opposite endpoint.
// Otherwise default to the target, preserving the arrow direction as a cue.
export function pickEdgeEndpoint(
  edge: { from: string; to: string },
  currentCenterId: string | undefined,
): string {
  if (edge.from === currentCenterId) return edge.to;
  if (edge.to === currentCenterId) return edge.from;
  return edge.to;
}
