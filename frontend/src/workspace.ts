import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

export interface Project { id: string; name: string; description: string }
export interface Budget { max_tool_calls: number; max_tokens: number; max_wall_seconds: number; max_output_bytes: number; max_concurrency: number }
export interface Snapshot { targets: string[]; ports: number[]; transport: string; port_profile: string; starts_at: string; expires_at: string; authorization: string; budget: Budget; mode: string; execution_profile: string; config_version: string }
export interface Scope { id: string; version: number; snapshot: Snapshot }
export interface Run { id: string; project_id: string; status: string; phase: string | null; version: number; scope_snapshot: Snapshot; created_at: string; demonstration_scenario: string; execution_ready: boolean }
export interface Preview { targets: string[]; valid: boolean; target_limit: number; over_limit: number; rows: { line: number; value: string; normalized: string | null; reason_code: string | null; duplicate_of: number | null }[] }
export interface ReadinessGate { gate: string; ready: boolean; reason_code: string | null }
// The execution side's own answer. The console renders the mode and the missing conditions from
// it instead of restating them, and a response that could not be obtained carries no observation.
export interface Capabilities {
  protocol_version: string; mode: string; execution_profile: string;
  fake_execution_ready: boolean; real_execution_ready: boolean;
  reason_code: string | null; observed_at: string | null; gates: ReadinessGate[];
  sandbox_management: 'disabled' | 'ready' | 'unavailable';
}

type ResourceFact = { kind: string; role: string | null; name: string; running: boolean | null };
// One instance's live state, as the execution side records it. `stop_state` is the field the
// console must never soften: `unconfirmed` says nobody proved the process ended, so nothing here
// may be rendered as reclaimed, and that instance keeps holding its capacity (issue #19 clause 1).
export interface ToolRuntime {
  instance_id: string; session_id: string; run_id: string; state: string;
  stop_state: 'never_started' | 'running' | 'confirmed' | 'unconfirmed';
  environment: { profile_id?: string; profile_version?: number; image_digests?: Record<string, string>; tool_inventory?: string[]; engine?: string; architecture?: string; created_at?: string };
  egress: { authorized?: { address: string; port: number }[]; applied_at?: string | null; revoked_at?: string | null; revocation_reason?: string | null; observation?: string | null; changes?: { reason: string; at: string }[] };
  lease_expires_at: string | null; halt_reason: string | null; resources: ResourceFact[];
}
export interface SessionRuntime { session_id: string; agent_session_id: string; run_id: string; scope_version: number; policy_version: number; created_at: string; instances: ToolRuntime[] }
export interface ReclaimItem { instance_id: string; state: string; stop_state: string; resources: ResourceFact[]; confirmation_required: boolean }
export interface RunRuntime {
  available: boolean; reason_code: string | null;
  sandbox_management: 'disabled' | 'ready' | 'unavailable';
  profile_id: string | null; observed_at: string; run_id: string; sessions: SessionRuntime[];
  unaccounted: ResourceFact[]; missing: ResourceFact[]; interrupted: string[];
  reclaimable: ReclaimItem[];
}
// What the execution side decided about where a call acts, and how it was going. `null` means no
// such statement exists for that call — the console says so instead of filling the gap itself.
export interface CallRuntime {
  action_id: string; execution_profile: string; target_ip: string; target_port: number;
  parameters_hash: string; argv: string[]; cwd: string | null; user: string | null;
  instance_id: string | null; session_id: string | null;
  image_digests: Record<string, string>; tool_inventory: string[];
  engine: string | null; architecture: string | null; profile_version: number | null;
  network_mode: 'none' | 'internal' | 'bridge'; gateway_ids: string[]; authorized: string[];
  observed_at: string;
}
export interface CallProgress {
  status: 'accepted' | 'running' | 'ended'; started_at: string | null; last_output_at: string | null;
  output_bytes: number; timeout_seconds: number; elapsed_ms: number | null; no_output_yet?: boolean;
}
// One archived artifact, with the honesty flags a reader needs beside the bytes.
export interface EvidenceMeta {
  id: string; relative_path: string; sha256: string; size_bytes: number;
  available: boolean; truncated: boolean; redacted: boolean; missing_reason: string | null;
}

