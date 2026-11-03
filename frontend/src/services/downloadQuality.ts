/** 下载清晰度 API 封装：读取 / 保存（B 站 yutto / 抖音 jiji262）。契约对齐 /api/v1/settings/download-quality。 */
import { getJson, putJson } from './http';

export interface QualityOption {
  value: string;
  label: string;
}

export interface DownloadQuality {
  bilibili: string;
  douyin: string;
  bilibili_explicit: boolean;
  douyin_explicit: boolean;
  bilibili_options: QualityOption[];
  douyin_options: QualityOption[];
  /** B 站登录态存在性（platform_cookies.json 的 bilibili cookie 含 SESSDATA）；只报存在性，不回显内容 */
  bilibili_login_available: boolean;
}

/** 读取当前清晰度与可选档位。 */
export function getDownloadQuality(): Promise<DownloadQuality> {
  return getJson<DownloadQuality>('/api/v1/settings/download-quality');
}

/** 保存清晰度；仅对之后的下载生效，已下载文件不会自动重下。 */
export function putDownloadQuality(payload: { bilibili?: string; douyin?: string }): Promise<DownloadQuality> {
  return putJson<DownloadQuality>('/api/v1/settings/download-quality', payload);
}
