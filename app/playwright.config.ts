import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  use: {
    baseURL: 'http://localhost:8000',
  },
  webServer: {
    command: 'cd .. && python -m sage sage/config.yaml',
    url: 'http://localhost:8000',
    reuseExistingServer: true,
  },
});
