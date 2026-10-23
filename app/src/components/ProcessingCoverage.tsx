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
          const selection = key === 'inspection' &&
            (dimension?.scope === 'complete' || dimension?.scope === 'limited')
            ? dimension.selection_summary : undefined;
          const countingRule = dimension?.identity?.tokenization ?? 'unknown';
          return <div key={key}>
            <dt>{label}</dt>
            <dd>{dimension?.scope.replace('_', ' ') ?? 'unknown'}
              {dimension?.generation != null && ` · generation ${dimension.generation}`}
              {dimension?.limitations?.length ? ` · ${dimension.limitations.join(', ')}` : ''}
              {dimension?.counts?.map(count => ` · ${count.represented}/${count.total ?? '?'} ${count.unit}`).join('')}
              {selection && <p>
                <span>Selection: {selection.outcome}</span>{' · '}
                <span>{selection.selected_inputs.toLocaleString()} {selection.selected_inputs === 1 ? 'excerpt' : 'excerpts'} selected</span>{' · '}
                <span>{selection.selected_tokens.toLocaleString()} {countingRule === 'utf8-byte-tokens:1'
                  ? 'UTF-8 bytes' : `size units (counting rule: ${countingRule})`}</span>{' · '}
                <span>{selection.associations_complete
                  ? `${selection.established_occurrences.toLocaleString()} established occurrences`
                  : `At least ${selection.established_occurrences.toLocaleString()} established occurrences (count incomplete)`}</span>{' · '}
                <span>Last inspected location: {selection.boundary ?? 'unknown'}</span>
              </p>}
            </dd>
          </div>;
        })}
      </dl>
      <p>Coverage describes established content. Pipeline success does not establish full-source coverage.</p>
    </details>
  );
}
