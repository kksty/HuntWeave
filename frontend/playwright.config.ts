import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests', workers: 1, retries: 0, reporter: 'list',
  use: { baseURL: process.env.HUNTWEAVE_E2E_BASE_URL || 'http://127.0.0.1:18000', trace: 'off', screenshot: 'off', video: 'off' },
});