// Every reason code the backend can actually emit, with one readable sentence each. The map is the
// console's whole vocabulary for refusals and gaps: a code with no entry here would leave an
// operator reading a bare identifier, so unknown codes fall back to a generic sentence and the
// identifier is shown beside it rather than instead of it (`reasonText`). `backend/tests/test_reason_codes.py`
// reads this map and fails when the backend grows a code nobody has written a sentence for.
export const messages: Record<string, string> = {
  // -- input, authorization and the Run's own lifecycle -----------------------------------------
  invalid_targets: '请修正或移除所有错误行后再提交。', ipv6_environment_unsupported: '当前部署未启用 IPv6 隔离，不能运行。',
  protected_address: '环回、链路本地、未指定或组播地址不支持。', invalid_ip: '仅接受纯 IP，不接受 URL、端口、域名、CIDR 或 zone ID。',
  target_limit_exceeded: '目标数量超过单 Run 上限，请按提示的行号移除多余目标。',
  invalid_ports: '端口必须在 1–65535 范围内，例如 80,443,8000-8010。', unexpected_custom_ports: '当前端口 profile 不接受自定义端口。',
  invalid_authorization_window: '授权结束时间必须晚于开始时间。', authorization_required: '请填写授权说明或内部授权记录引用。',
  authorization_expired: '授权已过期，请重新配置。', authorization_not_started: '尚未进入授权时间窗。',
  project_name_required: '项目名称不能为空。', project_not_found: '项目不存在。', scope_not_found: '授权范围不存在。',
  run_not_found: '该 Run 不存在。', task_not_found: '研究任务不存在。', evidence_not_found: '证据不存在。',
  invalid_idempotency_key: '创建请求的幂等键无效。', idempotency_conflict: '此请求键对应的内容已改变。',
  invalid_event_cursor: '事件游标无效，请重新读取当前状态。',
  run_paused: '该 Run 曾被暂停。', run_cancelled: '该 Run 曾被取消。',
  version_conflict: '状态已变更，请刷新后重试。', invalid_run_state: '当前状态不支持此操作，请刷新状态。',
  invalid_request: '输入不符合约束，请检查时间、预算和必填项。', invalid_control: '不支持该控制动作。',
  scope_denied: '该目标或端口不在本次授权快照内，计划已被拒绝。',  endpoint_not_expressible: '该地址无法表达为 IPv4/TCP，当前 profile 不支持。',
  budget_exhausted: '预算已耗尽，执行已阻断。', evidence_incomplete: '本次 Run 存在归档不完整的证据，受限结束。',
  // -- access and transport ------------------------------------------------------------------
  rate_limited: '请求过于频繁，请稍后重试。', access_key_missing: '服务端未配置全局访问密钥，业务服务不开放。',
  invalid_access_key: '全局访问密钥不正确。', authentication_required: '需要重新登录。',
  request_too_large: '请求体超过允许大小。', origin_invalid: '访问地址与服务端入口配置不一致。',
  csrf_invalid: '会话校验失败，请重新登录。', storage_unavailable: '数据库暂不可用，请稍后重试。',
  frontend_unavailable: '控制台构建产物缺失，无法提供业务页面。',
  // -- calls, reconciliation and execution facts -----------------------------------------------
  execution_unknown: '调用结果未知，需要先核对执行账本。', execution_reconciliation_required: '调用结果未知，请先依据证据核对后再继续。',
  execution_stop_unconfirmed: '执行端尚未确认该调用的进程与连接已停止，恢复与关闭暂不可用；占用的执行额度也尚未释放。',
  result_incomplete: '本次 Run 以受限方式结束：某个调用的结果始终未确认。',
  reconciliation_conflict: '该调用已有不同的裁定记录，不能就地改写。',
  reconciliation_evidence_missing: '缺少可依据的执行记录或证据，无法据此裁定。',
  reconciliation_evidence_contradicted: '执行端记录与该裁定矛盾，请核对后再决定。',
  reconciliation_lease_active: '原调用的控制租约仍然有效，请在租约到期后重试。',
  invalid_call_state: '该调用当前状态不支持此裁定。', call_not_found: '该调用不属于此 Run 或不存在。',
  action_not_in_profile: '该动作不属于本次 Run 的执行 profile。', action_not_real: '真实执行端不接受假动作；演示请另建显式假 Run。',
  execution_record_mismatch: '执行端记录与票据不符，已拒绝接收。',
  cancelled_before_dispatch: '派发前操作员已取消，该调用从未执行。',
  operator_cancelled: '操作员取消了该调用。', operator_stopped: '操作员请求停止该调用。',
  execution_timeout: '动作在自身时限内未完成，已按超时收尾。', action_failed: '动作以非零状态结束。',
  dependency_failed: '依赖或工具执行失败。', execution_unreachable: '执行端暂时不可达，等待重新对账。',
  lease_stale: '任务租约已过期。', invalid_evidence_range: '证据读取范围无效。',
  event_cursor_ahead: '事件游标超出当前已发布进度。',
  // -- the execution ledger's refusals ---------------------------------------------------------
  runner_state_already_owned: '执行端账本已被其他进程占用。', runner_state_unavailable: '执行端账本不可用，固定假动作无法推进。',
  call_id_conflict: '同一调用 ID 配了不同的参数，已拒绝。', parameters_hash_mismatch: '票据的规范化参数 hash 与实际参数不一致。',
  execution_ticket_expired: '执行票据已过期，未启动。', control_lease_too_long: '控制租约超过执行端允许的最长时限。',
  control_lease_expired: '控制租约到期，执行端已自行撤销并停止该实例。', invalid_control_lease: '控制租约无效。',
  stale_lease_generation: '票据的租约代次已过期。', scope_policy_mismatch: '票据的授权范围或策略版本与本 Run 不一致。',
  evidence_hash_mismatch: '证据文件哈希与索引不一致，不能据此确认。', evidence_missing: '原始证据文件缺失。',
  // -- evidence archiving ----------------------------------------------------------------------
  evidence_storage_full: '证据存储空间不足，已阻断受影响的执行；本次调用不再确认。',
  evidence_archive_unwritable: '证据归档目录不可写，已阻断受影响的执行。',
  evidence_artifact_too_large: '单件证据超过本次 Run 允许的上限，已阻断该执行。',
  evidence_storage_failed: '证据归档失败，该调用的结果不可用于确认。',
  evidence_too_large: '证据文件超过允许大小。', evidence_name_rejected: '证据文件名不被接受。',
  evidence_output_truncated: '输出超过本次 Run 的保留上限，已截断并标注，未保留的部分不视为已观察。',
  evidence_path_escapes_root: '证据路径越出归档根目录，已拒绝写入。',
  archive_unavailable: '证据归档不可用，无法读取该文件。', archive_hash_mismatch: '归档文件与索引哈希不一致，不能据此确认。',
  archive_missing: '归档文件缺失。',
  // -- readiness, capability and deployment ----------------------------------------------------
  environment_unsupported: '当前宿主环境未通过真实执行的隔离与 profile 验收。',
  runner_unavailable: '执行端不可达，无法确认当前执行力。',
  profile_unvalidated: '真实执行 profile 尚未按票据、租约、取消、回收与证据归档复验。',
  revert_path_missing: '缺少先收尾再撤除管理能力的回退入口。',
  contract_not_expressible: '能力契约仍把未就绪写死，无法表达当前状态。',
  console_not_consuming: '当前控制台未消费能力接口。',
  real_execution_not_ready: '真实执行的就绪门槛未逐项满足，本次 Run 不会创建真实调用。',
  real_execution_disabled: '本部署未启用真实执行的管理能力。',
  execution_profile_unknown: '票据引用了未注册的执行 profile。', execution_profile_mismatch: '票据的 profile 与本次 Run 固定的执行 profile 不一致。',
  execution_not_implemented: '执行端未实现该接口。', runner_authentication_required: '执行端拒绝了控制面的身份凭据。',
  // -- the trusted sandbox manager -------------------------------------------------------------
  sandbox_management_disabled: '本部署未启用容器管理能力，无法读取容器与网关状态。',
  sandbox_state_unsupported: '执行端未提供容器与网关状态读取接口。',
  sandbox_runtime_unreachable: '受信管理组件无法访问容器运行时。',
  sandbox_state_unwritable: '受信管理组件的状态目录不可写。',
  sandbox_profile_invalid: '固定 sandbox profile 内容不合法。', sandbox_profile_unknown: '未找到该 sandbox profile。',
  sandbox_profile_unsupported_version: 'sandbox profile 版本不受支持。', sandbox_profile_not_isolated: 'sandbox profile 未满足必要隔离，已拒绝加载。',
  sandbox_profile_image_not_pinned: 'sandbox profile 的镜像未固定 digest，已拒绝加载。',
  sandbox_profile_privileged_tool: 'sandbox profile 让工具容器获得额外权限，已拒绝加载。',
  sandbox_profile_not_applied: '实例未按 profile 装配，已留在中断态并阻塞同一会话的新实例。',
  sandbox_instance_not_running: '该实例当前不在可执行状态。', sandbox_instance_creation_failed: '沙箱实例创建失败。',
  sandbox_instance_mismatch: '请求指向的实例与该会话的实例不符。', sandbox_instance_interrupted: '该实例的创建曾被中断，需先核清残留。',
  sandbox_instance_active: '该实例仍然活动，不能按已结束处理。', sandbox_instance_reclaimed: '该实例已被回收。',
  sandbox_instance_unknown: '受信管理组件没有该实例的记录。', sandbox_session_unknown: '受信管理组件没有该会话的记录。',
  sandbox_authorization_mismatch: '该会话绑定的是另一份授权或策略版本，不能沿用。',
  sandbox_resource_missing: '受信管理组件已不再持有该资源。', sandbox_resources_unaccounted: '仍有账本未记账的资源，已阻断。',
  sandbox_image_unavailable: 'profile 声明的镜像不可取得。', ownership_mismatch: '该资源不属于本项目，已拒绝处置。',
  sandbox_reverting: '管理能力正在回退，不再创建新实例。', sandbox_revert_blocked: '仍有资源或停止未核清，回退被阻断。',
  sandbox_egress_unverified: '网关放行规则与授权集合不一致，已结束执行。', sandbox_gateway_not_ready: '网关未在时限内报告就绪，未创建工具容器。',
  sandbox_gateway_policy_failed: '向网关下发授权规则失败。', sandbox_platform_networks_unknown: '无法识别平台自身网络，拒绝开放出口。',
  sandbox_stop_unconfirmed: '受信管理组件无法确认该实例已停止，未释放其占用的资源。',
  revocation_failed: '出口撤销失败，该实例的放行状态不可信。', action_command_invalid: '动作命令为空或含非法参数。',
  // -- reasons recorded as facts rather than as reason_code ------------------------------------
  // These are the values the execution side writes into `halt_reason`, an egress change's `reason`
  // and an instance's `revocation_reason`. They reach the console through different fields than
  // `reason_code`, and they are mapped here so the isolation panel reads in the same language.
  scope_revoked: '授权范围已被撤销。', scope_shrunk: '授权范围已收窄。', initial: '首次放行。',
  egress_unverified: '出口规则核验未通过。', revert: '部署回退。', stopped: '已停止。',
  // -- platform diagnostics (never rendered in the console, mapped so nothing is unmapped) -----
  readiness_failed: '就绪检查未通过。', required_process_exited: '必需的子进程已退出。',
  startup_failed: '服务启动失败。', scheduler_unavailable: '调度进程不可用。', migration_failed: '数据库迁移失败。',
};

