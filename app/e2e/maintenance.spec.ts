// Playwright e2e for the top-level Maintenance page.
//
// Coverage scope:
//   - C1: /maintenance is reachable via the sidebar nav entry.
//   - C2: with at least one abstraction_skipped fixture seeded, the
//         reabstract row surfaces the count and the button is enabled.
//   - C3: the optimize-content-store row is present and the operation
//         can be triggered end-to-end (POST stubbed with page.route()).
//
// The full reabstract round-trip is NOT exercised here — Qwen3-MLX inference
// would dominate the wall-clock time and the MaintenancePanel.test.tsx
// component tests (mocked-API) cover the running and completion states.
//
// The fixture lives in the smoke-test vault so the reabstract worklist on
// cas stays free of test sediment (SAGE has no hard-delete).

import { test, expect } from '@playwright/test';

const VAULT_ID = 'test';
const BACKEND = 'http://localhost:8000';
const MAINTENANCE_FIXTURE_TAG = 'e2e-maintenance-fixture';

async function activeMaintenanceFixtureCount(): Promise<number> {
  const res = await fetch(`${BACKEND}/sage_vaults/${VAULT_ID}/discover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'catalog',
      filters: {
        tags: [MAINTENANCE_FIXTURE_TAG],
        lifecycle_status: 'active',
        pipeline_status: 'abstraction_skipped',
      },
      limit: 100,
    }),
  });
  if (!res.ok) return 0;
  const body = (await res.json()) as { total_available: number };
  return body.total_available;
}

test('maintenance page surfaces reabstract count and is reachable via the sidebar', async ({
  page,
}) => {
  // Precondition: the seed produced at least one tagged fixture doc in
  // abstraction_skipped. If this fails the rest of the test would pass
  // for the wrong reason (button-enabled assertion couldn't distinguish
  // a count-of-zero bug from a real-but-tiny deferred set).
  expect(await activeMaintenanceFixtureCount()).toBeGreaterThanOrEqual(1);

  // Land on dashboard and ensure the maintenance vault is active. (See
  // the same pattern in bulk-actions.spec.ts for the rationale: vault list
  // sort order is not stable so the default-vault choice depends on the
  // data.)
  await page.goto('/dashboard');
  const vaultSelector = page.getByRole('combobox').first();
  await vaultSelector.waitFor({ state: 'visible' });
  if ((await vaultSelector.inputValue()) !== VAULT_ID) {
    await vaultSelector.selectOption(VAULT_ID);
    await expect(vaultSelector).toHaveValue(VAULT_ID);
  }

  // C1: Maintenance nav entry exists in the sidebar and routes to /maintenance.
  const maintenanceNav = page.getByRole('link', { name: 'Maintenance' });
  await expect(maintenanceNav).toBeVisible();
  await maintenanceNav.click();
  await expect(page).toHaveURL(/\/maintenance$/);
  await expect(page.getByTestId('maintenance-panel')).toBeVisible();

  // C2: reabstract row present, count surfaced, button enabled.
  // The maintenance vault may contain abstraction_skipped docs unrelated
  // to this fixture across runs (SAGE has no hard-delete), so we assert
  // >= 1 rather than == 1.
  const countLocator = page.getByTestId('reabstract-count');
  await expect(countLocator).toBeVisible();
  await expect(countLocator).not.toHaveText('… deferred');
  const countText = (await countLocator.textContent()) ?? '';
  const count = parseInt(countText.match(/\d+/)?.[0] ?? '0', 10);
  expect(count).toBeGreaterThanOrEqual(1);

  await expect(page.getByTestId('reabstract-button')).toBeEnabled();
});

test('optimize-content-store row triggers the backend and renders the report', async ({
  page,
}) => {
  // C3: stub the optimize endpoint so the test doesn't actually touch
  // the vector database. A recognizable bytes_reclaimed value (2_500_000 -> "2.5 MB"
  // humanized) makes a route mismatch (stub never fires) loudly visible.
  //
  // The API client uses relative paths that Vite proxies to the FastAPI
  // backend, so the browser-level request URL is the Vite host
  // (localhost:5173), not BACKEND. Use a glob to match either host.
  await page.route(`**/sage_vaults/${VAULT_ID}/admin/optimize-content-store`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        vault_id: VAULT_ID,
        cleanup_older_than_days: 7,
        started_at: '2026-05-28T12:00:00Z',
        finished_at: '2026-05-28T12:00:05Z',
        pre_bytes: 10_000,
        post_bytes: 1,
        bytes_reclaimed: 2_500_000,
        pre_versions: 20,
        post_versions: 17,
        pre_fragments: 30,
        post_fragments: 28,
        pre_small_fragments: 5,
        post_small_fragments: 2,
      }),
    });
  });

  await page.goto('/dashboard');
  const vaultSelector = page.getByRole('combobox').first();
  await vaultSelector.waitFor({ state: 'visible' });
  if ((await vaultSelector.inputValue()) !== VAULT_ID) {
    await vaultSelector.selectOption(VAULT_ID);
    await expect(vaultSelector).toHaveValue(VAULT_ID);
  }

  await page.getByRole('link', { name: 'Maintenance' }).click();
  await expect(page.getByTestId('optimize-operation')).toBeVisible();

  // Default value (7) is already pre-filled; click through.
  await page.getByTestId('optimize-button').click();
  await page.getByTestId('optimize-confirm-apply').click();

  await expect(page.getByTestId('optimize-summary')).toBeVisible();
  // Reclaimed bytes render humanized (2.5 MB), not as a raw integer.
  await expect(page.getByTestId('optimize-bytes-reclaimed')).toContainText('2.5 MB');
  await expect(page.getByTestId('optimize-bytes-reclaimed')).not.toContainText('2500000');
});
