/** 个人来源（F2 通道）API 封装：状态 / 定时配置 / 手动同步。契约对齐 /api/v1/personal-source。 */
import { getJson, postJson, putJson } from './http';

export type PersonalScope = 'like' | 'collection' | 'collections';

export interface PersonalSourceStatus {
  channel: string;
  schedule: {
    enabled: boolean;
    interval_minutes: number;
    scopes: PersonalScope[];
    sec_user_id: string;
    auto_download: boolean;
  };
  /** 监控层是否在线（心跳）；含 RSSHub/博主轮询等全部监控任务 */
  monitor_online: boolean;
  /** 个人任务进程（python -m monitor.personal_worker）是否在线；只运行已授权个人任务 */
  personal_worker_online: boolean;
  /** 定时是否真的会执行 = enabled 且（监控层在线 或 个人任务进程在线） */
  schedule_active: boolean;
  sec_user_id_set: boolean;
  cookie_file_set: boolean;
  runtime_available: boolean;
  /** 全局流水线（环境变量 MONITOR_RUN_PIPELINE），与个人来源自动下载相互独立 */
  pipeline_enabled: boolean;
  /** 已授权下载的待处理积压数 */
  pending_download_count: number;
  last_sync: {
    finished_at: string | null;
    status: string;
    detail: string | null;
    new_works: number;
  } | null;
}

export interface PersonalSourceSchedule {
  enabled: boolean;
  interval_minutes: number;
  scopes: PersonalScope[];
  sec_user_id: string;
  auto_download: boolean;
}

export interface PersonalSyncTriggered {
  triggered: boolean;
  pid: number;
  triggered_at: string;
}

/** 来源状态（含最近一次同步结果与失败原因）。 */
export function getPersonalSourceStatus(): Promise<PersonalSourceStatus> {
  return getJson<PersonalSourceStatus>('/api/v1/personal-source/status');
}

/** 读取定时配置。 */
export function getPersonalSourceSchedule(): Promise<PersonalSourceSchedule> {
  return getJson<PersonalSourceSchedule>('/api/v1/personal-source/schedule');
}

/** 保存定时配置（默认关闭；启用需用户在此明确操作）。auto_download 为个人来源下载的独立授权。 */
export function putPersonalSourceSchedule(payload: {
  enabled: boolean;
  interval_minutes: number;
  scopes: PersonalScope[];
  sec_user_id?: string | null;
  auto_download?: boolean | null;
}): Promise<PersonalSourceSchedule> {
  return putJson<PersonalSourceSchedule>('/api/v1/personal-source/schedule', payload);
}

/** 手动同步一次（后端拉起一次性子进程，结果经状态接口查询）。 */
export function triggerPersonalSourceSync(): Promise<PersonalSyncTriggered> {
  return postJson<PersonalSyncTriggered>('/api/v1/personal-source/sync');
}

export interface PersonalSyncLoopState {
  running: boolean;
  started_at: string | null;
  max_rounds: number | null;
  stop_requested: boolean;
  rounds_done: number;
  pid: number | null;
  last_round: {
    round_no: number;
    exit: number;
    poll_status: string;
    new_works: number;
    works_total: number;
    at: string;
  } | null;
  finished_at: string | null;
  final_status: string | null;
}

export interface PersonalSyncLoopStatus {
  exists: boolean;
  running: boolean;
  state?: PersonalSyncLoopState;
  works_total: number;
  last_poll: { status: string; new_works: number; finished_at: string | null } | null;
}

/** 启动"连续同步到完整"循环：只读列表元数据，绝不下载（预算限速，轮间休息）。 */
export function startPersonalSyncLoop(maxRounds: number = 60): Promise<{ started: boolean; pid: number; max_rounds: number }> {
  return postJson<{ started: boolean; pid: number; max_rounds: number }>(
    '/api/v1/personal-source/sync-loop',
    { max_rounds: maxRounds },
  );
}

/** 请求停止连续同步（当前轮结束后安全退出）。 */
export function stopPersonalSyncLoop(): Promise<{ stopped: boolean }> {
  return postJson<{ stopped: boolean }>('/api/v1/personal-source/sync-loop/stop', {});
}

/** 连续同步循环状态（轮次、最新一轮、作品总数）。 */
export function getPersonalSyncLoopStatus(): Promise<PersonalSyncLoopStatus> {
  return getJson<PersonalSyncLoopStatus>('/api/v1/personal-source/sync-loop/status');
}

export interface PersonalWorkItem {
  external_id: string;
  title: string | null;
  content_type: string | null;
  published_at: string | null;
  url: string | null;
  cover_url: string | null;
  status: string | null;
  media_path: string | null;
}

export interface PersonalWorksPage {
  total: number;
  limit: number;
  offset: number;
  items: PersonalWorkItem[];
}

/** 已抓取的个人来源作品清单（分页+过滤；随爬取轮次实时增长）。 */
export function getPersonalSourceWorks(params: {
  limit?: number;
  offset?: number;
  content_type?: string | null;
  status?: string | null;
  q?: string | null;
}): Promise<PersonalWorksPage> {
  const usp = new URLSearchParams();
  if (params.limit != null) usp.set('limit', String(params.limit));
  if (params.offset != null) usp.set('offset', String(params.offset));
  if (params.content_type) usp.set('content_type', params.content_type);
  if (params.status) usp.set('status', params.status);
  if (params.q) usp.set('q', params.q);
  const qs = usp.toString();
  return getJson<PersonalWorksPage>(`/api/v1/personal-source/works${qs ? `?${qs}` : ''}`);
}
