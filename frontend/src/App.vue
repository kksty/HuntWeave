<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { gateLabels, managementLabels, reasonText, statusText, useWorkspace, type Preview, type Project, type Run, type Scope } from './workspace';
import RunConsole from './RunConsole.vue';

const workspace = useWorkspace();
const route = useRoute();
const router = useRouter();
const pending = ref(0), busy = computed(() => pending.value > 0), error = ref(''), projectId = ref(''), name = ref(''), description = ref('');
// Execution readiness is rendered from the execution side's own answer, so the page never
// claims a mode the platform is not in.
const capabilities = computed(() => workspace.capabilities);
let capabilityTimer: ReturnType<typeof setInterval> | null = null;
const targets = ref(''), preview = ref<Preview | null>(null);
const portProfile = ref('common-tcp-v1'), customPorts = ref(''), ports = ref<number[] | null>(null);
const shanghaiInput = (date: Date) => new Date(date.getTime() + 8 * 3600000).toISOString().slice(0, 16);
const starts = ref(shanghaiInput(new Date(Date.now() - 60000))), expires = ref(shanghaiInput(new Date(Date.now() + 3600000)));
const authorization = ref(''), calls = ref(50), tokens = ref(100000), seconds = ref(3600), outputMiB = ref(32), concurrency = ref(4);
const scope = ref<Scope | null>(null), selected = ref<Run | null>(null);
const scenario = ref('positive');
let idempotencyKey = crypto.randomUUID();
const valid = computed(() => !!projectId.value && !!preview.value?.valid && !!ports.value?.length && !!authorization.value.trim());
const formatTime = (value: string) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
async function perform(action: () => Promise<void>) {
  pending.value++; error.value = '';
  try { await action(); } catch (e) { error.value = e instanceof Error ? e.message : '暂时无法完成操作。'; }
  finally { pending.value--; }
}
watch(targets, () => { preview.value = null; });
watch([portProfile, customPorts], () => { ports.value = null; });
watch([projectId, targets, portProfile, customPorts, starts, expires, authorization, calls, tokens, seconds, outputMiB, concurrency], () => { scope.value = null; idempotencyKey = crypto.randomUUID(); });
async function refreshSelected() {
  const id = route.params.id;
  selected.value = typeof id === 'string' ? await workspace.api<Run>(`/api/v1/runs/${encodeURIComponent(id)}`) : null;
}
watch(() => route.params.id, () => perform(refreshSelected));
onMounted(() => perform(async () => {
  await workspace.load(); projectId.value = workspace.projects[0]?.id || ''; await refreshSelected();
  // The mode can change under the operator (execution side lost or restored), so it is re-read.
  capabilityTimer = setInterval(() => { void workspace.loadCapabilities().catch(() => undefined); }, 10000);
}));
onUnmounted(() => { if (capabilityTimer) clearInterval(capabilityTimer); capabilityTimer = null; });
function createProject() { return perform(async () => {
  const project = await workspace.api<Project>('/api/v1/projects', { name: name.value, description: description.value });
  workspace.projects.unshift(project); projectId.value = project.id; name.value = ''; description.value = '';
}); }
function previewTargets() { return perform(async () => { preview.value = await workspace.api<Preview>('/api/v1/targets/preview', { text: targets.value }); }); }
function previewPorts() { return perform(async () => { const result = await workspace.api<{ ports: number[] }>('/api/v1/ports/preview', { profile: portProfile.value, custom: portProfile.value === 'custom-tcp-v1' ? customPorts.value : '' }); ports.value = result.ports; }); }
function saveScope() { return perform(async () => {
  scope.value = await workspace.api<Scope>('/api/v1/scopes', { project_id: projectId.value, targets_text: targets.value, ports: { profile: portProfile.value, custom: portProfile.value === 'custom-tcp-v1' ? customPorts.value : '' }, starts_at: new Date(`${starts.value}+08:00`).toISOString(), expires_at: new Date(`${expires.value}+08:00`).toISOString(), authorization: authorization.value, budget: { max_tool_calls: calls.value, max_tokens: tokens.value, max_wall_seconds: seconds.value, max_output_bytes: outputMiB.value * 1048576, max_concurrency: concurrency.value } });
  idempotencyKey = crypto.randomUUID();
}); }
function createRun() { return perform(async () => {
  if (!scope.value) return;
  const run = await workspace.api<Run>('/api/v1/runs', { scope_id: scope.value.id, scope_version: scope.value.version, demonstration_scenario: scenario.value }, { 'Idempotency-Key': idempotencyKey });
  if (!workspace.runs.some(item => item.id === run.id)) workspace.runs.unshift(run);
  await router.push(`/runs/${run.id}`); selected.value = run;
}); }
watch(scenario, () => { idempotencyKey = crypto.randomUUID(); });
function updateRun(run: Run) { selected.value = run; workspace.runs = workspace.runs.map(item => item.id === run.id ? run : item); }
function startRun() { return perform(async () => {
  if (!selected.value) return;
  const run = await workspace.api<Run>(`/api/v1/runs/${selected.value.id}/start`, { version: selected.value.version });
  selected.value = run; workspace.runs = workspace.runs.map(item => item.id === run.id ? run : item);
}); }
</script>

