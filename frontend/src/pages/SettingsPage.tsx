/** P8 设置：cookie 状态 + 重新扫码引导 + 自动刷新爬取 + 轮询间隔配置 + API 连接 + 系统健康 + 公众号占位。 */
import { useState } from 'react';
import { KeyRound, RefreshCw, Settings2, UserCog } from 'lucide-react';
import { CookieStatusBadge, ToneBadge } from '../components/common/StatusBadge';
import { ErrorState, LoadingState } from '../components/common/StateViews';
import Toggle from '../components/common/Toggle';
import {
  checkAllCookieHealth,
  checkCookieHealth,
  getCookieHealth,
  getSystemHealth,
  pingHealth,
  switchAccountCookie,
} from '../services/health';
import { getArchivePolicy } from '../services/archive';
import { useAsyncData, usePolling } from '../hooks/useApi';
import { useRelogin } from '../state/relogin';
import { API_BASE_URL, getToken, setToken } from '../config/api';
import { readAggSettings, setAutoRefresh, setAutoRefreshMinutes } from '../utils/aggSettings';
import {
  COOKIE_STATUS_LABEL,
  PLATFORM_LABEL,
  POLL_INTERVAL_INFO,
  SYSTEM_COMPONENT_LABEL,
  SYSTEM_COMPONENT_STATUS,
} from '../utils/constants';
import { formatDateTime } from '../utils/format';
import type { CookieHealth } from '../types/api.d';

/** 各平台 Cookie 导出指引（切换账号弹窗展示）。 */
const COOKIE_HINTS: Record<string, string> = {
  douyin: '登录 douyin.com 后：F12 → 网络(Network) → 刷新页面 → 点第一个 www.douyin.com 请求 → 请求标头 → 复制整段 Cookie（需包含 sessionid）。',
  bilibili: '登录 bilibili.com 后：F12 → 网络 → 复制任意 api.bilibili.com 请求的整段 Cookie（需包含 SESSDATA）。',
  xhs: '登录 xiaohongshu.com 后：F12 → 网络 → 复制任意请求的整段 Cookie（需包含 web_session）。',
  twitter: '登录 x.com 后：F12 → 网络 → 复制任意请求的整段 Cookie（需包含 auth_token；在线检测需要代理可达）。',
};

const SYSTEM_TONE: Record<string, 'success' | 'danger' | 'warning' | 'neutral'> = {
  online: 'success',
  offline: 'danger',
  degraded: 'warning',
  unknown: 'neutral',
};

const AUTO_REFRESH_PRESETS: { label: string; minutes: number }[] = [
  { label: '1 小时', minutes: 60 },
  { label: '2 小时', minutes: 120 },
  { label: '5 小时', minutes: 300 },
  { label: '12 小时', minutes: 720 },
  { label: '24 小时', minutes: 1440 },
];

function Section({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4 shadow-card">
      <h2 className="text-sm font-semibold text-ink">{title}</h2>
      {desc ? <p className="mt-0.5 text-xs text-ink-muted">{desc}</p> : null}
      <div className="mt-3">{children}</div>
    </section>
  );
}