export const gateLabels: Record<string, string> = {
  profile_revalidation: 'profile 复验（票据、租约、取消、回收与证据归档）',
  contract_expressiveness: '能力契约可表达未就绪状态',
  console_consumption: '控制台消费能力接口',
  deployment_revert: '默认部署保持假执行且可一键回退',
};

export const managementLabels: Record<string, string> = {
  disabled: '未启用（默认部署：Runner 不接触容器管理接口）',
  ready: '已启用：受信管理组件可用',
  unavailable: '已启用但当前不可用',
};

// A code nobody has written a sentence for must not reach an operator as a bare identifier. The
// sentence says what happened in general terms and the identifier is kept beside it, so the reader
// can still quote it — that is the whole point of a closed reason vocabulary (PROJECT.md section
// 12.2). `backend/tests/test_reason_codes.py` keeps this map in step with what the backend emits.
export const unknownReasonText = '平台未为该原因码提供说明，请按标识查询执行端记录。';
export const reasonText = (reason: string | null | undefined) => {
  if (!reason) return '';
  return messages[reason] || unknownReasonText;
};
// The identifier is worth showing next to the sentence when a reader may need to quote it to an
// execution log; it is never worth showing *instead* of the sentence.
export const reasonWithCode = (reason: string | null | undefined) => {
  if (!reason) return '';
  return `${reasonText(reason)}（${reason}）`;
};

