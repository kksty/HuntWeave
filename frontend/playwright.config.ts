import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests', workers: 1, retries: 0, reporter: 'list', timeout: 90000,
  use: { baseURL: process.env.HUNTWEAVE_E2E_BASE_URL || 'http://127.0.0.1:8000', trace: 'off', screenshot: 'off', video: 'off' },
});
