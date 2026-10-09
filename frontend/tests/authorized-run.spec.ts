import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test, type Page } from '@playwright/test';

async function login(page: Page) {
  await page.goto('/login');
  await expect(page).toHaveTitle('登录 · HuntWeave');
  let key = readFileSync(resolve('../runtime/secrets/access_key'), 'utf8').trim();
  try { await page.getByLabel('全局访问密钥').fill(key); }
  catch { throw new Error('无法填写登录字段；为保护密钥，已隐藏调用参数。'); }
  finally { key = ''; }
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '先确定范围，再开始研究。' })).toBeVisible();
  await expect(page.getByRole('button', { name: '退出登录' })).toBeEnabled();
  await expect(page).toHaveTitle('HuntWeave · 开发演示');
  await expect(page.getByText('界巡', { exact: false })).toHaveCount(0);
}

test('anonymous deep links and bundles are protected; login creates an HttpOnly session', async ({ page, request }) => {
  expect((await request.get('/api/v1/runs')).status()).toBe(401);
  expect((await request.get('/assets/index.js')).status()).toBe(401);
  await page.goto('/runs/00000000-0000-0000-0000-000000000000');
  await expect(page).toHaveURL(/\/login$/);
  await login(page);
  const cookies = await page.context().cookies();
  const cookie = cookies.find(item => item.name === 'huntweave_local_session');
  expect(cookie?.httpOnly).toBe(true);
  expect(cookie?.sameSite).toBe('Strict');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect((await page.request.get('/api/v1/runs')).status()).toBe(401);
});

test('preview, freeze, idempotent draft creation, queue, reload and logout', async ({ page }) => {
  await login(page);
  const projectName = `假执行验证 ${Date.now()} <script>window.huntweaveInjected=true</script>`;
  if (!await page.getByLabel('项目名称', { exact: true }).isVisible()) {
    await page.getByText('新建项目', { exact: true }).click();
  }
  await page.getByLabel('项目名称', { exact: true }).fill(projectName);
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByLabel('所属项目')).not.toHaveValue('');
  await page.getByLabel(/目标 IP/).fill('192.0.2.10\nhttps://example.com\n2001:db8::1');
  await page.getByRole('button', { name: '预览 IP', exact: true }).click();
  await expect(page.getByText('存在错误，不能提交', { exact: false })).toBeVisible();
  await expect(page.getByText('当前部署未启用 IPv6 隔离，不能运行。')).toBeVisible();
  await expect(page.getByRole('button', { name: '保存授权快照' })).toBeDisabled();
  await page.getByLabel(/目标 IP/).fill('192.0.2.10\n192.0.2.10\n198.51.100.20');
  await page.getByRole('button', { name: '预览 IP', exact: true }).click();
  await expect(page.getByText('与第 1 行重复，已合并')).toBeVisible();
  await page.getByLabel('TCP 端口范围').selectOption('custom-tcp-v1');
  await page.getByLabel('端口与范围').fill('443,80,8000-8002,80');
  await page.getByRole('button', { name: '展开端口' }).click();
  await expect(page.getByText('5 个明确授权端口')).toBeVisible();
  await page.getByLabel('授权说明', { exact: true }).fill('只用于本地假执行验证');
  await page.getByRole('button', { name: '保存授权快照' }).click();
  await page.getByRole('button', { name: '创建假 Run' }).click();
  await expect(page).toHaveURL(/\/runs\/[a-f0-9-]+$/);
  const runUrl = page.url();
  await expect(page.getByRole('button', { name: '将假 Run 加入队列' })).toBeVisible();
  await page.getByRole('button', { name: '将假 Run 加入队列' }).click();
  await expect(page.getByRole('button', { name: '将假 Run 加入队列' })).toHaveCount(0);
  await page.reload();
  await expect(page).toHaveURL(runUrl);
  await expect(page.getByText('已排队 · 尚未执行').first()).toBeVisible();
  await expect(page.getByText('只用于本地假执行验证', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => Reflect.get(window, 'huntweaveInjected'))).toBeUndefined();
  await page.screenshot({ path: '../runtime/validation/p0-b-workspace.png', fullPage: true });
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page).toHaveURL(/\/login$/);
});
