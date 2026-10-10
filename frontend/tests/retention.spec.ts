import { type Page } from '@playwright/test';
import { expect, openNewProject, test } from './fixtures';

// Selective retention (issue #20, spec 0002 section 3.8, PROJECT.md section 10.5). Each check drives
// the section with a response shaped the way the execution side could really answer, so what is
// asserted is what a reader would see rather than what the platform would like to say. Two rules are
// under test in particular: a retention read nobody could take is an observation gap and never an
// empty cache, and a reclaim that cannot reach capacity is a block with its shortfall rather than a
// partial delete dressed up as success. No Runner with real container management is needed — the
// retention read and its actions are answered here, and nothing starts a container or a server.

const RUN_ID = '4f1d0c2b-5a6e-4c1d-8f3a-0b1c2d3e4f50';
const KEY = 'a1b2c3d4e5f60718293a4b5c6d7e8f90';
const PINNED = '9c0f1a2b-3c4d-4e5f-8a9b-0c1d2e3f4051';
const FREE = '7d8e9f00-1a2b-4c3d-9e4f-5a6b7c8d9e02';
const SESSION_ID = '2b3c4d5e-6f70-4182-9394-a5b6c7d8e9f0';
const INSTANCE_ID = '3c4d5e6f-7081-4293-a4b5-c6d7e8f90123';
const ACTOR_ID = 'aa11bb22-cc33-4d44-8e55-ff6677889900';
const AT = '2026-10-10T11:17:28+00:00';
const EXPIRES_AT = '2026-10-17T11:17:28+00:00';
const LIMITS = { candidate_window_days: 30, candidate_runs: 3, cache_ttl_days: 7, cache_capacity_bytes: 10737418240 };

/** One held artifact as the ledger reports it: `null` size means the engine did not measure it. */
function artifact(artifactId: string, name: string, sizeBytes: number | null, pinned: boolean) {
  return {
    artifact_id: artifactId, environment_key: KEY, run_id: RUN_ID, session_id: SESSION_ID,
    instance_id: INSTANCE_ID, kind: 'workspace', resource_name: name, size_bytes: sizeBytes,
    retained_at: AT, expires_at: EXPIRES_AT, pinned, state: 'retained', deleted_at: null, delete_reason: null,
  };
}

function decision(overrides: Record<string, unknown>) {
  return {
    decision_id: 'dd11ee22-ff33-4a44-8b55-cc6677889900', action: 'pin', target_kind: 'artifact',
    target: PINNED, at: AT, actor: ACTOR_ID, note: '这条工作区要留着复现', reason_code: null,
    affected_runs: [RUN_ID], freed_bytes: 0, deleted_artifacts: [], failed: [], ...overrides,
  };
}

/** A reachable execution side: one candidate version and two artifacts, one of them unmeasured. */
function retentionRead() {
  return {
    available: true, reason_code: null, sandbox_management: 'ready', observed_at: AT, limits: LIMITS,
    versions: [{
      environment_key: KEY, profile_id: 'sandbox-egress-v1', profile_version: 1,
      image_digests: { tool: 'sha256:' + 'a'.repeat(64) }, tool_inventory: ['curl', 'nmap'],
      engine: '29.7.2', architecture: 'x86_64', successful_runs: 3, successful_calls: 5,
      window_days: 30, threshold_runs: 3, candidate: true, last_used_at: AT,
      retained_artifacts: 2, retained_bytes: 1048576, unmeasured_artifacts: 1,
      pinned: false, shared_tool_version: false,
    }],
    artifacts: [
      artifact(PINNED, 'huntweave-workspace-9c0f1a2b', 1048576, true),
      artifact(FREE, 'huntweave-workspace-7d8e9f00', null, false),
    ],
    preview: {
      capacity_bytes: 10737418240, ttl_days: 7, retained_bytes: 1048576, unmeasured_artifacts: 1,
      to_delete: [artifact(FREE, 'huntweave-workspace-7d8e9f00', null, false)],
      protected: [{ artifact_id: PINNED, resource_name: 'huntweave-workspace-9c0f1a2b', run_id: RUN_ID, reason_code: 'retention_artifact_pinned' }],
      // The one selected artifact was never measured, so the reclaim frees no *known* bytes. That is
      // stated as 0 rather than guessed.
      freed_bytes: 0, blocked: false, reason_code: null, needed_bytes: 0,
    },
    decisions: [
      decision({}),
      decision({
        decision_id: 'dd11ee22-ff33-4a44-8b55-cc6677889901', action: 'delete', target_kind: 'version',
        target: KEY, note: '清掉一次性环境', reason_code: 'retention_delete_failed',
        failed: [`retention_artifact_in_use:${PINNED}`],
      }),
    ],
  };
}

/** No management capability: a real answer with a reason, not a cache with nothing in it. */
function gapRead() {
  return {
    available: false, reason_code: 'sandbox_management_disabled', sandbox_management: 'disabled',
    observed_at: AT, limits: LIMITS, versions: [], artifacts: [],
    preview: { capacity_bytes: 10737418240, ttl_days: 7, retained_bytes: 0, unmeasured_artifacts: 0, to_delete: [], protected: [], freed_bytes: 0, blocked: false, reason_code: null, needed_bytes: 0 },
    decisions: [],
  };
}

