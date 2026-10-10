import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  retries: 0,
  reporter: 'list',
  timeout: 30000,
  globalTimeout: 180000,
  maxFailures: 1,
  expect: { timeout: 5000 },
  use: {
    baseURL: process.env.HUNTWEAVE_E2E_BASE_URL || 'http://127.0.0.1:8000',
    storageState: { cookies: [], origins: [] },
    actionTimeout: 5000,
    navigationTimeout: 10000,
    trace: 'off', screenshot: 'off', video: 'off',
  },
});
