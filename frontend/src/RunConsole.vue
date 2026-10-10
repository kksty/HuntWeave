<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useWorkspace, ApiFailure, managementLabels, reasonText, statusText, type CallProgress, type CallRuntime, type EvidenceMeta, type Run, type RunRuntime } from './workspace';

interface AuditEvent { cursor: number; type: string; payload: Record<string, unknown>; created_at: string }
interface Observation { started: boolean; process_active: boolean | null; connection_open: boolean | null; lease_active: boolean | null; observed_at: string; stop_confirmed: boolean }
interface Reconciliation { call_id: string; outcome: string; operator_session_id: string; scope_version: number; evidence_ids: string[]; note: string; observation: Observation | null; redispatch_authorized: boolean; recorded_at: string }
interface CallResult { output?: string; exit_code?: number | null; evidence?: EvidenceMeta[]; summary?: Record<string, unknown>; duration_ms?: number | null; truncated?: boolean; redacted?: boolean; reason_code?: string | null }
interface Call { id: string; session_id: string; decision_id: string; status: string; action: string; parameters: Record<string, unknown>; parameters_hash: string | null; runtime: CallRuntime | null; progress: CallProgress | null; result: CallResult | null; evidence_ids: string[]; created_at: string; replaces_call_id: string | null; observation: Observation | null; reconciliation: Reconciliation | null; conditions: string[] }
interface Detail {
  run: Run; tasks: { id: string; role: string; status: string; step: string; lease_generation: number }[];
  sessions: { id: string; role: string; status: string; context: Record<string, unknown> }[];
  decisions: { id: string; session_id: string; step: string; action: string; summary: string; expected: string; stop_condition: string }[];
  calls: Call[]; budget: { reserved_tool_calls: number; settled_tool_calls: number; max_tool_calls: number; output_bytes: number };
  interruptions: { reason_code: string; recovery_condition: string; created_at: string }[]; heartbeat_at: string | null; cursor: number;
  // The execution side's own account of this Run's containers and gateways, or `null` when it
  // could not be asked. The two are rendered differently on purpose.
  runtime: RunRuntime | null;
}
interface PendingCall { id: string; status: string; conditions: string[] }
interface ResumePreview { version: number; last_completed_step: string | null; pending_calls: PendingCall[]; remaining_tool_calls: number; authorization_valid: boolean; can_resume: boolean; expected_actions: string[]; reason_code: string | null }
interface EvidenceView { id?: string; content?: string; sha256?: string; hash?: string; available?: boolean; availability?: string; truncated?: boolean; redacted?: boolean; size?: number; size_bytes?: number; reason_code?: string; missing_reason?: string | null }
const props = defineProps<{ run: Run }>();
const emit = defineEmits<{ updated: [run: Run] }>();
const workspace = useWorkspace();
const detail = ref<Detail | null>(null), events = ref<AuditEvent[]>([]), error = ref(''), busy = ref(false);
const connection = ref('正在连接'), lastHeartbeat = ref<string | null>(null), preview = ref<ResumePreview | null>(null);
const evidence = ref<EvidenceView | null>(null), evidenceId = ref(''), evidenceOffset = ref(0);
let source: EventSource | null = null, timer: ReturnType<typeof setInterval> | null = null, generation = 0;
// A ticking clock so "running for 42s" advances between polls without inventing progress. It only
// re-reads elapsed time; it never estimates how much of the action is done.
const now = ref(Date.now());
let clock: ReturnType<typeof setInterval> | null = null;
const time = (value: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value)) : '尚无心跳';
const stamp = (value: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value)) : '无法确认';
// How long a call has been going, and how long it has been silent. Rendered as numbers, never as a
// percentage: the platform does not know what fraction of an action is complete (issue #19).
function elapsed(call: Call): string {
  const from = call.progress?.started_at || call.created_at;
  const to = call.progress?.status === 'ended' && call.progress.elapsed_ms ? null : now.value;
  const ms = to === null ? call.progress?.elapsed_ms ?? 0 : new Date(to).getTime() - new Date(from).getTime();
  return duration(ms);
}
function silent(call: Call): string {
  if (call.progress?.last_output_at) return `${duration(now.value - new Date(call.progress.last_output_at).getTime())} 前`;
  return call.progress?.status === 'running' || call.progress?.status === 'accepted' ? '尚无输出' : '无输出';
}
function duration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 60) return `${total} 秒`;
  return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
}
const status = computed(() => detail.value?.run.status || props.run.status);
const phase = computed(() => detail.value?.run.phase || props.run.phase);
const canPause = computed(() => ['queued', 'running'].includes(status.value));
const canCancel = computed(() => ['draft', 'queued', 'running', 'waiting', 'recovering', 'pausing', 'paused'].includes(status.value));
const showResume = computed(() => status.value === 'paused' || (status.value === 'waiting' && phase.value !== 'awaiting_human'));
// The execution mode and its blocking reason come from the capability answer; the per-Run
// readiness comes from this Run's own view, so a lost execution side shows up within one poll.
const executionNotice = computed(() => {
  if (workspace.realExecutionReady) return '这是真实执行：动作、输出与证据来自实际执行端，仍受本次授权与预算约束。';
  const reason = workspace.readinessReason;
  return `这是开发演示：参数与输出来自固定假动作，不连接授权 IP，不形成真实漏洞结论。真实执行未开放${reason ? `，原因：${reason}` : '。'}决策摘要仅记录依据、预期与停止条件。`;
});
const executionUnavailable = computed(() => !!detail.value && (!detail.value.run.execution_ready || !workspace.fakeExecutionReady));
// The four views the design keeps apart, shown side by side rather than folded into one light.
// There is deliberately no single health indicator and no "safety percentage" here.
const management = computed(() => managementLabels[workspace.capabilities?.sandbox_management || 'disabled'] || '未知');
const viewStates = computed(() => [
  { key: 'execution', label: '执行', value: workspace.realExecutionReady ? '真实执行就绪' : workspace.fakeExecutionReady ? '固定假动作' : '执行端不可用' },
  { key: 'research', label: '研究', value: `${detail.value?.tasks.filter(task => task.status === 'running').length || 0} 个任务进行中` },
  { key: 'review', label: '复审', value: '未实现（P2）' },
  { key: 'delivery', label: '交付', value: '未实现（P2）' },
]);
// Anything the Run owes an operator: unconfirmed stops, unreconciled calls, unknown results. Kept
// always visible, because an unreconciled call is exactly what must not scroll out of sight.
const stopUnconfirmed = computed(() => (detail.value?.calls || []).filter(call => call.observation && !call.observation.stop_confirmed && call.observation.started));
const openConditions = computed(() => (detail.value?.calls || []).filter(call => call.conditions.length));
// Quarantine is derived from the execution facts the platform already reports: an instance whose
// stop is unconfirmed, or a call whose stop never got confirmed, is something the Run cannot treat
// as finished. The recovery condition is stated rather than summarised as a colour.
const quarantined = computed(() => (detail.value?.runtime?.sessions || []).flatMap(session => session.instances).filter(instance => instance.stop_state === 'unconfirmed'));
// Calls still holding an execution slot: accepted or running, plus any whose stop is unconfirmed.
// This is the same rule the capacity ledger uses — an unverified stop keeps its slot — stated in
// the console as an occupancy count and never as a promise of how much capacity is free.
const occupying = computed(() => (detail.value?.calls || []).filter(call => ['planned', 'dispatched', 'running', 'cancelling'].includes(call.status) || (call.observation?.started && !call.observation.stop_confirmed)));
const evidenceSummary = (call: Call) => {
  const items = call.result?.evidence || [];
  return {
    total: items.length,
    truncated: items.filter(item => item.truncated).length,
    redacted: items.filter(item => item.redacted).length,
    unavailable: items.filter(item => item.available === false).length,
  };
};
const callStateLabels: Record<string, string> = {
  planned: '已计划 · 尚未派发', dispatched: '已派发 · 等待执行端', running: '执行中', cancelling: '停止中 · 等待执行端确认',
  succeeded: '已成功', failed: '已失败', cancelled: '已取消', denied: '被拒绝 · 未执行', incomplete: '执行已发生 · 结果不完整', unknown: '结果未知 · 待核对',
};
const callState = (call: Call) => callStateLabels[call.status] || call.status;
const stopStateLabels: Record<string, string> = { never_started: '从未启动', running: '仍在运行', confirmed: '已确认停止', unconfirmed: '停止未确认' };
const instanceStateLabels: Record<string, string> = { creating: '创建中', ready: '就绪', stopped: '已停止（未确认）', reclaimed: '已回收', interrupted: '创建中断' };
const networkLabels: Record<string, string> = { none: '无出口（无法连接任何目标）', internal: '内部网络 · 由网关按授权放行', bridge: '桥接网络' };
const text = (value: string | null | undefined) => value || '无法确认';
const reason = (code: string | null | undefined) => code ? reasonText(code) : '';
async function refresh(epoch = generation) {
  try {
    const result = await workspace.api<Detail>(`/api/v1/runs/${props.run.id}/snapshot`);
    if (epoch !== generation) return;
    detail.value = result; emit('updated', result.run);
  } catch (e) { if (epoch === generation) error.value = e instanceof Error ? e.message : '状态读取失败。'; }
}
function stop() { source?.close(); source = null; if (timer) clearInterval(timer); timer = null; if (clock) clearInterval(clock); clock = null; }
watch(() => props.run.id, async () => {
  stop(); const epoch = ++generation; detail.value = null; events.value = []; preview.value = null; evidence.value = null; error.value = ''; connection.value = '正在连接';
  await refresh(epoch); if (epoch !== generation) return;
  try {
    let cursor = 0;
    // Page through committed history before connecting from its final cursor.
    for (;;) {
      const history = await workspace.api<{ events: AuditEvent[]; next_cursor: number; gap: boolean }>(`/api/v1/runs/${props.run.id}/event-history?after=${cursor}&limit=100`);
      if (epoch !== generation) return;
      if (history.gap) error.value = '历史游标已过期，存在事件缺口；当前状态已重新读取。';
      events.value.push(...history.events);
      if (history.next_cursor <= cursor || history.events.length < 100) { cursor = history.next_cursor; break; }
      cursor = history.next_cursor;
    }
    source = new EventSource(`/api/v1/runs/${props.run.id}/events?after=${cursor}`);
    source.onopen = () => { connection.value = '已连接'; };
    source.onerror = () => { connection.value = '连接中断，正在补拉'; };
    source.addEventListener('audit', (message) => {
      const event = JSON.parse((message as MessageEvent).data) as AuditEvent;
      if (!events.value.some(item => item.cursor === event.cursor)) events.value.push(event);
      void refresh(epoch);
    });
    source.addEventListener('heartbeat', () => { lastHeartbeat.value = new Date().toISOString(); });
    source.addEventListener('gap', () => { error.value = '事件存在缺口，请刷新查看当前状态。'; void refresh(epoch); });
    source.addEventListener('session_expired', () => { stop(); window.location.replace('/login'); });
    timer = setInterval(() => { void refresh(epoch); }, 5000);
    clock = setInterval(() => { now.value = Date.now(); }, 1000);
  } catch (e) { error.value = e instanceof Error ? e.message : '时间线读取失败。'; }
}, { immediate: true });
onBeforeUnmount(() => { generation++; stop(); });
// A control request the server refused because the Run moved on. The operator's decision must
// land on the state they actually saw, so the console shows what changed and waits for them
// instead of re-sending the action from a version it read behind their back.
const conflict = ref<{ action: string; seenVersion: number; current: Run } | null>(null);
const actionLabels: Record<string, string> = { pause: '暂停', cancel: '取消', close: '结束演示', resume: '恢复' };
const actionLabel = (action: string) => actionLabels[action] || action;
async function control(action: string) {
  busy.value = true; error.value = '';
  try {
    const run = await workspace.api<Run>(`/api/v1/runs/${props.run.id}/${action}`, { version: detail.value?.run.version || props.run.version });
    conflict.value = null; emit('updated', run); preview.value = null; await refresh();
  } catch (failure) {
    if (failure instanceof ApiFailure && failure.reasonCode === 'version_conflict') {
      const seenVersion = detail.value?.run.version || props.run.version;
      await refresh();
      conflict.value = { action, seenVersion, current: detail.value?.run || props.run };
    } else {
      error.value = failure instanceof Error ? failure.message : '操作失败。';
      await refresh();
    }
  } finally { busy.value = false; }
}
async function confirmConflict() {
  const pending = conflict.value;
  if (!pending) return;
  conflict.value = null;
  await control(pending.action);
}
async function resumePreview() {
  busy.value = true;
  try { preview.value = await workspace.api<ResumePreview>(`/api/v1/runs/${props.run.id}/resume-preview`); }
  catch (e) { error.value = e instanceof Error ? e.message : '恢复预览失败。'; }
  finally { busy.value = false; }
}
async function readEvidence(id: string, offset = 0) {
  try { evidence.value = await workspace.api<EvidenceView>(`/api/v1/evidence/${encodeURIComponent(id)}?offset=${offset}&limit=65536`); evidenceId.value = id; evidenceOffset.value = offset; }
  catch (e) { error.value = e instanceof Error ? e.message : '证据读取失败。'; }
}
// Reconciliation of a call whose outcome the execution ledger never confirmed. The verdict
// is the operator's, bound to the evidence they cite; it never restates an execution fact.
const reconcilable = computed(() => (detail.value?.calls || []).filter(call => call.status === 'unknown' || call.reconciliation));
const notes = ref<Record<string, string>>({}), citations = ref<Record<string, string>>({});
const conditionLabels: Record<string, string> = { outcome_unsettled: '结果未裁定', stop_unconfirmed: '停止未确认', call_pending: '调用进行中' };
const outcomeLabels: Record<string, string> = { not_executed: '确认未执行', executed: '确认已执行', undetermined: '仍未决' };
const conditionLabel = (value: string) => conditionLabels[value] || value;
const stateLabel = (value: boolean | null | undefined) => value === true ? '仍在' : value === false ? '已结束' : '无法确认';
const leaseLabel = (value: boolean | null | undefined) => value === true ? '仍有效' : value === false ? '已失效' : '无法确认';
async function reconcile(call: Call, outcome: string) {
  busy.value = true; error.value = '';
  const cited = (citations.value[call.id] || '').split(',').map(item => item.trim()).filter(Boolean);
  try {
    const result = await workspace.api<{ run: Run; call: Call }>(`/api/v1/runs/${props.run.id}/calls/${call.id}/reconciliation`, {
      outcome, version: detail.value?.run.version || props.run.version, evidence_ids: cited, note: notes.value[call.id] || '',
    });
    emit('updated', result.run); notes.value[call.id] = ''; citations.value[call.id] = '';
    await refresh();
  } catch (e) {
    // A stale version is shown and read again, never re-sent behind the operator's back:
    // the verdict must land on the state they actually saw.
    error.value = e instanceof Error ? e.message : '核对失败。';
    await refresh();
  } finally { busy.value = false; }
}
</script>