/** Two measured artifacts and a capacity that reclaiming both still cannot meet. */
function blockedRead(toDelete: unknown[]) {
  return {
    available: true, reason_code: null, sandbox_management: 'ready', observed_at: AT,
    limits: { ...LIMITS, cache_capacity_bytes: 393216 },
    versions: [{
      environment_key: KEY, profile_id: 'sandbox-egress-v1', profile_version: 1, image_digests: {},
      tool_inventory: ['curl'], engine: '29.7.2', architecture: 'x86_64', successful_runs: 3,
      successful_calls: 4, window_days: 30, threshold_runs: 3, candidate: true, last_used_at: AT,
      retained_artifacts: 2, retained_bytes: 3145728, unmeasured_artifacts: 0,
      pinned: false, shared_tool_version: false,
    }],
    artifacts: [
      artifact(PINNED, 'huntweave-workspace-9c0f1a2b', 1048576, true),
      artifact(FREE, 'huntweave-workspace-7d8e9f00', 2097152, false),
    ],
    preview: {
      capacity_bytes: 393216, ttl_days: 7, retained_bytes: 3145728, unmeasured_artifacts: 0,
      to_delete: toDelete,
      protected: [{ artifact_id: PINNED, resource_name: 'huntweave-workspace-9c0f1a2b', run_id: RUN_ID, reason_code: 'retention_artifact_pinned' }],
      // 3145728 - 2097152 = 1048576 still retained, against a 393216 capacity: the shortfall.
      freed_bytes: 2097152, blocked: true, reason_code: 'retention_capacity_insufficient', needed_bytes: 655360,
    },
    decisions: [],
  };
}

async function serveRetention(page: Page, body: unknown) {
  await page.route('**/api/v1/retention', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }));
}

/** The demo Run the console needs, created the same way the other console checks create one. */
async function createDemo(page: Page, targets = '192.0.2.50') {
  await openNewProject(page);
  await page.getByLabel('项目名称', { exact: true }).fill(`P1 选择性保留 ${Date.now()}`);
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

test('the retention section states the execution side read, and the reclaim waits for confirmation', async ({ page }) => {
  let sweeps = 0;
  await serveRetention(page, retentionRead());
  // The sweep is armed by a first click and only applied by the second: no request until then.
  await page.route('**/api/v1/retention/sweep', (route) => {
    sweeps += 1;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ decision: decision({ action: 'sweep' }), deleted: [], failed: [], view: retentionRead() }) });
  });
  await createDemo(page);

  const region = page.getByTestId('retention');
  await expect(region).toBeVisible();
  await expect(region).toContainText('选择性保留');
  // Numbers stay numbers: no percentage, ratio bar or single health light anywhere in the section.
  await expect(region).not.toContainText('%');

  // Candidate statistics: distinct Runs against the threshold, retries counted but never candidacy.
  const version = page.getByTestId('retention-version');
  await expect(version).toContainText('环境版本 a1b2c3d4…');
  await expect(version).toContainText('sandbox-egress-v1 v1');
  await expect(version).toContainText('3 / 3 个独立 Run 在 30 天内成功使用');
  await expect(version).toContainText('5 次（含同一 Run 内的重试');
  await expect(version).toContainText('候选');
  await expect(version).toContainText('未发布为共享工具版本');
  await expect(version).toContainText('2 个 · 1048576 B');
  await expect(version).toContainText('其中 1 个未测量');
  await expect(version.getByRole('button', { name: '固定', exact: true })).toBeVisible();
  await expect(version.getByRole('button', { name: '删除该版本的制品' })).toBeVisible();

  // Held artifacts: the resource name, the short Run id, the times, and 未知 for an unmeasured size.
  const held = page.getByTestId('retention-artifact');
  await expect(held).toHaveCount(2);
  const pinned = held.filter({ hasText: 'huntweave-workspace-9c0f1a2b' });
  await expect(pinned).toContainText(`Run ${RUN_ID.slice(0, 8)}…`);
  await expect(pinned).toContainText('大小 1048576 B');
  await expect(pinned).toContainText('保留于 2026年10月10日');
  await expect(pinned).toContainText('到期 2026年10月17日');
  await expect(pinned).toContainText('已固定');
  await expect(pinned.getByRole('button', { name: '取消固定', exact: true })).toBeVisible();
  await expect(pinned.getByRole('button', { name: '删除', exact: true })).toBeVisible();
  await expect(held.filter({ hasText: 'huntweave-workspace-7d8e9f00' })).toContainText('大小 未知');

  // The reclaim preview: what would go, what is protected and by which rule, and the policy numbers.
  const preview = page.getByTestId('retention-preview');
  await expect(preview).toContainText('1048576 B');
  await expect(preview).toContainText('10737418240 B');
  await expect(preview).toContainText('7 天');
  await expect(preview).toContainText('1 个未测量');
  await expect(region).toContainText('将删除（1 项）');
  await expect(region).toContainText('受保护（1 项）');
  await expect(region).toContainText('该制品被显式固定，自动回收不会移除它。');
  await expect(page.getByTestId('retention-blocked')).toHaveCount(0);

  // Recent decisions: action, target, who asked, note, and the refusals the action really met.
  const decisions = page.getByTestId('retention-decision');
  await expect(decisions).toHaveCount(2);
  await expect(decisions.first()).toContainText('固定 · 制品');
  await expect(decisions.first()).toContainText('这条工作区要留着复现');
  await expect(decisions.first()).toContainText(`操作员 ${ACTOR_ID.slice(0, 8)}…`);
  await expect(decisions.first()).toContainText('释放 0 B');
  await expect(decisions.nth(1)).toContainText('删除 · 环境版本');
  await expect(decisions.nth(1)).toContainText('部分制品未能删除，账本按实际结果记录，未删除的部分仍占用缓存。');
  await expect(decisions.nth(1)).toContainText('该制品仍被活动实例引用，删除被拒绝，未移除任何内容。');
  await page.screenshot({ path: '../runtime/validation/p1-console-retention.png', fullPage: true });

  // One click only shows what the sweep would delete; the request goes out on the confirmation.
  await page.getByTestId('retention-sweep').click();
  await expect(page.getByTestId('retention-sweep-confirm')).toBeVisible();
  await expect(page.getByTestId('retention-sweep-confirm')).toContainText('这一步只删除上面「将删除」列出的 1 项');
  await expect(page.getByTestId('retention-sweep-confirm')).toContainText('释放 0 B');
  expect(sweeps).toBe(0);
});

