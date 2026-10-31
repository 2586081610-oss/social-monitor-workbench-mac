import { postJson } from './http';
import type { LinkAnalysisDownloadResult } from '../types/api.d';

export function downloadSingleLink(url: string): Promise<LinkAnalysisDownloadResult> {
  return postJson<LinkAnalysisDownloadResult>('/api/v1/link-analyses/download', { url });
}