const runStatuses: Record<string, string> = { draft: '草稿', queued: '已排队', running: '自动执行中', waiting: '等待条件', recovering: '正在恢复', pausing: '正在暂停', paused: '已暂停', cancelling: '正在取消', cancelled: '已取消', closed: '演示已结束', failed: '失败' };
export const statusText = (run: Pick<Run, 'status' | 'phase'>) => run.status === 'waiting' && run.phase === 'awaiting_human' ? '自动阶段结束 · 待人工复审' : runStatuses[run.status] || run.status;

export class ApiFailure extends Error {
  // The contract reason_code decides whether a deliberate request can be re-sent.
  constructor(message: string, readonly reasonCode: string | undefined) { super(message); }
}

export const useWorkspace = defineStore('workspace', () => {
  // Session CSRF token lives in memory; the HttpOnly authentication cookie is server-managed.
  const csrf = ref('');
  const projects = ref<Project[]>([]);
  const runs = ref<Run[]>([]);
  // Null until the execution side has been asked: a missing answer is not readiness.
  const capabilities = ref<Capabilities | null>(null);
  // One vocabulary for every view: the mode, the two readiness answers and the blocking reason
  // all come from the same capability response instead of being restated per component.
  const realExecutionReady = computed(() => capabilities.value?.real_execution_ready === true);
  const fakeExecutionReady = computed(() => capabilities.value?.fake_execution_ready === true);
  const readinessReason = computed(() => reasonText(capabilities.value?.reason_code));
  const executionMode = computed(() => realExecutionReady.value ? '真实执行' : '开发演示 / 假执行');
  async function api<T>(path: string, body?: unknown, extra: Record<string, string> = {}): Promise<T> {
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf.value, ...extra }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    if (response.status === 401) { window.location.replace('/login'); throw new Error('会话已失效。'); }
    if (!response.ok) {
      const data = await response.json() as { reason_code?: string };
      // A refusal always carries its reason when the contract has one; the sentence is the
      // operator's, and the identifier is kept for quoting rather than shown alone.
      throw new ApiFailure(data.reason_code ? reasonWithCode(data.reason_code) : `请求失败（${response.status}）。`, data.reason_code);
    }
    return response.status === 204 ? undefined as T : response.json();
  }
  async function loadCapabilities() { capabilities.value = await api<Capabilities>('/api/v1/system/capabilities'); }
  async function load() {
    const session = await api<{ csrf_token: string }>('/auth/session');
    csrf.value = session.csrf_token;
    [projects.value, runs.value] = await Promise.all([api<Project[]>('/api/v1/projects'), api<Run[]>('/api/v1/runs')]);
    await loadCapabilities();
  }
  async function logout() { await api('/auth/logout', {}); csrf.value = ''; capabilities.value = null; window.location.replace('/login'); }
  return { csrf, projects, runs, capabilities, realExecutionReady, fakeExecutionReady, readinessReason, executionMode, api, load, loadCapabilities, logout };
});