<template>
  <header><RouterLink to="/" class="brand">HUNTWEAVE</RouterLink><span class="badge">{{ workspace.executionMode }}</span><button class="quiet" :disabled="busy" @click="perform(workspace.logout)">退出登录</button></header>
  <main>
    <div class="intro"><p class="eyebrow">研究工作台 · P0</p><h1>先确定范围，再开始研究。</h1><p>保存授权范围与预算，创建可恢复的演示记录。当前版本不连接目标，不执行扫描。</p></div>
    <p class="notice" role="status" data-testid="execution-readiness">
      当前执行模式：{{ workspace.executionMode }}。容器管理能力：{{ managementLabels[capabilities?.sandbox_management || 'disabled'] || '未知' }}。
      <template v-if="!capabilities">尚未取得执行端的能力结论，不能据此认为平台就绪。</template>
      <template v-else-if="!workspace.realExecutionReady">真实执行未开放：{{ workspace.readinessReason || '未取得执行端的能力结论。' }}</template>
      <template v-if="capabilities && !workspace.fakeExecutionReady">执行端不可用：固定假动作链路当前无法推进。</template>
      <template v-if="capabilities?.observed_at">能力观测于 {{ formatTime(capabilities.observed_at) }}。</template>
    </p>
    <details v-if="capabilities && !workspace.realExecutionReady && capabilities.gates.length" class="notice" data-testid="execution-gates"><summary>真实执行就绪门槛（逐项）</summary><ul><li v-for="gate in capabilities.gates" :key="gate.gate">{{ gateLabels[gate.gate] || gate.gate }}：{{ gate.ready ? '已满足' : reasonText(gate.reason_code) }}</li></ul></details>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="columns">
      <section class="panel configuration"><h2>01 / 项目与授权</h2>
        <fieldset :disabled="busy">
          <label>所属项目<select v-model="projectId"><option value="" disabled>先创建一个项目</option><option v-for="project in workspace.projects" :key="project.id" :value="project.id">{{ project.name }}</option></select></label>
          <details :open="!workspace.projects.length"><summary>新建项目</summary><form @submit.prevent="createProject"><label>项目名称<input v-model="name" required maxlength="120"></label><label>项目说明<textarea v-model="description" maxlength="2000" rows="2"></textarea></label><button :disabled="!name.trim()">创建项目</button></form></details>
          <label>目标 IP <small>每行一个；重复项会合并，错误行必须修正或移除</small><textarea v-model="targets" rows="5" placeholder="192.0.2.10&#10;198.51.100.20" maxlength="200000"></textarea></label>
          <button class="secondary" :disabled="!targets.trim()" @click="previewTargets">预览 IP</button>
          <div v-if="preview" class="preview">
            <p>
              {{ preview.targets.length }} 个去重目标 · 单 Run 上限 {{ preview.target_limit }}
              <template v-if="preview.over_limit"> · 超限 {{ preview.over_limit }} 行</template>
              · {{ preview.valid ? '可以提交' : '存在错误，不能提交' }}
            </p>
            <p v-if="preview.over_limit" class="error" role="alert">超过上限的行已在下表按行号标出；请移除这些行后再提交。超限时整份清单不会被接受。</p>
            <div class="table-scroll"><table><thead><tr><th>行</th><th>IP / 结果</th></tr></thead><tbody><tr v-for="row in preview.rows" :key="row.line"><td>{{ row.line }}</td><td><code>{{ row.normalized || row.value }}</code><small :class="{ invalid: row.reason_code }">{{ row.reason_code ? reasonText(row.reason_code) : row.duplicate_of ? `与第 ${row.duplicate_of} 行重复，已合并` : '有效' }}</small></td></tr></tbody></table></div>
          </div>
          <label>TCP 端口范围<select v-model="portProfile"><option value="common-tcp-v1">常用 TCP · common-tcp-v1</option><option value="custom-tcp-v1">自定义 TCP</option><option value="all-tcp-v1">全部 TCP · 1–65535</option></select></label>
          <label v-if="portProfile === 'custom-tcp-v1'">端口与范围<input v-model="customPorts" placeholder="80,443,8000-8010"></label>
          <button class="secondary" @click="previewPorts">展开端口</button>
          <details v-if="ports" open class="preview"><summary>{{ ports.length }} 个明确授权端口</summary><p class="port-list">{{ ports.join(', ') }}</p></details>
          <div class="pair"><label>授权开始（上海时间）<input v-model="starts" type="datetime-local" required></label><label>授权结束（上海时间）<input v-model="expires" type="datetime-local" required></label></div>
          <label>授权说明<textarea v-model="authorization" rows="2" maxlength="2000" placeholder="说明此范围的授权依据与用途" required></textarea></label>
          <details><summary>预算上限</summary><div class="pair"><label>工具调用次数<input v-model.number="calls" type="number" min="1" max="1000"></label><label>模型 token<input v-model.number="tokens" type="number" min="1" max="1000000"></label><label>运行时长（秒）<input v-model.number="seconds" type="number" min="1" max="86400"></label><label>输出大小（MiB）<input v-model.number="outputMiB" type="number" min="1" max="1024"></label><label>并发上限<input v-model.number="concurrency" type="number" min="1" max="32"></label></div></details>
          <button :disabled="!valid" @click="saveScope">保存授权快照</button>
          <div v-if="scope" class="preview"><p>授权快照已保存 · {{ scope.snapshot.targets.length }} 个 IP / {{ scope.snapshot.ports.length }} 个端口</p><p>修改配置将需要重新保存。创建请求重试会复用同一 Run。</p><label>假输出场景<select v-model="scenario"><option value="positive">正例 · 需要独立补证</option><option value="negative">反例 · 不支持假设</option><option value="failure">失败 · 工具错误</option></select></label><button @click="createRun">创建假 Run</button></div>
        </fieldset>
      </section>
      <div class="right-column">
        <section class="panel"><h2>02 / 演示记录</h2><p v-if="!workspace.runs.length" class="muted">尚无 Run。保存左侧授权快照后创建。</p><ul class="run-list"><li v-for="run in workspace.runs" :key="run.id"><RouterLink :to="`/runs/${run.id}`"><strong>{{ workspace.projects.find(p => p.id === run.project_id)?.name || '项目' }}</strong><span>{{ statusText(run) }}</span><small>{{ formatTime(run.created_at) }} · {{ run.scope_snapshot.targets.length }} 个 IP</small></RouterLink></li></ul></section>
        <section v-if="selected" class="panel" aria-label="Run 详情"><div class="section-heading"><h2>授权快照</h2><span class="badge">{{ statusText(selected) }}</span></div><p class="muted">{{ selected.id }} · 状态版本 {{ selected.version }}</p><dl><dt>目标</dt><dd>{{ selected.scope_snapshot.targets.join(', ') }}</dd><dt>端口（TCP）</dt><dd class="port-list">{{ selected.scope_snapshot.ports.join(', ') }}</dd><dt>有效期（上海时间）</dt><dd>{{ formatTime(selected.scope_snapshot.starts_at) }} → {{ formatTime(selected.scope_snapshot.expires_at) }}</dd><dt>授权说明</dt><dd>{{ selected.scope_snapshot.authorization }}</dd><dt>执行配置</dt><dd>开发演示 / 假执行 · {{ selected.scope_snapshot.execution_profile }} · {{ selected.scope_snapshot.config_version }}</dd><dt>预算</dt><dd>{{ selected.scope_snapshot.budget.max_tool_calls }} 次工具调用 / {{ selected.scope_snapshot.budget.max_tokens }} token / {{ selected.scope_snapshot.budget.max_wall_seconds }} 秒 / {{ selected.scope_snapshot.budget.max_output_bytes }} 字节 / 并发 {{ selected.scope_snapshot.budget.max_concurrency }}</dd></dl><button v-if="selected.status === 'draft'" :disabled="busy" @click="startRun">将假 Run 加入队列</button><p class="notice">固定假动作仅验证决策、证据与恢复契约；自动阶段结束后等待人工结束演示。</p><button class="quiet" :disabled="busy" @click="perform(refreshSelected)">刷新状态</button></section>
        <RunConsole v-if="selected" :key="selected.id" :run="selected" @updated="updateRun" />
      </div>
    </div>
  </main>
</template>
