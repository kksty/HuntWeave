import { defineStore } from 'pinia';
import { ref } from 'vue';

export interface Project { id: string; name: string; description: string }
export interface Budget { max_tool_calls: number; max_tokens: number; max_wall_seconds: number; max_output_bytes: number; max_concurrency: number }
export interface Snapshot { targets: string[]; ports: number[]; transport: string; port_profile: string; starts_at: string; expires_at: string; authorization: string; budget: Budget; mode: string; execution_profile: string; config_version: string }
export interface Scope { id: string; version: number; snapshot: Snapshot }
export interface Run { id: string; project_id: string; status: string; phase: string | null; version: number; scope_snapshot: Snapshot; created_at: string; demonstration_scenario: string }
export interface Preview { targets: string[]; valid: boolean; rows: { line: number; value: string; normalized: string | null; reason_code: string | null; duplicate_of: number | null }[] }

export const messages: Record<string, string> = {
  invalid_targets: '请修正或移除所有错误行后再提交。', ipv6_environment_unsupported: '当前部署未启用 IPv6 隔离，不能运行。',
  protected_address: '环回、链路本地、未指定或组播地址不支持。', invalid_ip: '仅接受纯 IP，不接受 URL、端口、域名、CIDR 或 zone ID。',
  invalid_ports: '端口必须在 1–65535 范围内，例如 80,443,8000-8010。', invalid_authorization_window: '授权结束时间必须晚于开始时间。',
  authorization_expired: '授权已过期，请重新配置。', authorization_not_started: '尚未进入授权时间窗。',
  version_conflict: '状态已变更，请刷新后重试。', idempotency_conflict: '此请求键对应的内容已改变。', origin_invalid: '访问地址与服务端入口配置不一致。',
  csrf_invalid: '会话校验失败，请重新登录。', storage_unavailable: '数据库暂不可用，请稍后重试。', invalid_request: '输入不符合约束，请检查时间、预算和必填项。',
  invalid_run_state: '当前状态不支持此操作，请刷新状态。', execution_unknown: '调用结果未知，需要先核对执行账本。',
  execution_reconciliation_required: '调用结果未知，请先依据证据核对后再继续。',
  execution_stop_unconfirmed: '执行端尚未确认该调用的进程与连接已停止，恢复与关闭暂不可用。',
  result_incomplete: '本次 Run 以受限方式结束：某个调用的结果始终未确认。',
  reconciliation_conflict: '该调用已有不同的裁定记录，不能就地改写。',
  reconciliation_evidence_missing: '缺少可依据的执行记录或证据，无法据此裁定。',
  reconciliation_evidence_contradicted: '执行端记录与该裁定矛盾，请核对后再决定。',
  reconciliation_lease_active: '原调用的控制租约仍然有效，请在租约到期后重试。',
  invalid_call_state: '该调用当前状态不支持此裁定。', call_not_found: '该调用不属于此 Run 或不存在。',
  budget_exhausted: '预算已耗尽，执行已阻断。', evidence_missing: '原始证据文件缺失。', evidence_corrupt: '证据校验失败。',
};

export class ApiFailure extends Error {
  // The contract reason_code decides whether a deliberate request can be re-sent.
  constructor(message: string, readonly reasonCode: string | undefined) { super(message); }
}

export const useWorkspace = defineStore('workspace', () => {
  // Session CSRF token lives in memory; the HttpOnly authentication cookie is server-managed.
  const csrf = ref('');
  const projects = ref<Project[]>([]);
  const runs = ref<Run[]>([]);
  async function api<T>(path: string, body?: unknown, extra: Record<string, string> = {}): Promise<T> {
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf.value, ...extra }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    if (response.status === 401) { window.location.replace('/login'); throw new Error('会话已失效。'); }
    if (!response.ok) {
      const data = await response.json() as { reason_code?: string };
      throw new ApiFailure(messages[data.reason_code || ''] || `请求失败（${data.reason_code || response.status}）。`, data.reason_code);
    }
    return response.status === 204 ? undefined as T : response.json();
  }
  async function load() {
    const session = await api<{ csrf_token: string }>('/auth/session');
    csrf.value = session.csrf_token;
    [projects.value, runs.value] = await Promise.all([api<Project[]>('/api/v1/projects'), api<Run[]>('/api/v1/runs')]);
  }
  async function logout() { await api('/auth/logout', {}); csrf.value = ''; window.location.replace('/login'); }
  return { csrf, projects, runs, api, load, logout };
});
