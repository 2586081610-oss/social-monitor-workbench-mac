/** 健康度资源 API 封装（对齐 openapi: health）。 */
import { getJson, postJson, putJson } from './http';
import type { CookieHealth, CookieHealthList, SystemHealthList } from '../types/api.d';

export function getCookieHealth(): Promise<CookieHealthList> {
  return getJson<CookieHealthList>('/api/v1/health/cookie');
}

export function checkCookieHealth(platform: string): Promise<CookieHealth> {
  return postJson<CookieHealth>(`/api/v1/health/cookie/${platform}/check`);
}

export function checkAllCookieHealth(): Promise<CookieHealthList> {
  return postJson<CookieHealthList>('/api/v1/health/cookie/check-all');
}

/** 切换账号：覆写该平台 Cookie 存储并立即校验，返回校验结果。 */
export function switchAccountCookie(platform: string, cookie: string): Promise<CookieHealth> {
  return putJson<CookieHealth>(`/api/v1/health/cookie/${platform}`, { cookie });
}

export function getSystemHealth(): Promise<SystemHealthList> {
  return getJson<SystemHealthList>('/api/v1/health/system');
}

/** 无认证存活探针（部署/运维用，不进 openapi 契约）。 */
export function pingHealth(): Promise<{ status: string }> {
  return getJson<{ status: string }>('/api/v1/healthz');
}
