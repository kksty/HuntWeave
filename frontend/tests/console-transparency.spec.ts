import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test, type Page } from '@playwright/test';

// The console's own honesty rules (issue #19). Each check drives the page against a snapshot shaped
// the way the execution side could really report, so what is asserted is what a reader would see —
// not what the platform would like to say.

async function login(page: Page) {
  await page.goto('/login');
  let key = readFileSync(resolve('../runtime/secrets/access_key'), 'utf8').trim();
  try { await page.getByLabel('全局访问密钥').fill(key); }
  catch { throw new Error('无法填写登录字段；为保护密钥，已隐藏调用参数。'); }
  finally { key = ''; }
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '先确定范围，再开始研究。' })).toBeVisible();
}

async function openNewProject(page: Page) {
  // The fold is bound to "no project exists yet", so it is closed once one does — and it is closed
  // *after* first paint, which is why reading visibility once and clicking on that reading is not a
  // reliable sequence. Clicking the summary and then waiting for the field is; a click that lands
  // the wrong way is retried by the same loop.
  const field = page.getByLabel('项目名称', { exact: true });
  const summary = page.locator('summary', { hasText: '新建项目' });
  await expect(summary).toBeVisible();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await summary.click();
    try {
      await expect(field).toBeVisible({ timeout: 5000 });
      return;
    } catch {
      // The fold was still settling from the first paint: clicking again opens it.
    }
  }
  await expect(field).toBeVisible();
}

async function createDemo(page: Page, targets = '192.0.2.40') {
  await openNewProject(page);
  await page.getByLabel('项目名称', { exact: true }).fill(`P1 透明控制台 ${Date.now()}`);
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByLabel('所属项目')).not.toHaveValue('');
  await page.getByLabel(/目标 IP/).fill(targets);
  await page.getByRole('button', { name: '预览 IP', exact: true }).click();
  await page.getByLabel('TCP 端口范围').selectOption('custom-tcp-v1');
  await page.getByLabel('端口与范围').fill('80');
  await page.getByRole('button', { name: '展开端口' }).click();
  await page.getByLabel('授权说明', { exact: true }).fill('固定假动作浏览器验收，无目标连接');
  await page.getByRole('button', { name: '保存授权快照' }).click();
  await page.getByRole('button', { name: '创建假 Run' }).click();
  await expect(page).toHaveURL(/\/runs\/[a-f0-9-]+$/);
}

/** The real snapshot, with the fields a scenario needs stated by the test rather than awaited. */
async function frozenSnapshot(page: Page, patch: (snapshot: Record<string, any>) => void) {
  const path = `/api/v1${new URL(page.url()).pathname}/snapshot`;
  const snapshot = await (await page.request.get(path)).json();
  patch(snapshot);
  const body = JSON.stringify(snapshot);
  await page.route('**/api/v1/runs/*/snapshot', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body }));
  return path;
}

test('the console states its views separately and treats a missing answer as a gap', async ({ page }) => {
  await login(page);
  await createDemo(page);
  const grid = page.getByTestId('run-state-grid');
  await expect(grid).toBeVisible();
  // Four views plus the deployment's management capability and this Run's occupancy: never one
  // lamp, and never a percentage that would imply the platform measured safety.
  await expect(grid).toContainText('执行');
  await expect(grid).toContainText('研究');
  await expect(grid).toContainText('复审');
  await expect(grid).toContainText('交付');
  await expect(grid).toContainText('容器管理能力');
  await expect(grid).toContainText('未启用（默认部署');
  await expect(grid).toContainText('本次占用（控制槽与物理额度）');
  await expect(grid).not.toContainText('%');
  await page.screenshot({ path: '../runtime/validation/p1-console-state-grid.png', fullPage: true });

  // The same page with an execution side that cannot answer: that is an observation gap, and it is
  // rendered as one instead of as a Run with nothing outside it.
  await frozenSnapshot(page, (snapshot) => {
    snapshot.runtime = {
      available: false, reason_code: 'sandbox_management_disabled',
      sandbox_management: 'disabled', profile_id: null,
      observed_at: new Date().toISOString(), run_id: snapshot.run.id,
      sessions: [], unaccounted: [], missing: [], interrupted: [], reclaimable: [],
    };
  });
  await page.reload();
  const gap = page.getByTestId('runtime-unavailable');
  await expect(gap).toBeVisible({ timeout: 30000 });
  await expect(gap).toContainText('无法读取容器与网关状态');
  await expect(gap).toContainText('这是观测缺口，不是「没有资源在运行」');
});

