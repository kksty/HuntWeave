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
  const firstRunUrl = page.url();
  const firstCount = (await (await page.request.get('/api/v1/runs')).json()).length;
  await page.getByRole('button', { name: '创建假 Run' }).click();
  await expect(page.getByRole('button', { name: '退出登录' })).toBeEnabled();
  await expect(page).toHaveURL(firstRunUrl);
  expect((await (await page.request.get('/api/v1/runs')).json()).length).toBe(firstCount);
  // A new immutable scope needs a new key, even if its input fields are unchanged.
  await page.getByRole('button', { name: '保存授权快照' }).click();
  await page.getByRole('button', { name: '创建假 Run' }).click();
  await expect(page).not.toHaveURL(firstRunUrl);
  expect((await (await page.request.get('/api/v1/runs')).json()).length).toBe(firstCount + 1);
  const runUrl = page.url();
  await expect(page.getByRole('button', { name: '将假 Run 加入队列' })).toBeVisible();
  await page.getByRole('button', { name: '将假 Run 加入队列' }).click();
  await expect(page.getByRole('button', { name: '将假 Run 加入队列' })).toHaveCount(0);
  await page.reload();
  await expect(page).toHaveURL(runUrl);
  await expect(page.getByRole('region', { name: '玻璃鱼缸执行台' })).toBeVisible();
  await expect(page.getByText('自动阶段结束 · 待人工复审').first()).toBeVisible({ timeout: 60000 });
  await expect(page.getByText('只用于本地假执行验证', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => Reflect.get(window, 'huntweaveInjected'))).toBeUndefined();
  await page.screenshot({ path: '../runtime/validation/p0-b-workspace.png', fullPage: true });
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page).toHaveURL(/\/login$/);
});

async function createDemo(page: Page, scenario: string) {
  if (!await page.getByLabel('项目名称', { exact: true }).isVisible()) await page.getByText('新建项目', { exact: true }).click();
  await page.getByLabel('项目名称', { exact: true }).fill(`P0 控制 ${scenario} ${Date.now()}`);
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await page.getByLabel(/目标 IP/).fill('192.0.2.30');
  await page.getByRole('button', { name: '预览 IP', exact: true }).click();
  await page.getByLabel('TCP 端口范围').selectOption('custom-tcp-v1');
  await page.getByLabel('端口与范围').fill('80');
  await page.getByRole('button', { name: '展开端口' }).click();
  await page.getByLabel('授权说明', { exact: true }).fill('固定假动作浏览器验收，无目标连接');
  await page.getByRole('button', { name: '保存授权快照' }).click();
  await page.getByLabel('假输出场景').selectOption(scenario);
  await page.getByRole('button', { name: '创建假 Run' }).click();
  await expect(page).toHaveURL(/\/runs\/[a-f0-9-]+$/);
  await page.getByRole('button', { name: '将假 Run 加入队列' }).click();
}

test('pause, reload, resume preview, original evidence and human close preserve a Run', async ({ page }) => {
  await login(page);
  await createDemo(page, 'positive');
  const originalUrl = page.url();
  await page.getByRole('button', { name: '暂停 Run', exact: true }).click();
  await expect(page.getByRole('button', { name: '查看恢复预览' })).toBeVisible({ timeout: 30000 });
  await page.reload();
  await expect(page).toHaveURL(originalUrl);
  await page.getByRole('button', { name: '查看恢复预览' }).click();
  await expect(page.getByRole('region', { name: '恢复预览' })).toBeVisible();
  await expect(page.getByRole('button', { name: '按预览恢复' })).toBeEnabled();
  await page.getByRole('button', { name: '按预览恢复' }).click();
  await expect(page.getByRole('button', { name: '结束演示', exact: true })).toBeVisible({ timeout: 60000 });
  await page.getByRole('button', { name: '查看原始证据' }).first().click();
  await expect(page.getByRole('region', { name: '原始证据' })).toBeVisible();
  const evidence = page.getByRole('region', { name: '原始证据' });
  await expect(evidence.locator('pre')).not.toBeEmpty();
  await expect(evidence).toContainText('SHA-256');
  await page.getByRole('button', { name: '结束演示', exact: true }).click();
  await expect(page.getByText('演示已结束').first()).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(originalUrl);
  await expect(page.getByText('演示已结束').first()).toBeVisible();
  await page.screenshot({ path: '../runtime/validation/p0-console.png', fullPage: true });
});

test('cancel waits for Runner acknowledgement and prevents later dispatch', async ({ page }) => {
  await login(page);
  await createDemo(page, 'negative');
  // Wait for the console to show the first dispatched call, so the cancel exercises the
  // acknowledgement path against a live snapshot instead of racing an empty console.
  await expect(page.getByText('fake.collect', { exact: false }).first()).toBeVisible({ timeout: 30000 });
  await page.getByRole('button', { name: '取消 Run', exact: true }).click();
  await expect(page.getByText('已取消', { exact: true }).first()).toBeVisible({ timeout: 30000 });
  const snapshotUrl = `/api/v1${new URL(page.url()).pathname}/snapshot`;
  const first = await (await page.request.get(snapshotUrl)).json();
  await page.reload();
  await expect(page.getByText('已取消', { exact: true }).first()).toBeVisible();
  const second = await (await page.request.get(snapshotUrl)).json();
  expect(second.calls.length).toBe(first.calls.length);
  expect(second.budget).toEqual(first.budget);
  await expect(page.getByRole('button', { name: '按预览恢复' })).toHaveCount(0);
});
