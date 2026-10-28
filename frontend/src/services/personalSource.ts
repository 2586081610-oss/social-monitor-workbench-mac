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
  /** 监控层是否在线（心跳）；定时只在监控层运行时才会执行 */
  monitor_online: boolean;
  /** 定时是否真的会执行 = enabled 且监控层在线 */
  schedule_active: boolean;
  sec_user_id_set: boolean;
  cookie_file_set: boolean;
  runtime_available: boolean;
  /** 全局流水线（环境变量 MONITOR_RUN_PIPELINE），与个人来源自动下载相互独立 */
  pipeline_enabled: boolean;
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
