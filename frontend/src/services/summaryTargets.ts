/** AI 总结交付目标：查询与默认工具设置（方案 A）。 */
import { getJson, putJson } from './http';

export interface SummaryTargetInfo {
  name: string;
  kind: string | null;
  is_default: boolean;
  root: string | null;
}

export interface SummaryTargetsData {
  default_target: string;
  targets: SummaryTargetInfo[];
}

export function listSummaryTargets(): Promise<SummaryTargetsData> {
  return getJson<SummaryTargetsData>('/api/v1/summary-targets');
}

export function setDefaultSummaryTarget(target: string): Promise<SummaryTargetsData> {
  return putJson<SummaryTargetsData>('/api/v1/summary-targets/default', { target });
}

/** 执行者健康：通/不通按队列真实消费记录判定（后端不编造）。 */
export interface SummaryTargetHealth {
  name: string;
  kind: string | null;
  is_default: boolean;
  pending_count: number;
  done_count: number;
  error_count: number;
  quota_exhausted_count: number;
  last_done_at: string | null;
  last_executor: string | null;
  status: 'active' | 'no-consumer' | 'quota_exhausted' | 'idle' | 'empty';
  cli: { available: boolean | null; note: string };
}

export interface SummaryTargetsHealthData {
  default_target: string;
  targets: SummaryTargetHealth[];
}

export function getSummaryTargetsHealth(): Promise<SummaryTargetsHealthData> {
  return getJson<SummaryTargetsHealthData>('/api/v1/summary-targets/health');
}