<template>
  <section class="panel execution-console" aria-label="玻璃鱼缸执行台">
    <div class="section-heading"><h2>03 / 玻璃鱼缸</h2><span class="badge">{{ workspace.realExecutionReady ? '真实执行' : '固定假动作' }}</span></div>
    <p class="muted">{{ connection }} · 执行心跳 {{ time(detail?.heartbeat_at || null) }} · 流心跳 {{ time(lastHeartbeat) }}</p>
    <div class="state-grid" data-testid="run-state-grid">
      <div v-for="view in viewStates" :key="view.key" class="state-cell"><small>{{ view.label }}</small><strong>{{ view.value }}</strong></div>
      <div class="state-cell"><small>容器管理能力</small><strong>{{ management }}</strong></div>
      <div class="state-cell"><small>本次占用（控制槽与物理额度）</small><strong>{{ occupying.length }} 个调用仍占用</strong></div>
    </div>
    <p v-if="stopUnconfirmed.length" class="error" role="alert" data-testid="stop-unconfirmed">
      停止未确认（{{ stopUnconfirmed.length }} 个调用）：执行端没有证明这些调用的进程与连接已经结束。
      它们的最后实例与调用仍被记名，占用的控制槽与物理执行额度尚未释放，界面也不会显示为已回收或已取消。
      恢复条件是执行端给出受信停止事实（实例记录里的确认），或按核对入口依据证据裁定。
    </p>
    <p v-if="quarantined.length" class="notice" role="alert" data-testid="instance-quarantine">
      实例隔离（{{ quarantined.length }} 个）：{{ quarantined.map(item => `${item.instance_id.slice(0, 8)}… · ${instanceStateLabels[item.state] || item.state} · ${stopStateLabels[item.stop_state]}`).join('；') }}。
      在停止确认之前只允许读取平台已有证据与受信管理核对，不再发起新的目标请求；解除条件是停止事实、未知结果核对与新动作相关性分别校验通过。
    </p>
    <p v-if="openConditions.length" class="notice" role="region" aria-label="未决调用">
      未决调用（{{ openConditions.length }} 个），仍阻塞该 Run 的继续与结束：{{ openConditions.map(call => `${call.action} · ${callState(call)} · 缺 ${call.conditions.map(conditionLabel).join('、')}`).join('；') }}。
    </p>
    <p class="notice" data-testid="console-execution-mode">{{ executionNotice }}</p>
    <p v-if="executionUnavailable" class="error" role="alert" data-testid="execution-unavailable">执行端未就绪：当前 Run 的固定假动作链路不可用，状态可能不会推进。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-if="conflict" class="notice" role="alert" data-testid="version-conflict">
      <strong>状态已变化，操作未执行</strong>
      <p>你点击时的状态版本是 {{ conflict.seenVersion }}；现在为 {{ statusText(conflict.current) }}（版本 {{ conflict.current.version }}）{{ conflict.current.phase ? ` · 阶段 ${conflict.current.phase}` : '' }}。</p>
      <p>控制台已读取新状态，但不会替你重发这次{{ actionLabel(conflict.action) }}。</p>
      <div class="control-actions">
        <button :disabled="busy" @click="confirmConflict">按新状态确认{{ actionLabel(conflict.action) }}（版本 {{ conflict.current.version }}）</button>
        <button class="secondary" :disabled="busy" @click="conflict = null">放弃</button>
      </div>
    </div>
    <div class="control-actions">
      <button v-if="canPause" :disabled="busy" @click="control('pause')">暂停 Run</button>
      <button v-if="canCancel" class="secondary" :disabled="busy" @click="control('cancel')">取消 Run</button>
      <button v-if="showResume" :disabled="busy" @click="resumePreview">查看恢复预览</button>
      <button v-if="status === 'waiting' && phase === 'awaiting_human'" :disabled="busy" @click="control('close')">结束演示</button>
    </div>
    <p v-if="['pausing', 'cancelling'].includes(status)" role="status">正在等待执行端确认收尾；状态尚未完成。</p>
    <div v-if="preview" class="preview" role="region" aria-label="恢复预览">
      <h3>恢复预览</h3><p>上次完成：{{ preview.last_completed_step || '尚未完成动作' }} · 剩余 {{ preview.remaining_tool_calls }} 次调用 · 授权{{ preview.authorization_valid ? '有效' : '无效' }}</p>
      <p>待核对调用：{{ preview.pending_calls.length }}</p><ul class="pending-calls"><li v-for="item in preview.pending_calls" :key="item.id">{{ item.status }} · 缺：{{ item.conditions.map(conditionLabel).join('、') }} · {{ item.id }}</li></ul>
      <p>预计动作：{{ preview.expected_actions.join('、') || '暂无可执行动作' }}</p>
      <p v-if="preview.reason_code">原因：{{ reason(preview.reason_code) }}（{{ preview.reason_code }}）</p>
      <button :disabled="busy || !preview.can_resume" @click="control('resume')">按预览恢复</button>
    </div>
    <div v-if="detail" class="console-content">
      <h3>会话容器与网关</h3>
      <p v-if="!detail.runtime" class="notice">执行端未回答容器与网关状态；这不等同于「当前没有容器在运行」。</p>
      <p v-else-if="!detail.runtime.available" class="notice" data-testid="runtime-unavailable">
        无法读取容器与网关状态：{{ reason(detail.runtime.reason_code) }}（容器管理能力：{{ managementLabels[detail.runtime.sandbox_management] || detail.runtime.sandbox_management }}）。
        这是观测缺口，不是「没有资源在运行」。
      </p>
      <template v-else>
        <p class="muted">执行 profile {{ detail.runtime.profile_id || '未知' }} · 观测于 {{ stamp(detail.runtime.observed_at) }}。以下是执行端记录的容器与网关，不是控制台的推断。</p>
        <div v-if="!detail.runtime.sessions.length" class="notice">执行端记录里本 Run 没有会话或实例：本次 Run 尚未创建过容器。</div>
        <article v-for="session in detail.runtime.sessions" :key="session.session_id" class="session-card">
          <div class="section-heading"><strong>执行会话 {{ session.session_id.slice(0, 8) }}…</strong><span class="badge">{{ session.instances.length }} 个实例</span></div>
          <small>范围版本 {{ session.scope_version }} · 策略版本 {{ session.policy_version }} · 创建 {{ stamp(session.created_at) }}</small>
          <p v-if="!session.instances.length" class="muted">该会话当前没有实例记录。</p>
          <article v-for="instance in session.instances" :key="instance.instance_id" class="instance-card">
            <div class="section-heading"><strong>实例 {{ instance.instance_id.slice(0, 8) }}…</strong><span :class="['badge', instance.stop_state === 'unconfirmed' ? 'badge-warn' : '']">{{ instanceStateLabels[instance.state] || instance.state }} · {{ stopStateLabels[instance.stop_state] }}</span></div>
            <dl>
              <dt>网络模式</dt><dd>由 profile 决定（{{ detail.runtime.profile_id || '未知 profile' }}）；网关按授权逐条放行，未列出的目标默认拒绝</dd>
              <dt>已放行目标</dt><dd>{{ (instance.egress.authorized || []).map(item => `${item.address}:${item.port}`).join('、') || '无（默认拒绝）' }}</dd>
              <dt>放行时刻</dt><dd>生效 {{ stamp(instance.egress.applied_at || null) }} · 撤销 {{ stamp(instance.egress.revoked_at || null) }}{{ instance.egress.revocation_reason ? ` · 原因 ${reason(instance.egress.revocation_reason)}` : '' }}</dd>
              <dt>网关规则回读</dt><dd>{{ instance.egress.observation || '未记录回读差异' }}</dd>
              <dt>环境清单</dt><dd>{{ instance.environment.profile_id || '未知' }} v{{ instance.environment.profile_version ?? '?' }} · {{ instance.environment.engine || '未知引擎' }}/{{ instance.environment.architecture || '未知架构' }}</dd>
              <dt>镜像 digest</dt><dd class="hash-list">{{ Object.entries(instance.environment.image_digests || {}).map(([role, digest]) => `${role}: ${digest}`).join(' · ') || '未记录' }}</dd>
              <dt>工具清单</dt><dd>{{ (instance.environment.tool_inventory || []).join('、') || '该 profile 未声明工具（生命周期检查）' }}</dd>
              <dt>控制租约</dt><dd>{{ stamp(instance.lease_expires_at) }}{{ instance.halt_reason ? ` · 停止原因 ${reason(instance.halt_reason)}` : '' }}</dd>
              <dt>受管资源</dt><dd>{{ instance.resources.map(item => `${item.role || item.kind}(${item.running === null ? '状态未知' : item.running ? '运行中' : '已停止'})`).join('、') || '无' }}</dd>
            </dl>
            <p v-if="instance.stop_state === 'unconfirmed'" class="error" role="alert">该实例的停止未被确认：不显示为已回收，其占用的物理执行额度在受信停止事实出现前不归还。</p>
          </article>
        </article>
        <div v-if="detail.runtime.unaccounted.length || detail.runtime.missing.length || detail.runtime.interrupted.length" class="notice" role="alert" data-testid="runtime-drift">
          <strong>账本与运行时不一致</strong>
          <p v-if="detail.runtime.unaccounted.length">运行时仍有账本未记录的资源：{{ detail.runtime.unaccounted.map(item => item.name).join('、') }}</p>
          <p v-if="detail.runtime.missing.length">账本记录的资源已在运行时消失：{{ detail.runtime.missing.map(item => item.name).join('、') }}</p>
          <p v-if="detail.runtime.interrupted.length">创建中断的实例：{{ detail.runtime.interrupted.map(id => id.slice(0, 8)).join('、') }}</p>
        </div>
        <details class="notice" data-testid="reclaim-preview">
          <summary>回收预览（{{ detail.runtime.reclaimable.length }} 个实例，只预览不执行）</summary>
          <ul><li v-for="item in detail.runtime.reclaimable" :key="item.instance_id">
            {{ item.instance_id.slice(0, 8) }}… · {{ item.state }} · 将移除 {{ item.resources.map(resource => resource.role || resource.kind).join('、') || '无' }}{{ item.confirmation_required ? ' · 仍需停止确认，未确认前不得视为可回收' : '' }}
          </li></ul>
          <p class="muted">回收动作本身归选择性保留切片；本区块只陈述执行端记录的可回收范围。</p>
        </details>
      </template>
      <div class="budget-strip"><span>累计调用预留 <strong>{{ detail.budget.reserved_tool_calls }}</strong></span><span>已结算 <strong>{{ detail.budget.settled_tool_calls }} / {{ detail.budget.max_tool_calls }}</strong></span><span>输出 <strong>{{ detail.budget.output_bytes }} B</strong></span><span>仍占用执行额度 <strong>{{ occupying.length }}</strong></span></div>
      <p class="muted">控制槽与物理执行额度相互独立；额度耗尽时新目标执行背压，取消、核对与回收仍可用。容量只由受信停止事实归还（归 #21 的压力验收）。</p>
      <h3>研究任务</h3><ul class="task-list"><li v-for="task in detail.tasks" :key="task.id"><strong>{{ task.role }}</strong><span>{{ task.status }} · {{ task.step }}</span><small>代次 {{ task.lease_generation }} · {{ task.id }}</small></li></ul>
      <div v-for="item in detail.interruptions" :key="item.created_at" class="notice"><strong>{{ reason(item.reason_code) }}</strong><p>{{ item.recovery_condition }}</p><small>{{ item.reason_code }}</small></div>
      <h3>决策摘要</h3><details v-for="decision in detail.decisions" :key="decision.id"><summary>{{ decision.action }} · {{ decision.step }}</summary><p>{{ decision.summary }}</p><p>预期：{{ decision.expected }}</p><p>停止条件：{{ decision.stop_condition }}</p><small>{{ decision.id }} · {{ decision.session_id }}</small></details>
      <h3>实际调用与证据</h3>
      <p class="muted">每个调用分开陈述：命令与规范化参数 hash、执行身份与环境版本、以及与执行端事实不同的计划。模型说明只作为「依据摘要」显示，不替代实际命令。</p>
      <article v-for="call in detail.calls" :key="call.id" class="call-card">
        <div class="section-heading"><strong>{{ call.action }}</strong><span class="badge">{{ callState(call) }}</span></div>
        <small>{{ call.id }} · {{ time(call.created_at) }}<template v-if="call.replaces_call_id"> · 替代 {{ call.replaces_call_id }}</template></small>
        <p v-if="call.progress" class="call-progress" data-testid="call-progress">
          <template v-if="call.progress.status === 'running' || call.progress.status === 'accepted'">运行时长 {{ elapsed(call) }} · 上次输出 {{ silent(call) }} · 该动作时限 {{ call.progress.timeout_seconds }} 秒 · 已归档 {{ call.progress.output_bytes }} B</template>
          <template v-else>总耗时 {{ duration(call.progress.elapsed_ms ?? 0) }} · 该动作时限 {{ call.progress.timeout_seconds }} 秒 · 已归档 {{ call.progress.output_bytes }} B</template>
          <span class="muted">（不显示进度百分比：平台没有测量动作完成度）</span>
        </p>
        <p v-if="!call.runtime" class="notice">该调用没有执行端的运行环境陈述（早于本接口的记录）；下方只显示票据参数，工作目录、执行身份与环境版本无法确认。</p>
        <template v-else>
          <p class="call-binding">
            目标 <code>{{ call.runtime.target_ip }}:{{ call.runtime.target_port }}</code> ·
            参数 hash <code>{{ call.runtime.parameters_hash.slice(0, 16) }}…</code> ·
            执行 profile {{ call.runtime.execution_profile }} ·
            工作目录 <code>{{ text(call.runtime.cwd) }}</code> ·
            执行身份 <code>{{ text(call.runtime.user) }}</code>
          </p>
          <p class="call-binding">
            实例 <code>{{ call.runtime.instance_id ? call.runtime.instance_id.slice(0, 8) + '…' : '本进程内固定夹具（无容器）' }}</code> ·
            网络模式 {{ networkLabels[call.runtime.network_mode] }} ·
            已放行 {{ call.runtime.authorized.join('、') || '无' }} ·
            网关 {{ call.runtime.gateway_ids.length }} 个
          </p>
          <p class="call-binding">
            工具环境 {{ call.runtime.engine || '未知引擎' }}/{{ call.runtime.architecture || '未知架构' }} · profile v{{ call.runtime.profile_version ?? '?' }} ·
            镜像 digest {{ Object.keys(call.runtime.image_digests).length }} 项 ·
            工具清单 {{ call.runtime.tool_inventory.join('、') || '未声明' }}
          </p>
          <details v-if="call.runtime.argv.length" open><summary>实际 argv（由动作与票据组合）</summary><pre>{{ call.runtime.argv.join(' ') }}</pre></details>
          <p v-else class="muted">该执行端不组合 argv（固定夹具没有命令行可展示）。</p>
        </template>
        <details open><summary>票据参数（规范化前）</summary><pre>{{ JSON.stringify(call.parameters, null, 2) }}</pre></details>
        <template v-if="call.result">
          <p v-if="call.result.reason_code" class="notice">执行端结论原因：{{ reason(call.result.reason_code) }}</p>
          <p v-if="call.result.truncated || call.result.redacted" class="notice" data-testid="call-evidence-flags">
            该调用的归档不完整：{{ call.result.truncated ? '输出被本次 Run 的保留上限截断' : '' }}{{ call.result.truncated && call.result.redacted ? '；' : '' }}{{ call.result.redacted ? '含凭据形态内容已脱敏，原值未保存' : '' }}。未保留的部分不作为已观察事实。
          </p>
          <p class="muted">证据 {{ evidenceSummary(call).total }} 项 · 截断 {{ evidenceSummary(call).truncated }} · 脱敏 {{ evidenceSummary(call).redacted }} · 不可读 {{ evidenceSummary(call).unavailable }}</p>
          <details><summary>执行结果 / 退出码 / 摘要</summary><pre>{{ JSON.stringify({ output: call.result.output, exit_code: call.result.exit_code, summary: call.result.summary, duration_ms: call.result.duration_ms }, null, 2) }}</pre></details>
        </template>
        <button v-for="id in call.evidence_ids" :key="id" class="quiet" @click="readEvidence(id)">查看原始证据</button>
      </article>
      <div v-if="reconcilable.length" class="reconciliation" role="region" aria-label="待核对调用">
        <h3>待核对调用</h3>
        <p class="notice">执行端无法确认这些调用是否已经执行。下面的裁定是操作员依据证据作出的业务记录，不是执行端结论，也不会替代执行端对进程、连接与租约的停止确认。</p>
        <article v-for="call in reconcilable" :key="call.id" class="call-card">
          <div class="section-heading"><strong>{{ call.action }}</strong><span class="badge">{{ callState(call) }}</span></div>
          <small>{{ call.id }} · {{ time(call.created_at) }}</small>
          <p>执行端事实（来自执行账本）：已开始 {{ call.observation?.started === undefined ? '无法确认' : (call.observation?.started ? '是' : '否') }} · 进程 {{ stateLabel(call.observation?.process_active) }} · 连接 {{ stateLabel(call.observation?.connection_open) }} · 旧租约 {{ leaseLabel(call.observation?.lease_active) }} · 停止确认 {{ call.observation?.stop_confirmed ? '已确认' : '未确认' }}</p>
          <p>仍缺条件：{{ call.conditions.length ? call.conditions.map(conditionLabel).join('、') : '无，可按预览继续' }}</p>
          <div v-if="call.reconciliation" class="notice">
            <strong>操作员裁定：{{ outcomeLabels[call.reconciliation.outcome] || call.reconciliation.outcome }}</strong>
            <p>{{ call.reconciliation.note || '（未填写说明）' }}</p>
            <p>依据证据 {{ call.reconciliation.evidence_ids.length }} 条 · 范围版本 {{ call.reconciliation.scope_version }} · 允许重派 {{ call.reconciliation.redispatch_authorized ? '是' : '否' }}</p>
            <small>{{ call.reconciliation.operator_session_id }} · {{ time(call.reconciliation.recorded_at) }}</small>
          </div>
          <template v-else>
            <input v-model="notes[call.id]" aria-label="裁定说明" placeholder="裁定说明（可选）" />
            <input v-model="citations[call.id]" aria-label="依据证据" placeholder="依据证据 ID，逗号分隔（可选）" />
            <div class="control-actions">
              <button :disabled="busy" @click="reconcile(call, 'not_executed')">确认未执行</button>
              <button :disabled="busy" @click="reconcile(call, 'executed')">确认已执行</button>
              <button class="secondary" :disabled="busy" @click="reconcile(call, 'undetermined')">仍未决</button>
            </div>
          </template>
        </article>
      </div>
      <div v-if="evidence" class="evidence-view" role="region" aria-label="原始证据">
        <h3>原始证据</h3>
        <small>{{ evidenceId }} · 偏移 {{ evidenceOffset }} · SHA-256 {{ evidence.sha256 || evidence.hash }}</small>
        <p :class="evidence.available === false || evidence.availability === 'missing' ? 'error' : ''">
          可用：{{ evidence.available ?? evidence.availability }} · 截断：{{ evidence.truncated ? '是（未保留的部分不作为已观察事实）' : '否' }} · 脱敏：{{ evidence.redacted ? '是（原值未保存）' : '否' }} · 大小 {{ evidence.size_bytes ?? evidence.size ?? '未知' }} B
          <template v-if="evidence.missing_reason"> · 缺失原因：{{ reason(evidence.missing_reason) }}</template>
        </p>
        <pre>{{ evidence.content }}</pre>
        <button v-if="evidenceOffset > 0" class="quiet" @click="readEvidence(evidenceId, Math.max(0, evidenceOffset - 65536))">上一段</button><button v-if="evidence.content && evidence.content.length >= 65536" class="quiet" @click="readEvidence(evidenceId, evidenceOffset + 65536)">下一段</button>
      </div>
      <h3>事件时间线</h3><ol class="event-list"><li v-for="event in events" :key="event.cursor"><div><span class="muted">#{{ event.cursor }} · {{ time(event.created_at) }}</span> <strong>{{ event.type }}</strong></div><pre>{{ JSON.stringify(event.payload, null, 2) }}</pre></li></ol>
    </div>
  </section>
</template>
