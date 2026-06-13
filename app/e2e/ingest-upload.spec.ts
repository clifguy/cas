// Playwright e2e for the cloud (hosted-profile) bulk-ingest upload flow.
//
// Coverage scope:
//   - F1: with ?profile=hosted forcing the upload affordance, a file picked in
//         the browser uploads to the SAGE batch endpoint, streams progress, and
//         the document lands in the vault.
//   - F2: with no override, profile detection defaults to the directory-scan UI
//         in the co-located e2e harness.
//
// The CI harness boots co-located SAGE (python -m sage), so profile detection
// would otherwise show the directory UI. The ?profile=hosted URL override
// renders the upload affordance; the SAGE documents:batch endpoint exists in
// co-located too, so the upload genuinely ingests. The fixture lands in the
// smoke-test vault so the cas vault stays free of test sediment (SAGE has no
// hard-delete). Upload content is unique per run because SAGE dedups on content
// hash, so a stable fixture would raise DuplicateContentError on the second run.

import { test, expect } from '@playwright/test';

const VAULT_ID = 'test';
const BACKEND = 'http://localhost:8000';

async function activeDocCount(): Promise<number> {
  const res = await fetch(`${BACKEND}/sage_vaults/${VAULT_ID}/discover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'catalog',
      filters: { lifecycle_status: 'active' },
      limit: 1,
    }),
  });
  if (!res.ok) return -1;
  const body = (await res.json()) as { total_available: number };
  return body.total_available;
}

test('F1: cloud upload affordance uploads a file and ingests it via the batch endpoint', async ({
  page,
}) => {
  const before = await activeDocCount();
  expect(before).toBeGreaterThanOrEqual(0);

  // Land on dashboard and make the smoke-test vault active. (Vault-list sort
  // order is not stable, so the default-vault choice depends on the data; see
  // bulk-actions.spec.ts / maintenance.spec.ts for the same pattern.)
  await page.goto('/dashboard');
  const vaultSelector = page.getByRole('combobox').first();
  await vaultSelector.waitFor({ state: 'visible' });
  if ((await vaultSelector.inputValue()) !== VAULT_ID) {
    await vaultSelector.selectOption(VAULT_ID);
    await expect(vaultSelector).toHaveValue(VAULT_ID);
  }

  // In-app navigation to Ingest with the hosted-profile override. Uses
  // history.pushState (not page.goto) so React state — the selected vault —
  // survives the URL change; a full reload would reset activeVault to vaults[0].
  await page.evaluate(() => {
    window.history.pushState({}, '', '/ingest?profile=hosted');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });

  // The hosted upload affordance renders (not the directory input).
  const fileInput = page.getByTestId('upload-file-input');
  await expect(fileInput).toBeVisible();

  // Unique content per run avoids the content-hash dedup.
  const unique = `e2e-upload-${Date.now()}`;
  await fileInput.setInputFiles({
    name: `${unique}.md`,
    mimeType: 'text/markdown',
    buffer: Buffer.from(`# ${unique}\n\nBody for ${unique}.\n`),
  });

  // Step 2 preview → upload the single supported file.
  await page.getByRole('button', { name: /upload selected \(1\)/i }).click();

  // Step 3: the per-file progress row and the results summary render.
  await expect(page.getByText(new RegExp(`\\[completed\\] ${unique}\\.md`))).toBeVisible();
  await expect(page.getByText('Results Summary')).toBeVisible();
  await expect(page.getByText(/1 new/)).toBeVisible();

  // The document actually landed in the vault.
  await expect.poll(async () => activeDocCount()).toBe(before + 1);
});

test('F2: with no override, detection defaults to the directory-scan UI', async ({ page }) => {
  await page.goto('/dashboard');
  const vaultSelector = page.getByRole('combobox').first();
  await vaultSelector.waitFor({ state: 'visible' });
  if ((await vaultSelector.inputValue()) !== VAULT_ID) {
    await vaultSelector.selectOption(VAULT_ID);
    await expect(vaultSelector).toHaveValue(VAULT_ID);
  }

  await page.evaluate(() => {
    window.history.pushState({}, '', '/ingest');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });

  await expect(page.getByPlaceholder('/path/to/source/directory')).toBeVisible();
  await expect(page.getByTestId('upload-file-input')).toHaveCount(0);
});