export default function SettingsPage() {
  const [tokenInput, setTokenInput] = useState(getToken());
  const [connMsg, setConnMsg] = useState<string | null>(null);
  const [connOk, setConnOk] = useState<boolean | null>(null);
  const [autoRefresh, setAutoRefreshState] = useState(() => readAggSettings().autoRefresh);
  const [autoMinutes, setAutoMinutes] = useState(() => readAggSettings().autoRefreshMinutes);
  const { open } = useRelogin();

  const cookieQuery = useAsyncData(() => getCookieHealth(), []);
  const systemQuery = useAsyncData(() => getSystemHealth(), []);
  const archivePolicyQuery = useAsyncData(() => getArchivePolicy(), []);
  usePolling(cookieQuery.refetch, true, 30000);

  const cookies = cookieQuery.data?.items ?? [];
  const systems = systemQuery.data?.items ?? [];

  const testConnection = async () => {
    setTokenInput(tokenInput.trim());
    setToken(tokenInput.trim());
    setConnMsg(null);
    setConnOk(null);
    try {
      await pingHealth();
      setConnOk(true);
      setConnMsg('连接成功：聚合 API 可达（无认证探针）。');
    } catch (e) {
      setConnOk(false);
      setConnMsg(e instanceof Error ? `连接失败：${e.message}` : '连接失败');
    }
  };

  const handleAutoRefreshToggle = (value: boolean) => {
    setAutoRefresh(value);
    setAutoRefreshState(value);
  };

  const handlePreset = (minutes: number) => {
    setAutoRefreshMinutes(minutes);
    setAutoMinutes(minutes);
  };

  const handleCustomMinutes = (value: string) => {
    const n = Number(value);
    if (Number.isFinite(n) && n > 0) {
      setAutoRefreshMinutes(n);
      setAutoMinutes(Math.round(n));
    }
  };

  const intervalLabel =
    autoMinutes >= 60 && autoMinutes % 60 === 0 ? `${autoMinutes / 60} 小时` : `${autoMinutes} 分钟`;

  // 账号健康：立即检测 / 一键检测 / 切换账号
  const [checkingAll, setCheckingAll] = useState(false);
  const [checking, setChecking] = useState<string | null>(null);
  const [switchTarget, setSwitchTarget] = useState<string | null>(null);
  const [switchInput, setSwitchInput] = useState('');
  const [switchBusy, setSwitchBusy] = useState(false);
  const [switchResult, setSwitchResult] = useState<CookieHealth | null>(null);
  const [switchMsg, setSwitchMsg] = useState<string | null>(null);

  const handleCheckPlatform = async (platform: string) => {
    setChecking(platform);
    try {
      await checkCookieHealth(platform);
    } catch {
      // 校验失败也以列表最新数据为准
    } finally {
      setChecking(null);
      await cookieQuery.refetch();
    }
  };

  const handleCheckAll = async () => {
    setCheckingAll(true);
    try {
      await checkAllCookieHealth();
    } catch {
      // 同上
    } finally {
      setCheckingAll(false);
      await cookieQuery.refetch();
    }
  };

  const openSwitch = (platform: string) => {
    setSwitchTarget(platform);
    setSwitchInput('');
    setSwitchResult(null);
    setSwitchMsg(null);
  };

  const handleSwitchSave = async () => {
    if (!switchTarget) return;
    const cookie = switchInput.trim();
    if (cookie.length < 20) {
      setSwitchMsg('Cookie 内容太短（至少 20 字符），请确认复制的是整段请求 Cookie。');
      return;
    }
    setSwitchBusy(true);
    setSwitchMsg(null);
    setSwitchResult(null);
    try {
      const result = await switchAccountCookie(switchTarget, cookie);
      setSwitchResult(result);
      await cookieQuery.refetch();
    } catch (e) {
      setSwitchMsg(e instanceof Error ? `切换失败：${e.message}` : '切换失败，请稍后重试');
    } finally {
      setSwitchBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-ink">设置</h1>
        <p className="text-xs text-ink-muted">API 地址 {API_BASE_URL}</p>
      </div>

      {/* API 连接 / Token */}
      <Section title="API 连接与口令" desc="前端通过 Bearer token 访问聚合 API；口令保存在浏览器本地。">
        <div className="flex flex-wrap items-center gap-2">
          <KeyRound className="h-4 w-4 text-ink-muted" />
          <input
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="ADMIN_TOKEN"
            type="password"
            className="min-w-0 flex-1 rounded-md border border-line px-3 py-2 font-mono text-sm focus:border-primary focus:outline-none"
          />
          <button
            type="button"
            onClick={testConnection}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm text-white transition-colors hover:bg-primary-hover"
          >
            <RefreshCw className="h-4 w-4" />
            保存并测试
          </button>
        </div>
        {connMsg ? (
          <p className={`mt-2 text-sm ${connOk ? 'text-success' : 'text-danger'}`}>{connMsg}</p>
        ) : null}
      </Section>

      <Section title="本地归档规则" desc="读取服务端当前生效的设置；页面只显示根标识与友好名称，不返回机器绝对路径。">
        {archivePolicyQuery.loading ? (
          <LoadingState label="读取本地归档规则…" />
        ) : archivePolicyQuery.error || !archivePolicyQuery.data ? (
          <ErrorState message={archivePolicyQuery.error?.message ?? '归档规则暂不可用'} onRetry={archivePolicyQuery.refetch} />
        ) : (
          <div className="space-y-3 text-sm">
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="rounded-md border border-line px-3 py-2">
                <p className="text-xs text-ink-muted">归档时区</p>
                <p className="mt-1 font-medium text-ink">{archivePolicyQuery.data.timezone}（北京时间）</p>
              </div>
              <div className="rounded-md border border-line px-3 py-2">
                <p className="text-xs text-ink-muted">新目录命名规则</p>
                <p className="mt-1 break-all font-mono text-xs text-ink-secondary">{archivePolicyQuery.data.directory_template}</p>
              </div>
            </div>
            <div className="rounded-md border border-line px-3 py-2">
              <p className="text-xs text-ink-muted">单作品信息文件</p>
              <p className="mt-1 break-words text-xs text-ink-secondary">{archivePolicyQuery.data.information_files.join(' · ') || '未配置'}</p>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="rounded-md border border-line px-3 py-2">
                <p className="text-xs text-ink-muted">合集清单文件</p>
                <p className="mt-1 text-xs text-ink-secondary">{archivePolicyQuery.data.collection_file_name}</p>
              </div>
              <div className="rounded-md border border-line px-3 py-2">
                <p className="text-xs text-ink-muted">归档总清单文件</p>
                <p className="mt-1 text-xs text-ink-secondary">{archivePolicyQuery.data.catalog_file_name}</p>
              </div>
            </div>
            <div className="rounded-md border border-info/30 bg-info-light px-3 py-2 text-xs leading-5 text-info">
              <p className="font-semibold">时间来源约定</p>
              <p>发布时间只采用平台信息或人工核实；没有证据时保持未知。下载完成时间只记录已验证媒体的真实写入完成，不用扫描时间、文件时间或重复使用媒体的时间代替。</p>
              <p>网页日期筛选按北京时间日历日解释；日/分钟精度保持原精度显示。</p>
            </div>
            <div>
              <p className="mb-1 text-xs font-medium text-ink-secondary">允许的平台</p>
              <div className="flex flex-wrap gap-2">
                {archivePolicyQuery.data.platforms.map((platform) => (
                  <span key={platform.value} className="rounded-full bg-sidebar px-2.5 py-1 text-xs text-ink-secondary">{platform.label}</span>
                ))}
              </div>
              <p className="mb-1 mt-3 text-xs font-medium text-ink-secondary">服务端允许的归档根目录</p>
              <div className="grid gap-2 sm:grid-cols-2">
                {archivePolicyQuery.data.roots.map((root) => (
                  <div key={root.root_key} className="flex items-center justify-between gap-3 rounded-md border border-line px-3 py-2 text-xs">
                    <span className="min-w-0 truncate text-ink">{root.label} <span className="font-mono text-ink-muted">({root.root_key})</span></span>
                    <span className={`shrink-0 rounded-full px-2 py-0.5 ${root.configured ? 'bg-success-light text-success' : 'bg-warning-light text-warning'}`}>{root.configured ? '可用' : '当前不可用'}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </Section>

      {/* 凭据健康度（账号健康）：真校验 + 账号切换 */}
      <Section title="账号健康（四平台登录态）" desc="已登录 / 未登录由在线校验实时判定，探测不到的如实显示，不编造。">
        <div className="mb-3 flex items-center justify-between gap-2">
          <p className="text-xs text-ink-muted">检测使用各平台轻量接口，不会发帖、点赞或改动账号。</p>
          <button
            type="button"
            onClick={handleCheckAll}
            disabled={checkingAll || checking !== null}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-xs text-ink-secondary transition-colors hover:bg-sidebar disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${checkingAll ? 'animate-spin' : ''}`} />
            {checkingAll ? '检测中…' : '一键检测全部'}
          </button>
        </div>
        {cookieQuery.loading ? (
          <LoadingState label="加载账号状态…" />
        ) : cookieQuery.error ? (
          <ErrorState message={cookieQuery.error.message} onRetry={cookieQuery.refetch} />
        ) : cookies.length === 0 ? (
          <p className="text-xs text-ink-muted">暂无账号健康数据。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs uppercase tracking-widest text-ink-muted">
                  <th className="px-3 py-2 font-semibold">平台</th>
                  <th className="px-3 py-2 font-semibold">登录状态</th>
                  <th className="px-3 py-2 font-semibold">最近检查</th>
                  <th className="px-3 py-2 font-semibold">校验详情</th>
                  <th className="px-3 py-2 text-right font-semibold">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {cookies.map((c) => (
                  <tr key={c.platform}>
                    <td className="px-3 py-2.5 font-medium text-ink">{PLATFORM_LABEL[c.platform] ?? c.platform}</td>
                    <td className="px-3 py-2.5"><CookieStatusBadge status={c.status} /></td>
                    <td className="px-3 py-2.5 text-xs text-ink-muted">{formatDateTime(c.last_checked_at)}</td>
                    <td className="px-3 py-2.5 text-xs text-ink-secondary">{c.note ?? '—'}</td>
                    <td className="px-3 py-2.5 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          onClick={() => handleCheckPlatform(c.platform)}
                          disabled={checking !== null || checkingAll}
                          className="rounded-md border border-line px-2 py-1 text-xs text-ink-secondary transition-colors hover:bg-sidebar disabled:opacity-50"
                        >
                          {checking === c.platform ? '检测中…' : '立即检测'}
                        </button>
                        <button
                          type="button"
                          onClick={() => openSwitch(c.platform)}
                          className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-ink-secondary transition-colors hover:bg-sidebar"
                        >
                          <UserCog className="h-3 w-3" />
                          切换账号
                        </button>
                        {c.status === 'NEEDS_RELOGIN' ? (
                          <button
                            type="button"
                            onClick={() => open(c.platform)}
                            className="rounded-md border border-danger/40 px-2 py-1 text-xs text-danger transition-colors hover:bg-danger hover:text-white"
                          >
                            重新登录
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-2 text-[11px] text-ink-muted">
          状态说明：{COOKIE_STATUS_LABEL.OK}＝在线校验通过；{COOKIE_STATUS_LABEL.WARN}＝有会话凭证但网络未打通；{COOKIE_STATUS_LABEL.NEEDS_RELOGIN}＝未登录或凭证失效；{COOKIE_STATUS_LABEL.UNKNOWN}＝响应异常无法判定。
        </p>
      </Section>

      {/* 系统健康 */}
      <Section title="系统健康" desc="监控层 / 外部服务 / 流水线引擎在线状态。">
        {systemQuery.loading ? (
          <LoadingState label="加载系统健康…" />
        ) : systemQuery.error ? (
          <ErrorState message={systemQuery.error.message} onRetry={systemQuery.refetch} />
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {systems.map((s) => (
              <div key={s.component} className="flex items-center justify-between rounded-md border border-line px-3 py-2">
                <span className="text-sm text-ink">{SYSTEM_COMPONENT_LABEL[s.component] ?? s.component}</span>
                <span className="flex items-center gap-2">
                  <span className="text-xs text-ink-muted">{s.detail ?? ''}</span>
                  <ToneBadge tone={SYSTEM_TONE[s.status] ?? 'neutral'} label={SYSTEM_COMPONENT_STATUS[s.status] ?? s.status} />
                </span>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* 自动刷新爬取 */}
      <Section title="自动刷新爬取" desc="Web 端按间隔定时触发全平台爬取；需首页「聚合监控」总开关保持开启。">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-ink-secondary">
            <Toggle checked={autoRefresh} onChange={handleAutoRefreshToggle} label="启用自动刷新" />
            启用自动刷新
          </label>
          <span className="text-xs text-ink-muted">当前间隔：{intervalLabel}</span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {AUTO_REFRESH_PRESETS.map((p) => (
            <button
              key={p.minutes}
              type="button"
              disabled={!autoRefresh}
              onClick={() => handlePreset(p.minutes)}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                autoMinutes === p.minutes
                  ? 'bg-primary text-white'
                  : 'border border-line bg-surface text-ink-secondary hover:bg-sidebar'
              }`}
            >
              {p.label}
            </button>
          ))}
          <label className="flex items-center gap-1.5 text-xs text-ink-muted">
            自定义
            <input
              type="number"
              min={1}
              value={autoMinutes}
              disabled={!autoRefresh}
              onChange={(e) => handleCustomMinutes(e.target.value)}
              className="w-20 rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink focus:border-primary focus:outline-none disabled:opacity-50"
            />
            分钟
          </label>
        </div>
        <p className="mt-2 flex items-center gap-1.5 text-xs text-ink-muted">
          <Settings2 className="h-3.5 w-3.5" />
          自动刷新仅在 Web 端打开时生效。
        </p>
      </Section>

      {/* 轮询间隔配置 */}
      <Section title="轮询间隔配置" desc="P1 由监控层统一调度（环境变量配置），此处仅展示当前默认值。">
        <div className="divide-y divide-line rounded-md border border-line">
          {POLL_INTERVAL_INFO.map((row) => (
            <div key={row.platform} className="flex items-center justify-between px-3 py-2 text-sm">
              <span className="text-ink-secondary">{row.platform}</span>
              <span className="font-mono text-xs text-ink">{row.value}</span>
            </div>
          ))}
        </div>
        <p className="mt-2 flex items-center gap-1.5 text-xs text-ink-muted">
          <Settings2 className="h-3.5 w-3.5" />
          调整方式：monitor 层环境变量（如 RSSHUB_POLL_MINUTES / DOUYIN_POLL_MINUTES），修改后重启监控层生效。
        </p>
      </Section>

      {/* 公众号导入占位 */}
      <Section title="公众号导入" desc="接口预留（B1 Backlog），P1 不实现自动监测与 AI 分析。">
        <button
          type="button"
          disabled
          className="rounded-md border border-line px-3 py-1.5 text-sm text-ink-muted"
          title="接口占位 501"
        >
          公众号导入（预留，暂不可用）
        </button>
      </Section>

      {/* 切换账号弹窗：粘贴 Cookie → 保存落盘 → 立即校验 */}
      {switchTarget ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onClick={() => {
            if (!switchBusy) setSwitchTarget(null);
          }}
        >
          <div
            className="max-h-[85vh] w-full max-w-xl overflow-auto rounded-lg border border-line bg-surface p-4 shadow-card"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-sm font-semibold text-ink">切换账号 · {PLATFORM_LABEL[switchTarget] ?? switchTarget}</h2>
            <p className="mt-1.5 text-xs leading-5 text-ink-muted">{COOKIE_HINTS[switchTarget] ?? ''}</p>
            <textarea
              value={switchInput}
              onChange={(e) => setSwitchInput(e.target.value)}
              rows={5}
              placeholder="粘贴整段 Cookie（Cookie: 后面的完整内容）…"
              className="mt-3 w-full rounded-md border border-line px-3 py-2 font-mono text-xs text-ink focus:border-primary focus:outline-none"
            />
            {switchResult ? (
              <div className="mt-2 flex items-center gap-2 text-sm">
                <span className="text-ink-secondary">校验结果：</span>
                <CookieStatusBadge status={switchResult.status} />
                <span className="text-xs text-ink-muted">{switchResult.note ?? ''}</span>
              </div>
            ) : null}
            {switchMsg ? <p className="mt-2 text-sm text-danger">{switchMsg}</p> : null}
            <p className="mt-2 text-[11px] text-ink-muted">
              保存后立即在线校验；原 Cookie 会被覆盖，下载与采集管道将改用新账号。
            </p>
            <div className="mt-3 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setSwitchTarget(null)}
                disabled={switchBusy}
                className="rounded-md border border-line px-3 py-1.5 text-sm text-ink-secondary disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleSwitchSave}
                disabled={switchBusy || switchInput.trim().length < 20}
                className="rounded-md bg-primary px-3 py-1.5 text-sm text-white transition-colors hover:bg-primary-hover disabled:opacity-50"
              >
                {switchBusy ? '保存并校验中…' : '保存并立即校验'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
