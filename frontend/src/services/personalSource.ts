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
  };
  sec_user_id_set: boolean;
  cookie_file_set: boolean;
  runtime_available: boolean;
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

/** 保存定时配置（默认关闭；启用需用户在此明确操作）。 */
export function putPersonalSourceSchedule(payload: {
  enabled: boolean;
  interval_minutes: number;
  scopes: PersonalScope[];
  sec_user_id?: string | null;
}): Promise<PersonalSourceSchedule> {
  return putJson<PersonalSourceSchedule>('/api/v1/personal-source/schedule', payload);
}

/** 手动同步一次（后端拉起一次性子进程，结果经状态接口查询）。 */
export function triggerPersonalSourceSync(): Promise<PersonalSyncTriggered> {
  return postJson<PersonalSyncTriggered>('/api/v1/personal-source/sync');
}
