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