test('an unconfirmed stop is shown as unconfirmed, with what still holds it', async ({ page }) => {
  await login(page);
  await createDemo(page);
  const instanceId = '11111111-2222-3333-4444-555555555555';
  await frozenSnapshot(page, (snapshot) => {
    snapshot.runtime = {
      available: true, reason_code: null, sandbox_management: 'ready',
      profile_id: 'sandbox-egress-v1', observed_at: new Date().toISOString(),
      run_id: snapshot.run.id,
      sessions: [{
        session_id: '99999999-8888-7777-6666-555555555555',
        agent_session_id: snapshot.sessions[0]?.id ?? '99999999-8888-7777-6666-555555555556',
        run_id: snapshot.run.id, scope_version: 1, policy_version: 1,
        created_at: new Date().toISOString(),
        instances: [{
          instance_id: instanceId, session_id: '99999999-8888-7777-6666-555555555555',
          run_id: snapshot.run.id, state: 'ready', stop_state: 'unconfirmed',
          environment: { profile_id: 'sandbox-egress-v1', profile_version: 1, image_digests: { tool: 'sha256:' + 'a'.repeat(64) }, tool_inventory: ['curl'], engine: '29.7.2', architecture: 'x86_64', created_at: new Date().toISOString() },
          egress: { authorized: [], applied_at: new Date().toISOString(), revoked_at: new Date().toISOString(), revocation_reason: 'scope_revoked', changes: [{ reason: 'initial', at: new Date().toISOString() }, { reason: 'scope_revoked', at: new Date().toISOString() }] },
          lease_expires_at: null, halt_reason: 'operator_cancelled',
          resources: [{ kind: 'container', role: 'tool', name: 'huntweave-tool', running: true }],
        }],
      }],
      unaccounted: [], missing: [], interrupted: [],
      reclaimable: [{ instance_id: instanceId, state: 'ready', stop_state: 'unconfirmed', resources: [{ kind: 'container', role: 'tool', name: 'huntweave-tool', running: true }], confirmation_required: true }],
    };
    // One call whose stop the execution side did not confirm, stated the way the ledger states it:
    // the process and the connection are `null` (cannot confirm), never `false` (stopped).
    snapshot.calls = [{
      id: 'aaaaaaaa-bbbb-cccc-dddd-000000000001',
      session_id: snapshot.sessions[0]?.id ?? instanceId,
      decision_id: 'aaaaaaaa-bbbb-cccc-dddd-000000000002',
      status: 'unknown', action: 'shell.exec', parameters: { command: 'id' },
      parameters_hash: 'b'.repeat(64),
      progress: { status: 'ended', started_at: new Date().toISOString(), last_output_at: null, output_bytes: 12, timeout_seconds: 60, elapsed_ms: 900 },
      runtime: {
        action_id: 'shell.exec', execution_profile: 'real-lab-v1',
        target_ip: '192.0.2.40', target_port: 80,
        parameters_hash: 'b'.repeat(64), argv: ['sh', '-c', 'id'], cwd: '/workspace',
        user: '10001:10001', instance_id: instanceId,
        session_id: '99999999-8888-7777-6666-555555555555',
        image_digests: { tool: 'sha256:' + 'a'.repeat(64) },
        tool_inventory: ['curl'], engine: '29.7.2', architecture: 'x86_64', profile_version: 1,
        network_mode: 'internal', gateway_ids: ['gateway-1'], authorized: ['192.0.2.40:80'],
        observed_at: new Date().toISOString(),
      },
      result: null, evidence_ids: [], created_at: new Date().toISOString(),
      replaces_call_id: null, reconciliation: null, conditions: ['outcome_unsettled', 'stop_unconfirmed'],
      observation: {
        started: true, process_active: null, connection_open: null, lease_active: false,
        observed_at: new Date().toISOString(), stop_confirmed: false,
      },
    }];
  });
  await page.reload();
  const banner = page.getByTestId('stop-unconfirmed');
  await expect(banner).toBeVisible({ timeout: 30000 });
  await expect(banner).toContainText('停止未确认');
  await expect(banner).toContainText('尚未释放');
  await expect(banner).toContainText('不会显示为已回收或已取消');
  const quarantine = page.getByTestId('instance-quarantine');
  await expect(quarantine).toContainText('停止未确认');
  await expect(quarantine).toContainText('不再发起新的目标请求');
  // The reclaim preview states the confirmation it still needs instead of promising removal.
  await expect(page.getByTestId('reclaim-preview')).toContainText('仍需停止确认');
  // The call card shows the command, the directory and the identity the execution side reported.
  await expect(page.locator('.call-binding').first()).toContainText('工作目录');
  await expect(page.locator('.call-binding').first()).toContainText('/workspace');
  await expect(page.locator('.call-card pre').first()).toContainText('sh -c id');
  await page.screenshot({ path: '../runtime/validation/p1-console-stop-unconfirmed.png', fullPage: true });
});

