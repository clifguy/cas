import { execSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';

const SPEC_DIR = dirname(fileURLToPath(import.meta.url));

const VAULT_ID = 'cas';
const FIXTURE_TAG = 'e2e-bulk-fixture';
const BACKEND = 'http://localhost:8000';

async function activeFixtureCount(): Promise<number> {
  const res = await fetch(`${BACKEND}/sage_vaults/${VAULT_ID}/discover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'catalog',
      filters: { tags: [FIXTURE_TAG], lifecycle_status: 'active' },
      limit: 100,
    }),
  });
  if (!res.ok) return 0;
  const body = (await res.json()) as { results: unknown[] };
  return body.results.length;
}

async function archivedFixtureCount(): Promise<number> {
  const res = await fetch(`${BACKEND}/sage_vaults/${VAULT_ID}/discover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'catalog',
      filters: { tags: [FIXTURE_TAG], lifecycle_status: 'archived' },
      limit: 100,
    }),
  });
  if (!res.ok) return 0;
  const body = (await res.json()) as { results: unknown[] };
  return body.results.length;
}

test.beforeAll(() => {
  const venvPython = resolve(SPEC_DIR, '../../.venv/bin/python');
  const seed = resolve(SPEC_DIR, 'fixtures/seed_e2e_fixtures.py');
  try {
    execSync(`${venvPython} ${seed}`, { stdio: 'inherit' });
  } catch (err) {
    throw new Error(
      `e2e seed failed (run \`${venvPython} ${seed}\` manually to debug). Underlying error: ${(err as Error).message}`,
    );
  }
});

test('bulk lifecycle round-trip in Search archives three documents', async ({ page }) => {
  // Precondition: seed has placed exactly 3 active fixture-tagged docs in cas.
  expect(await activeFixtureCount()).toBe(3);
  const baselineArchived = await archivedFixtureCount();

  // Ensure the cas vault is active. The default-vault choice depends on
  // alphabetical order of the SAGE-served vault list, so explicitly select
  // the cas vault if Layout landed somewhere else.
  await page.goto('/dashboard');
  const vaultSelector = page.getByRole('combobox').first();
  await vaultSelector.waitFor({ state: 'visible' });
  if ((await vaultSelector.inputValue()) !== VAULT_ID) {
    await vaultSelector.selectOption(VAULT_ID);
  }

  // Navigate to Search with the fixture tag URL filter so only the three
  // seeded rows are visible.
  await page.goto(`/search?mode=browse&tags=${FIXTURE_TAG}`);

  // Wait for the three fixture documents.
  await expect(page.getByRole('link', { name: 'e2e-doc-1' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'e2e-doc-2' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'e2e-doc-3' })).toBeVisible();

  // The tag filter must produce exactly 3 rows — coincidental-pass guard:
  // if the filter regresses, the cas vault's normal active doc set would
  // dwarf 3 and this assertion would fail loudly.
  await expect(page.getByRole('link', { name: /^e2e-doc-\d$/ })).toHaveCount(3);

  // Click the header select-all.
  await page.getByTestId('bulk-select-all').check();

  // BulkActionBar shows count.
  await expect(page.getByTestId('bulk-action-bar-count')).toHaveText(/3 selected/);

  // Open lifecycle dialog and pick archive.
  await page.getByRole('button', { name: /set lifecycle/i }).click();
  await page.getByTestId('bulk-lifecycle-action').selectOption('archive');
  await page.getByTestId('bulk-lifecycle-apply').click();

  // Results panel shows 3/0.
  await expect(page.getByTestId('bulk-lifecycle-results-summary')).toContainText(/3 succeeded, 0 failed/);
  await page.getByRole('button', { name: /^close$/i }).click();

  // Reload and verify all three are now archived (tag-filtered counts).
  await page.reload();
  expect(await activeFixtureCount()).toBe(0);
  expect(await archivedFixtureCount()).toBe(baselineArchived + 3);
});
