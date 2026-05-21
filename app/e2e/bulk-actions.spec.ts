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
  // sort order of the SAGE-served vault list, so explicitly select the
  // cas vault if Layout landed somewhere else. The subsequent navigation
  // uses history.pushState (not page.goto) so React state survives the URL
  // change — page.goto would full-reload and reset activeVault back to
  // vaults[0].
  await page.goto('/dashboard');
  const vaultSelector = page.getByRole('combobox').first();
  await vaultSelector.waitFor({ state: 'visible' });
  if ((await vaultSelector.inputValue()) !== VAULT_ID) {
    await vaultSelector.selectOption(VAULT_ID);
    await expect(vaultSelector).toHaveValue(VAULT_ID);
  }

  // In-app navigation to Search with the fixture tag URL filter scoped to
  // active docs. Archived fixture-tagged predecessors accumulate over time
  // (per T-0130 design notes) so an active filter is required to keep the
  // row set deterministic.
  await page.evaluate((url) => {
    window.history.pushState({}, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, `/search?mode=browse&tags=${FIXTURE_TAG}&lifecycle_status=active`);

  // Wait for the three fixture documents. SAGE may rename the rendered
  // source filename with a hash suffix (e.g., `e2e-doc-1_abc12345`) when
  // archived predecessors hold the original filename — accept both forms.
  const fixtureLink = (n: number) =>
    page.getByRole('link', { name: new RegExp(`^e2e-doc-${n}(_[a-f0-9]+)?$`) });
  await expect(fixtureLink(1)).toBeVisible();
  await expect(fixtureLink(2)).toBeVisible();
  await expect(fixtureLink(3)).toBeVisible();

  // The tag+active filter must produce exactly 3 rows — coincidental-pass
  // guard: if the filter regresses, the cas vault's normal active doc set
  // would dwarf 3 and this assertion would fail loudly.
  await expect(
    page.getByRole('link', { name: /^e2e-doc-\d(_[a-f0-9]+)?$/ }),
  ).toHaveCount(3);

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