test('a retention read the platform could not take is a gap, not an empty cache', async ({ page }) => {
  await serveRetention(page, gapRead());
  await createDemo(page);

  const gap = page.getByTestId('retention-unavailable');
  await expect(gap).toBeVisible();
  await expect(gap).toContainText('无法读取保留策略状态');
  await expect(gap).toContainText('本部署未启用容器管理能力，无法读取容器与网关状态。');
  await expect(gap).toContainText('这是观测缺口，不是「缓存为空」');
  await expect(gap).toContainText('容器管理能力：未启用（默认部署');

  // The empty-list wording belongs to an answered read only. Here nothing may be rendered as a
  // cache that simply holds nothing.
  const region = page.getByTestId('retention');
  await expect(region).not.toContainText('候选版本');
  await expect(region).not.toContainText('执行端账本当前没有持有任何制品');
  await expect(region).not.toContainText('执行端还没有记录任何保留决定');
  // The policy this deployment runs under is still stated, and marked as not an observation.
  await expect(region).toContainText('策略参数仍按本部署的运行配置列出');
  await expect(region).toContainText('30 天内 ≥ 3 个独立 Run 为候选');
});

test('a blocked reclaim is shown as a block with its shortfall, never as a partial success', async ({ page }) => {
  const toDelete = [artifact(FREE, 'huntweave-workspace-7d8e9f00', 2097152, false)];
  await serveRetention(page, blockedRead(toDelete));
  // The sweep removes exactly the reclaimable set and the view keeps reporting the shortfall.
  const afterSweep = blockedRead([]);
  await page.route('**/api/v1/retention/sweep', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        decision: decision({ action: 'sweep', target_kind: 'deployment', target: 'preview', note: '按预览回收', deleted_artifacts: [FREE], freed_bytes: 2097152 }),
        deleted: [FREE], failed: [], view: afterSweep,
      }),
    }));
  await createDemo(page);

  const blocked = page.getByTestId('retention-blocked');
  await expect(blocked).toBeVisible();
  await expect(blocked).toContainText('容量阻断');
  await expect(blocked).toContainText('655360 B');
  await expect(blocked).toContainText('即使回收全部可回收制品，缓存仍超出容量上限，回收被阻断并给出缺口。');
  await expect(blocked).toContainText('不会把它显示成部分成功');

  // The confirmation says the same thing the preview does: this will not reach capacity.
  await page.getByTestId('retention-sweep').click();
  await expect(page.getByTestId('retention-sweep-confirm')).toContainText('回收完成后仍会超出容量上限 655360 B');
  await expect(page.getByTestId('retention-sweep-apply')).toContainText('仍不足以降到容量以内');
  await page.getByTestId('retention-sweep-apply').click();

  // What the execution side reported, and the block that is still standing afterwards.
  const report = page.getByTestId('retention-report');
  await expect(report).toBeVisible();
  await expect(report).toContainText('回收 · 部署范围 preview');
  await expect(report).toContainText('已删除 1 项 · 释放 2097152 B');
  await expect(page.getByTestId('retention-blocked')).toBeVisible();
  await expect(page.getByTestId('retention-blocked')).toContainText('655360 B');
  await page.screenshot({ path: '../runtime/validation/p1-console-retention-blocked.png', fullPage: true });
});
