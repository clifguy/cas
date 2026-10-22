import type { ProcessingCoverage as Coverage } from '../api/types';

const dimensions = [
  ['source_retention', 'Source retained'], ['inspection', 'Inspection'],
  ['projection', 'Readable projection'], ['keyword_indexing', 'Keyword index'],
  ['passage_embeddings', 'Passage embeddings'], ['abstraction_evidence', 'Abstraction evidence'],
] as const;

export function ProcessingCoverage({ coverage, compact = false }: { coverage?: Coverage; compact?: boolean }) {
  const scopes = coverage ? [...new Set(dimensions.map(([key]) => coverage[key].scope))].join(", ") : "unknown";
  return (
    <details aria-label="Processing coverage" style={{ fontSize: 12, marginTop: compact ? 4 : 8 }}>
      <summary>Processing coverage: {scopes}</summary>
      <dl>
        {dimensions.map(([key, label]) => {
          const dimension = coverage?.[key];
          return <div key={key}>
            <dt>{label}</dt>
            <dd>{dimension?.scope.replace('_', ' ') ?? 'unknown'}
              {dimension?.generation != null && ` · generation ${dimension.generation}`}
              {dimension?.limitations?.length ? ` · ${dimension.limitations.join(', ')}` : ''}
              {dimension?.counts?.map(count => ` · ${count.represented}/${count.total ?? '?'} ${count.unit}`).join('')}
            </dd>
          </div>;
        })}
      </dl>
      <p>Coverage describes established content. Pipeline success does not establish full-source coverage.</p>
    </details>
  );
}
