import { execSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

// CI-ONLY: the seed below writes to the real `cas` vault (ingest + archive of
// tag-scoped fixtures). CI runs this against a throwaway `cas` vault on a fresh
// runner; do not run e2e locally against a working vault. See
// fixtures/seed_e2e_fixtures.py.
export default async function globalSetup(): Promise<void> {
  const venvPython = resolve(HERE, '../../.venv/bin/python');
  const seed = resolve(HERE, 'fixtures/seed_e2e_fixtures.py');
  try {
    execSync(`${venvPython} ${seed}`, { stdio: 'inherit' });
  } catch (err) {
    throw new Error(
      `e2e seed failed (run \`${venvPython} ${seed}\` manually to debug). ` +
        `Underlying error: ${(err as Error).message}`,
    );
  }
}