test('a cut or redacted archive is labelled instead of presented as a whole observation', async ({ page }) => {
  await login(page);
  await createDemo(page);
  const callId = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
  await frozenSnapshot(page, (snapshot) => {
    snapshot.calls = [{
      id: callId, session_id: snapshot.sessions[0]?.id ?? callId, decision_id: callId,
      status: 'succeeded', action: 'shell.exec', parameters: { command: 'env' },
      parameters_hash: 'c'.repeat(64), runtime: null,
      progress: { status: 'ended', started_at: new Date().toISOString(), last_output_at: new Date().toISOString(), output_bytes: 20 * 1024 * 1024, timeout_seconds: 60, elapsed_ms: 4200 },
      result: {
        output: 'password= [redacted:password_assignment]\n', exit_code: 0,
        summary: { exit_code: 0 }, duration_ms: 4200, truncated: true, redacted: true, reason_code: null,
        evidence: [{
          id: callId, relative_path: 'run/call.txt', sha256: 'd'.repeat(64),
          size_bytes: 20 * 1024 * 1024, available: true, truncated: true, redacted: true, missing_reason: null,
        }],
      },
      evidence_ids: [], created_at: new Date().toISOString(),
      replaces_call_id: null, observation: null, reconciliation: null, conditions: [],
    }];
  });
  await page.reload();
  const flags = page.getByTestId('call-evidence-flags');
  await expect(flags).toBeVisible({ timeout: 30000 });
  await expect(flags).toContainText('输出被本次 Run 的保留上限截断');
  await expect(flags).toContainText('含凭据形态内容已脱敏，原值未保存');
  await expect(flags).toContainText('未保留的部分不作为已观察事实');
  // A call with no execution-side statement says so rather than borrowing the current profile.
  await expect(page.locator('.call-card')).toContainText('该调用没有执行端的运行环境陈述');
  // The timing is stated as a duration and a bound, never as a progress percentage.
  await expect(page.getByTestId('call-progress')).toContainText('不显示进度百分比');
});

test('input over the per-Run limit is answered by line, and IPv6 keeps its own reason', async ({ page }) => {
  await login(page);
  const overflow = Array.from({ length: 101 }, (_, index) => `192.0.2.${index + 1}`);
  overflow[99] = '2001:db8::5'; // the hundredth row is not executable here, and says so
  await page.getByLabel(/目标 IP/).fill(overflow.join('\n'));
  await page.getByRole('button', { name: '预览 IP', exact: true }).click();
  await expect(page.getByText('超限 1 行')).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('alert')).toContainText('超过上限的行已在下表按行号标出');
  // The IPv6 row is refused for its own reason rather than being counted out first.
  await expect(page.getByText('当前部署未启用 IPv6 隔离，不能运行。')).toBeVisible();
  await expect(page.getByText('目标数量超过单 Run 上限，请按提示的行号移除多余目标。', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存授权快照' })).toBeDisabled();
});
