<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useWorkspace, ApiFailure, statusText, type Run } from './workspace';

interface AuditEvent { cursor: number; type: string; payload: Record<string, unknown>; created_at: string }
interface Observation { started: boolean; process_active: boolean | null; connection_open: boolean | null; lease_active: boolean | null; observed_at: string; stop_confirmed: boolean }
interface Reconciliation { call_id: string; outcome: string; operator_session_id: string; scope_version: number; evidence_ids: string[]; note: string; observation: Observation | null; redispatch_authorized: boolean; recorded_at: string }
interface Call { id: string; session_id: string; decision_id: string; status: string; action: string; parameters: Record<string, unknown>; result: Record<string, unknown> | null; evidence_ids: string[]; created_at: string; replaces_call_id: string | null; observation: Observation | null; reconciliation: Reconciliation | null; conditions: string[] }
interface Detail {
  run: Run; tasks: { id: string; role: string; status: string; step: string; lease_generation: number }[];
  sessions: { id: string; role: string; status: string; context: Record<string, unknown> }[];
  decisions: { id: string; session_id: string; step: string; action: string; summary: string; expected: string; stop_condition: string }[];
  calls: Call[]; budget: { reserved_tool_calls: number; settled_tool_calls: number; max_tool_calls: number; output_bytes: number };
  interruptions: { reason_code: string; recovery_condition: string; created_at: string }[]; heartbeat_at: string | null; cursor: number;
}
interface PendingCall { id: string; status: string; conditions: string[] }
interface ResumePreview { version: number; last_completed_step: string | null; pending_calls: PendingCall[]; remaining_tool_calls: number; authorization_valid: boolean; can_resume: boolean; expected_actions: string[]; reason_code: string | null }
interface EvidenceView { id?: string; content?: string; sha256?: string; hash?: string; available?: boolean; availability?: string; truncated?: boolean; redacted?: boolean; size?: number; reason_code?: string; missing_reason?: string | null }
const props = defineProps<{ run: Run }>();
const emit = defineEmits<{ updated: [run: Run] }>();
const workspace = useWorkspace();
const detail = ref<Detail | null>(null), events = ref<AuditEvent[]>([]), error = ref(''), busy = ref(false);
const connection = ref('正在连接'), lastHeartbeat = ref<string | null>(null), preview = ref<ResumePreview | null>(null);
const evidence = ref<EvidenceView | null>(null), evidenceId = ref(''), evidenceOffset = ref(0);
let source: EventSource | null = null, timer: ReturnType<typeof setInterval> | null = null, generation = 0;
const time = (value: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value)) : '尚无心跳';
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
async function refresh(epoch = generation) {
  try {
    const result = await workspace.api<Detail>(`/api/v1/runs/${props.run.id}/snapshot`);
    if (epoch !== generation) return;
    detail.value = result; emit('updated', result.run);
  } catch (e) { if (epoch === generation) error.value = e instanceof Error ? e.message : '状态读取失败。'; }
}
function stop() { source?.close(); source = null; if (timer) clearInterval(timer); timer = null; }
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
      <p v-if="preview.reason_code">原因：{{ preview.reason_code }}</p>
      <button :disabled="busy || !preview.can_resume" @click="control('resume')">按预览恢复</button>
    </div>
    <div v-if="detail" class="console-content">
      <div class="budget-strip"><span>累计调用预留 <strong>{{ detail.budget.reserved_tool_calls }}</strong></span><span>已结算 <strong>{{ detail.budget.settled_tool_calls }} / {{ detail.budget.max_tool_calls }}</strong></span><span>输出 <strong>{{ detail.budget.output_bytes }} B</strong></span></div>
      <h3>研究任务</h3><ul class="task-list"><li v-for="task in detail.tasks" :key="task.id"><strong>{{ task.role }}</strong><span>{{ task.status }} · {{ task.step }}</span><small>代次 {{ task.lease_generation }} · {{ task.id }}</small></li></ul>
      <div v-for="item in detail.interruptions" :key="item.created_at" class="notice"><strong>{{ item.reason_code }}</strong><p>{{ item.recovery_condition }}</p></div>
      <h3>决策摘要</h3><details v-for="decision in detail.decisions" :key="decision.id"><summary>{{ decision.action }} · {{ decision.step }}</summary><p>{{ decision.summary }}</p><p>预期：{{ decision.expected }}</p><p>停止条件：{{ decision.stop_condition }}</p><small>{{ decision.id }} · {{ decision.session_id }}</small></details>
      <h3>实际调用与证据</h3><article v-for="call in detail.calls" :key="call.id" class="call-card"><div class="section-heading"><strong>{{ call.action }}</strong><span class="badge">{{ call.status }}</span></div><small>{{ call.id }} · {{ time(call.created_at) }}<template v-if="call.replaces_call_id"> · 替代 {{ call.replaces_call_id }}</template></small><pre>{{ JSON.stringify(call.parameters, null, 2) }}</pre><details v-if="call.result"><summary>执行结果 / 耗时 / 退出码 / 来源</summary><pre>{{ JSON.stringify(call.result, null, 2) }}</pre></details><button v-for="id in call.evidence_ids" :key="id" class="quiet" @click="readEvidence(id)">查看原始证据</button></article>
      <div v-if="reconcilable.length" class="reconciliation" role="region" aria-label="待核对调用">
        <h3>待核对调用</h3>
        <p class="notice">执行端无法确认这些调用是否已经执行。下面的裁定是操作员依据证据作出的业务记录，不是执行端结论，也不会替代执行端对进程、连接与租约的停止确认。</p>
        <article v-for="call in reconcilable" :key="call.id" class="call-card">
          <div class="section-heading"><strong>{{ call.action }}</strong><span class="badge">{{ call.status }}</span></div>
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
      <div v-if="evidence" class="evidence-view" role="region" aria-label="原始证据"><h3>原始证据</h3><small>{{ evidenceId }} · 偏移 {{ evidenceOffset }} · SHA-256 {{ evidence.sha256 || evidence.hash }}</small><p>可用：{{ evidence.available ?? evidence.availability }} · 截断：{{ evidence.truncated ? '是' : '否' }} · 脱敏：{{ evidence.redacted ? '是' : '否' }}</p><pre>{{ evidence.content }}</pre><button v-if="evidenceOffset > 0" class="quiet" @click="readEvidence(evidenceId, Math.max(0, evidenceOffset - 65536))">上一段</button><button v-if="evidence.content && evidence.content.length >= 65536" class="quiet" @click="readEvidence(evidenceId, evidenceOffset + 65536)">下一段</button></div>
      <h3>事件时间线</h3><ol class="event-list"><li v-for="event in events" :key="event.cursor"><div><span class="muted">#{{ event.cursor }} · {{ time(event.created_at) }}</span> <strong>{{ event.type }}</strong></div><pre>{{ JSON.stringify(event.payload, null, 2) }}</pre></li></ol>
    </div>
  </section>
</template>
