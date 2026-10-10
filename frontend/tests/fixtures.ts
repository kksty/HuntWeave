import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test as base, type Page } from '@playwright/test';

const here = dirname(fileURLToPath(import.meta.url));

export async function openNewProject(page: Page) {
  const field = page.getByLabel('项目名称', { exact: true });
  if (!await field.isVisible()) await page.locator('summary', { hasText: '新建项目' }).click();
  await expect(field).toBeVisible();
}

// Each business check gets its own session through the API. Signing out in one check cannot
// revoke another check's credentials. Entry-point checks opt out and exercise the login page.
export const test = base.extend<{ authenticated: boolean }>({
  authenticated: [true, { option: true }],
  page: async ({ page, authenticated, baseURL }, use) => {
    if (!authenticated) {
      await use(page);
      return;
    }
    const origin = new URL(baseURL!).origin;
    let key = readFileSync(resolve(here, '../../runtime/secrets/access_key'), 'utf8').trim();
    let csrf = '';
    let status: number | undefined;
    try {
      const response = await page.request.post('/auth/login', {
        headers: { Origin: origin }, data: { access_key: key }, timeout: 5000,
      });
      status = response.status();
      const body: { csrf_token?: unknown } = await response.json();
      if (!response.ok() || typeof body.csrf_token !== 'string') throw new Error();
      csrf = body.csrf_token;
    } catch {
      // Playwright request errors can include the submitted body. Keep credentials out of reports.
      throw new Error(`浏览器测试登录失败${status ? `（HTTP ${status}）` : ''}；检查本机服务与密钥配置。`);
    } finally {
      key = '';
    }
    try {
      // Exercise the real fake-Run lifecycle with short fixture actions. The production default
      // stays unchanged; only requests explicitly creating a demonstration receive this value.
      await page.route('**/api/v1/runs', route => {
        if (route.request().method() !== 'POST') return route.continue();
        const body = route.request().postDataJSON();
        if (!('demonstration_scenario' in body)) return route.continue();
        return route.continue({
          postData: JSON.stringify({ ...body, demonstration_duration_ms: 1000 }),
        });
      });
      const [projects] = await Promise.all([
        page.waitForResponse(response =>
          new URL(response.url()).pathname === '/api/v1/projects'
          && response.request().method() === 'GET', { timeout: 5000 }),
        page.goto('/'),
      ]);
      expect(projects.ok(), '工作台项目列表请求成功').toBe(true);
      await expect(page.getByRole('heading', { name: '先确定范围，再开始研究。' })).toBeVisible();
      // A response arrives before Vue has finished load() and settled the project disclosure.
      // The fieldset becoming enabled is the page's own signal that initialization completed.
      await expect(page.getByLabel('所属项目')).toBeEnabled();
      await use(page);
    } finally {
      // A check may already have signed out. Cleanup is bounded even if the service stops.
      await page.request.post('/auth/logout', {
        headers: { Origin: origin, 'X-CSRF-Token': csrf }, timeout: 5000,
      }).catch(() => undefined);
    }
  },
});

export { expect };
