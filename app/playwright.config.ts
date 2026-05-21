import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  // Serialize specs on CI. Each spec's beforeAll hook calls the shared
  // seed_e2e_fixtures.py script, which is not parallel-safe: two
  // simultaneous archive-then-ingest cycles race and one trips a
  // duplicate_content 409 (observed locally under --workers=2). Developers
  // running the suite locally keep the default parallelism.
  workers: process.env.CI ? 1 : undefined,
  use: {
    baseURL: 'http://localhost:5173',
  },
  webServer: [
    {
      command: 'cd .. && .venv/bin/python -m sage',
      url: 'http://localhost:8000/sage_vaults',
      reuseExistingServer: true,
    },
    {
      command: 'npm run dev',
      url: 'http://localhost:5173',
      reuseExistingServer: true,
    },
  ],
});
