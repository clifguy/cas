// Playwright e2e for the Settings → Maintenance panel (T-0117).
//
// Coverage scope (per the approved T-0117 plan):
//   - C1: the Maintenance tab is present in Settings for the cas vault.
//   - C2: with at least one abstraction_skipped fixture seeded, the panel
//         surfaces the count and the reabstract button is enabled.
//
// The full reabstract round-trip is NOT exercised here — Qwen3-MLX inference
// would dominate the wall-clock time and the MaintenancePanel.test.tsx
// component tests (mocked-API) cover the running and completion states.

import { test, expect } from '@playwright/test';

const VAULT_ID = 'cas';
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

test('maintenance panel shows deferred count and enables the reabstract button', async ({
  page,
}) => {
  // Precondition: the seed produced at least one tagged fixture doc in
  // abstraction_skipped. If this fails the rest of the test would pass
  // for the wrong reason (button-enabled assertion couldn't distinguish
  // a count-of-zero bug from a real-but-tiny deferred set).
  expect(await activeMaintenanceFixtureCount()).toBeGreaterThanOrEqual(1);

  // Land on dashboard and ensure cas is the active vault. (See the same
  // pattern in bulk-actions.spec.ts for the rationale: vault list sort
  // order is not stable so the default-vault choice depends on the data.)
  await page.goto('/dashboard');
  const vaultSelector = page.getByRole('combobox').first();
  await vaultSelector.waitFor({ state: 'visible' });
  if ((await vaultSelector.inputValue()) !== VAULT_ID) {
    await vaultSelector.selectOption(VAULT_ID);
    await expect(vaultSelector).toHaveValue(VAULT_ID);
  }

  // In-app navigation to Settings — pushState preserves React state
  // (page.goto would full-reload and reset activeVault back to vaults[0]).
  await page.evaluate(() => {
    window.history.pushState({}, '', '/settings');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });

  // C1: Maintenance tab exists and is selectable.
  const maintenanceTab = page.getByRole('button', { name: 'Maintenance' });
  await expect(maintenanceTab).toBeVisible();
  await maintenanceTab.click();

  // Panel mounts.
  await expect(page.getByTestId('maintenance-panel')).toBeVisible();

  // C2: count displayed and button enabled.
  // The cas vault may contain other abstraction_skipped docs unrelated to
  // this fixture (real CAS work that landed there), so we assert >= 1
  // rather than == 1.
  const countLocator = page.getByTestId('reabstract-count');
  await expect(countLocator).toBeVisible();
  await expect(countLocator).not.toHaveText('… deferred');
  const countText = (await countLocator.textContent()) ?? '';
  const count = parseInt(countText.match(/\d+/)?.[0] ?? '0', 10);
  expect(count).toBeGreaterThanOrEqual(1);

  await expect(page.getByTestId('reabstract-button')).toBeEnabled();
});
