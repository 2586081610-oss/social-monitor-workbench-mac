/** 个人来源（F2 通道）：来源状态、手动同步、定时发现配置（默认关闭）。
 *
 * 隐私约定：登录态 cookie 只在本机文件，页面不显示内容；同步失败原因如实展示。
 */
import { useEffect, useState } from 'react';
import { ExternalLink, Search } from 'lucide-react';
import { Heart, Play, RefreshCw, Save, Square } from 'lucide-react';
import { ErrorState, LoadingState } from '../components/common/StateViews';
import { ToneBadge } from '../components/common/StatusBadge';
import Toggle from '../components/common/Toggle';
import {
  getPersonalSourceStatus,
  getPersonalSourceWorks,
  getPersonalSyncLoopStatus,
  putPersonalSourceSchedule,
  startPersonalSyncLoop,
  stopPersonalSyncLoop,
  triggerPersonalSourceSync,
} from '../services/personalSource';
import type { PersonalScope, PersonalWorkItem } from '../services/personalSource';
import { useAsyncData, usePolling } from '../hooks/useApi';
import { formatDateTime } from '../utils/format';

const SCOPE_OPTIONS: { value: PersonalScope; label: string }[] = [
  { value: 'like', label: '点赞列表' },
  { value: 'collection', label: '平铺收藏' },
  { value: 'collections', label: '收藏夹及夹内作品' },
];

const INTERVAL_OPTIONS = [
  { label: '每 1 小时', minutes: 60 },
  { label: '每 6 小时', minutes: 360 },
  { label: '每 12 小时', minutes: 720 },
  { label: '每 24 小时', minutes: 1440 },
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

function Readiness({ ok, okLabel, missingLabel }: { ok: boolean; okLabel: string; missingLabel: string }) {
  return (
    <ToneBadge tone={ok ? 'success' : 'warning'} label={ok ? okLabel : missingLabel} />
  );
}

export default function PersonalSourcePage() {
  const statusQuery = useAsyncData(() => getPersonalSourceStatus(), []);
  usePolling(statusQuery.refetch, true, 30000);

  const [enabled, setEnabled] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState(360);
  const [scopes, setScopes] = useState<PersonalScope[]>(['like', 'collection', 'collections']);
  const [secUserId, setSecUserId] = useState('');
  const [autoDownload, setAutoDownload] = useState(false);
  const [formLoaded, setFormLoaded] = useState(false);
  const [saveMsg, setSaveMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);

  // 状态到达后把当前配置装进表单（仅一次，避免轮询覆盖用户正在编辑的值）
  useEffect(() => {
    const schedule = statusQuery.data?.schedule;
    if (schedule && !formLoaded) {
      setEnabled(schedule.enabled);
      setIntervalMinutes(schedule.interval_minutes);
      setScopes(schedule.scopes.length ? schedule.scopes : ['like', 'collection', 'collections']);
      setSecUserId(schedule.sec_user_id);
      setAutoDownload(schedule.auto_download);
      setFormLoaded(true);
    }
  }, [statusQuery.data, formLoaded]);

  const toggleScope = (value: PersonalScope) => {
    setScopes((prev) => (prev.includes(value) ? prev.filter((s) => s !== value) : [...prev, value]));
  };

  const saveSchedule = async () => {
    if (!scopes.length) {
      setSaveMsg({ ok: false, text: '至少选择一个要读取的列表' });
      return;
    }
    setSaving(true);
    setSaveMsg(null);
    try {
      const saved = await putPersonalSourceSchedule({
        enabled,
        interval_minutes: intervalMinutes,
        scopes,
        sec_user_id: secUserId.trim(),
        auto_download: autoDownload,
      });
      const parts: string[] = [];
      parts.push(
        saved.enabled
          ? `定时发现开启，每 ${saved.interval_minutes} 分钟检查一次新增`
          : '定时发现保持关闭（手动同步仍可用）',
      );
      if (saved.enabled && !statusQuery.data?.schedule_active) {
        parts.push('注意：当前没有任务进程在运行，定时不会执行（在工作区根目录执行 ./start_personal_worker.sh，只执行已授权的个人任务）');
      }
      parts.push(saved.auto_download ? '个人来源自动下载已授权（仅处理个人来源作品）' : '个人来源自动下载保持关闭');
      setSaveMsg({ ok: true, text: `已保存：${parts.join('；')}` });
      statusQuery.refetch();
    } catch (err) {
      const message = err instanceof Error ? err.message : '保存失败';
      setSaveMsg({ ok: false, text: message });
    } finally {
      setSaving(false);
    }
  };

  const syncNow = async () => {
    setSyncing(true);
    try {
      await triggerPersonalSourceSync();
      setSaveMsg({ ok: true, text: '手动同步已启动，稍后刷新查看结果' });
      setTimeout(() => statusQuery.refetch(), 1500);
    } catch (err) {
      const message = err instanceof Error ? err.message : '启动失败';
      setSaveMsg({ ok: false, text: message });
    } finally {
      setSyncing(false);
    }
  };

  // 连续同步循环：状态轮询 + 启停
  const loopQuery = useAsyncData(() => getPersonalSyncLoopStatus(), []);
  usePolling(loopQuery.refetch, true, 15000);
  const [startingLoop, setStartingLoop] = useState(false);
  const [stoppingLoop, setStoppingLoop] = useState(false);
  const [loopMsg, setLoopMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const startLoop = async (maxRounds: number) => {
    setStartingLoop(true);
    setLoopMsg(null);
    try {
      await startPersonalSyncLoop(maxRounds);
      setLoopMsg({ ok: true, text: `连续同步已启动（上限 ${maxRounds} 轮）：只读列表元数据，绝不下载；可随时停止` });
      loopQuery.refetch();
    } catch (err) {
      const message = err instanceof Error ? err.message : '启动失败';
      setLoopMsg({ ok: false, text: message });
    } finally {
      setStartingLoop(false);
    }
  };

  const stopLoop = async () => {
    setStoppingLoop(true);
    setLoopMsg(null);
    try {
      const res = await stopPersonalSyncLoop();
      setLoopMsg({ ok: true, text: res.stopped ? '已请求停止：当前轮结束后安全退出（不中断进行中的轮）' : '循环本就未在运行' });
      loopQuery.refetch();
    } catch (err) {
      const message = err instanceof Error ? err.message : '停止失败';
      setLoopMsg({ ok: false, text: message });
    } finally {
      setStoppingLoop(false);
    }
  };

  // 已抓取作品实时清单：分页/过滤 + 随循环自动刷新（每 12 秒）
  const [worksPage, setWorksPage] = useState(0);
  const [typeFilter, setTypeFilter] = useState<'' | 'video' | 'image'>('');
  const [statusFilter, setStatusFilter] = useState<'' | 'NEW' | 'DOWNLOADED' | 'DONE' | 'FAILED'>('');
  const [titleQuery, setTitleQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [worksTick, setWorksTick] = useState(0);
  const PAGE_SIZE = 50;
  const worksQuery = useAsyncData(
    () =>
      getPersonalSourceWorks({
        limit: PAGE_SIZE,
        offset: worksPage * PAGE_SIZE,
        content_type: typeFilter || null,
        status: statusFilter || null,
        q: appliedQuery || null,
      }),
    [worksPage, typeFilter, statusFilter, appliedQuery, worksTick],
  );
  usePolling(worksQuery.refetch, true, 12000);
  // 联动：循环每完成一轮（last_poll 变化），立即刷新清单
  const lastPollKey = loopQuery.data?.last_poll?.finished_at ?? '';
  useEffect(() => {
    if (lastPollKey) setWorksTick((t) => t + 1);
  }, [lastPollKey]);

  const works = worksQuery.data;
  const totalPages = works ? Math.max(1, Math.ceil(works.total / PAGE_SIZE)) : 1;

  if (statusQuery.loading && !statusQuery.data) return <LoadingState />;
  if (statusQuery.error && !statusQuery.data) {
    return <ErrorState message={statusQuery.error.message} onRetry={statusQuery.refetch} />;
  }

  const status = statusQuery.data;
  const lastSync = status?.last_sync ?? null;
  const lastOk = lastSync?.status === 'ok';
  const lastPartial = lastSync?.status === 'partial';

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <header className="flex items-center gap-2">
        <Heart className="h-5 w-5 text-primary" />
        <h1 className="text-base font-semibold text-ink">个人来源（抖音 · F2 通道）</h1>
      </header>

      <Section
        title="来源状态"
        desc="只读本人点赞、平铺收藏、平台收藏夹及夹内作品；不读取观看历史和聊天记录。"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Readiness ok={!!status?.runtime_available} okLabel="F2 运行时就绪" missingLabel="F2 运行时未安装" />
          <Readiness ok={!!status?.cookie_file_set} okLabel="登录态文件已配置" missingLabel="登录态文件缺失" />
          <Readiness
            ok={!!status?.sec_user_id_set}
            okLabel="本人 sec_user_id 已填（同步时不再自动识别）"
            missingLabel="sec_user_id 未填（同步时尝试自动识别）"
          />
          <Readiness
            ok={!!status?.monitor_online || !!status?.personal_worker_online}
            okLabel={status?.personal_worker_online ? '个人任务进程在线' : '监控层在线'}
            missingLabel="个人任务进程未运行"
          />
          <Readiness
            ok={!!status?.schedule_active}
            okLabel="定时执行中"
            missingLabel={
              status?.schedule?.enabled
                ? '定时已开启但没有任何任务进程运行，暂不执行'
                : '定时未开启'
            }
          />
          <Readiness
            ok={!!status?.schedule?.auto_download}
            okLabel={`个人来源自动下载已授权${status?.pending_download_count ? `（待下载 ${status.pending_download_count}）` : ''}`}
            missingLabel="个人来源自动下载关闭"
          />
        </div>
        <div className="mt-3 rounded-md border border-line bg-sidebar p-3 text-xs leading-5 text-ink-muted">
          <p>
            最近一次同步：
            {lastSync ? (
              <>
                <span className="text-ink">{formatDateTime(lastSync.finished_at)}</span>
                {' '}
                <ToneBadge
                  tone={lastOk ? 'success' : lastPartial ? 'warning' : 'danger'}
                  label={lastOk ? '成功' : lastPartial ? '部分完成' : '失败'}
                />
                {lastSync.new_works > 0 ? <span className="ml-1 text-ink">新增作品 {lastSync.new_works}</span> : null}
                {lastSync.detail ? <span className="ml-1 text-danger">{lastSync.detail}</span> : null}
              </>
            ) : (
              '尚未执行过'
            )}
          </p>
          <p className="mt-1">
            自动下载由下方「个人来源自动下载」开关单独授权（默认关闭，只处理个人来源作品，
            不启动其他来源的全局队列）；全局流水线（环境变量 MONITOR_RUN_PIPELINE={String(!!status?.pipeline_enabled)}）
            与之相互独立。下载失败会如实标注原因，不会打开或播放作品网页兜底。
          </p>
          {enabled && !status?.schedule_active ? (
            <p className="mt-1 text-warning">
              定时发现已开启但没有任何任务进程在运行。start_all.sh 只启动前后端、不含定时任务；
              请在工作区根目录执行
              <code className="mx-1 rounded bg-surface px-1 py-0.5 text-ink">./start_personal_worker.sh</code>
              （使用项目 .venv 解释器，只执行已授权的个人同步/下载，不启动其他平台轮询）。
            </p>
          ) : null}
        </div>
        <div className="mt-3">
          <button
            type="button"
            onClick={syncNow}
            disabled={syncing || !status?.runtime_available || !status?.cookie_file_set}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-white transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${syncing ? 'animate-spin' : ''}`} />
            {syncing ? '启动中…' : '立即同步一次'}
          </button>
        </div>
      </Section>

      <Section
        title="连续同步到完整"
        desc="一轮接一轮读取列表元数据（作品ID/标题/类型/发布时间/链接/封面），直到点赞、收藏、收藏夹全部完整。绝不下载媒体（下载由下方独立开关授权）；每轮 15 分钟预算限速，对账号最温和。"
      >
        {(() => {
          const loop = loopQuery.data;
          const st = loop?.state;
          return (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <ToneBadge
                  tone={loop?.running ? 'success' : 'neutral'}
                  label={loop?.running ? `运行中：第 ${st?.rounds_done ?? 0} / ${st?.max_rounds ?? '-'} 轮` : '未在运行'}
                />
                {loop?.running && st?.stop_requested ? <ToneBadge tone="warning" label="已请求停止（本轮结束生效）" /> : null}
                {!loop?.running && st?.final_status ? (
                  <ToneBadge
                    tone={st.final_status === 'all_complete' ? 'success' : 'warning'}
                    label={
                      st.final_status === 'all_complete'
                        ? '全部范围完整 ✅'
                        : st.final_status === 'stopped_by_user'
                          ? '已手动停止'
                          : st.final_status === 'max_rounds_reached'
                            ? '达到轮数上限'
                            : `结束：${st.final_status}`
                    }
                  />
                ) : null}
                {loop ? (
                  <span className="text-xs text-ink-muted">
                    作品总数 {loop.works_total}
                    {loop.last_poll ? ` · 最近一轮 ${loop.last_poll.status}（新增 ${loop.last_poll.new_works}）` : ''}
                  </span>
                ) : null}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                {[30, 60, 120].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => startLoop(n)}
                    disabled={startingLoop || stoppingLoop || !!loop?.running || !status?.runtime_available || !status?.cookie_file_set}
                    className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-white transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <Play className="h-4 w-4" />
                    启动（上限 {n} 轮）
                  </button>
                ))}
                <button
                  type="button"
                  onClick={stopLoop}
                  disabled={stoppingLoop || startingLoop || !loop?.running}
                  className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-ink transition-colors hover:bg-sidebar disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Square className="h-4 w-4" />
                  {stoppingLoop ? '停止中…' : '停止'}
                </button>
              </div>

              {loopMsg ? (
                <p className={`text-xs ${loopMsg.ok ? 'text-success' : 'text-danger'}`}>{loopMsg.text}</p>
              ) : null}
              <p className="text-xs leading-5 text-ink-muted">
                自动停止条件：全部范围完整 / 连续 3 轮失败 / 达到轮数上限。运行中每轮结果写入同步历史；
                停止在当前轮结束后生效，不中断进行中的读取。
              </p>
            </div>
          );
        })()}
      </Section>

      <Section
        title="已抓取作品"
        desc="连续同步抓到的列表元数据（作品ID/标题/类型/发布时间/链接/封面/状态/媒体路径），随爬取轮次实时更新；媒体路径有值表示文件已下载归档。"
      >
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-2 h-4 w-4 text-ink-muted" />
            <input
              value={titleQuery}
              onChange={(e) => setTitleQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setWorksPage(0);
                  setAppliedQuery(titleQuery.trim());
                }
              }}
              placeholder="按标题搜索，回车确认"
              className="w-56 rounded-md border border-line bg-surface py-1.5 pl-8 pr-2 text-sm text-ink outline-none focus:border-primary"
            />
          </div>
          {([['', '全部类型'], ['video', '视频'], ['image', '图文']] as const).map(([v, label]) => (
            <button
              key={v || 'all-type'}
              type="button"
              onClick={() => { setWorksPage(0); setTypeFilter(v); }}
              className={`rounded-md border px-2.5 py-1 text-xs transition-colors ${
                typeFilter === v ? 'border-primary bg-primary text-white' : 'border-line text-ink hover:bg-sidebar'
              }`}
            >
              {label}
            </button>
          ))}
          {([['', '全部状态'], ['NEW', 'NEW'], ['DOWNLOADED', 'DOWNLOADED'], ['DONE', 'DONE'], ['FAILED', 'FAILED']] as const).map(([v, label]) => (
            <button
              key={v || 'all-status'}
              type="button"
              onClick={() => { setWorksPage(0); setStatusFilter(v); }}
              className={`rounded-md border px-2.5 py-1 text-xs transition-colors ${
                statusFilter === v ? 'border-primary bg-primary text-white' : 'border-line text-ink hover:bg-sidebar'
              }`}
            >
              {label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => { worksQuery.refetch(); loopQuery.refetch(); }}
            className="rounded-md border border-line px-2.5 py-1 text-xs text-ink hover:bg-sidebar"
          >
            <RefreshCw className="mr-1 inline h-3 w-3" />
            刷新
          </button>
          <span className="text-xs text-ink-muted">
            共 {works?.total ?? '…'} 条 · 第 {worksPage + 1}/{totalPages} 页 · {loopQuery.data?.running ? '爬取运行中，自动更新' : '爬取未运行'}
          </span>
        </div>

        {worksQuery.loading && !worksQuery.data ? (
          <p className="mt-3 text-xs text-ink-muted">加载中…</p>
        ) : worksQuery.error && !worksQuery.data ? (
          <p className="mt-3 text-xs text-danger">加载失败：{worksQuery.error.message}</p>
        ) : (
          <div className="mt-3 overflow-hidden rounded-md border border-line">
            <table className="w-full table-fixed text-left text-xs">
              <thead className="bg-sidebar text-ink-muted">
                <tr>
                  <th className="w-14 px-2 py-2 font-medium">封面</th>
                  <th className="px-2 py-2 font-medium">标题</th>
                  <th className="w-14 px-2 py-2 font-medium">类型</th>
                  <th className="w-24 px-2 py-2 font-medium">发布时间</th>
                  <th className="w-24 px-2 py-2 font-medium">状态</th>
                  <th className="w-16 px-2 py-2 font-medium">链接</th>
                  <th className="w-40 px-2 py-2 font-medium">媒体路径</th>
                </tr>
              </thead>
              <tbody>
                {(works?.items ?? []).map((w: PersonalWorkItem) => (
                  <tr key={w.external_id} className="border-t border-line align-middle hover:bg-sidebar/60">
                    <td className="px-2 py-1.5">
                      {w.cover_url ? (
                        <img
                          src={w.cover_url}
                          alt=""
                          referrerPolicy="no-referrer"
                          className="h-12 w-9 rounded object-cover"
                          onError={(e) => { (e.target as HTMLImageElement).style.visibility = 'hidden'; }}
                        />
                      ) : (
                        <div className="h-12 w-9 rounded bg-sidebar" />
                      )}
                    </td>
                    <td className="truncate px-2 py-1.5 text-ink" title={w.title ?? ''}>
                      {w.title || '（无标题）'}
                    </td>
                    <td className="px-2 py-1.5">
                      <ToneBadge tone={w.content_type === 'video' ? 'success' : 'warning'} label={w.content_type === 'video' ? '视频' : '图文'} />
                    </td>
                    <td className="px-2 py-1.5 text-ink-muted">{(w.published_at || '未知').slice(0, 10)}</td>
                    <td className="px-2 py-1.5">
                      <ToneBadge
                        tone={w.status === 'DOWNLOADED' || w.status === 'DONE' ? 'success' : w.status === 'FAILED' ? 'danger' : 'neutral'}
                        label={w.status || '—'}
                      />
                    </td>
                    <td className="px-2 py-1.5">
                      {w.url ? (
                        <a href={w.url} target="_blank" rel="noreferrer" className="text-primary hover:underline" title={w.url ?? ''}>
                          <ExternalLink className="h-3.5 w-3.5" />
                        </a>
                      ) : '—'}
                    </td>
                    <td className="truncate px-2 py-1.5 text-ink-muted" title={w.media_path ?? ''}>
                      {w.media_path || '—'}
                    </td>
                  </tr>
                ))}
                {(works?.items?.length ?? 0) === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-2 py-6 text-center text-ink-muted">
                      没有匹配的作品
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            onClick={() => setWorksPage((p) => Math.max(0, p - 1))}
            disabled={worksPage === 0}
            className="rounded-md border border-line px-3 py-1 text-xs text-ink hover:bg-sidebar disabled:cursor-not-allowed disabled:opacity-50"
          >
            上一页
          </button>
          <button
            type="button"
            onClick={() => setWorksPage((p) => (works && (p + 1) * PAGE_SIZE < works.total ? p + 1 : p))}
            disabled={!works || (worksPage + 1) * PAGE_SIZE >= works.total}
            className="rounded-md border border-line px-3 py-1 text-xs text-ink hover:bg-sidebar disabled:cursor-not-allowed disabled:opacity-50"
          >
            下一页
          </button>
          <span className="text-xs text-ink-muted">
            每页 {PAGE_SIZE} 条
          </span>
        </div>
      </Section>

      <Section
        title="定时发现新增作品"
        desc="默认关闭。开启后由监控层按设定间隔读取列表并登记新作品；是否自动下载由下方独立开关授权。"
      >
        <div className="flex items-center justify-between rounded-md border border-line px-3 py-2.5">
          <div>
            <p className="text-sm text-ink">定时发现</p>
            <p className="text-xs text-ink-muted">
              {enabled ? `每 ${intervalMinutes} 分钟检查一次` : '当前关闭，不会自动读取本人数据'}
            </p>
          </div>
          <Toggle checked={enabled} onChange={setEnabled} label="定时发现开关" />
        </div>

        <div className="mt-2 flex items-center justify-between rounded-md border border-line px-3 py-2.5">
          <div>
            <p className="text-sm text-ink">个人来源自动下载</p>
            <p className="text-xs text-ink-muted">
              {autoDownload
                ? '已授权：新登记的个人来源作品将自动下载归档（默认关闭）'
                : '默认关闭：只登记发现的新作品，不自动下载'}
            </p>
          </div>
          <Toggle checked={autoDownload} onChange={setAutoDownload} label="个人来源自动下载开关" />
        </div>

        <div className="mt-3 space-y-2">
          <p className="text-xs font-medium text-ink-muted">读取范围</p>
          {SCOPE_OPTIONS.map((option) => (
            <label
              key={option.value}
              className="flex cursor-pointer items-center gap-2 rounded-md border border-line px-3 py-2 text-sm text-ink hover:bg-sidebar"
            >
              <input
                type="checkbox"
                checked={scopes.includes(option.value)}
                onChange={() => toggleScope(option.value)}
                className="h-4 w-4 accent-primary"
              />
              {option.label}
            </label>
          ))}
        </div>

        <div className="mt-3 space-y-2">
          <p className="text-xs font-medium text-ink-muted">检查频率</p>
          <div className="flex flex-wrap gap-2">
            {INTERVAL_OPTIONS.map((option) => (
              <button
                key={option.minutes}
                type="button"
                onClick={() => setIntervalMinutes(option.minutes)}
                className={`rounded-md border px-3 py-1.5 text-sm transition-colors ${
                  intervalMinutes === option.minutes
                    ? 'border-primary bg-primary text-white'
                    : 'border-line text-ink hover:bg-sidebar'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="mt-3">
          <label className="text-xs font-medium text-ink-muted" htmlFor="sec-user-id">
            本人 sec_user_id（可选：抖音网页版个人主页链接中的 sec_uid；留空时同步尝试自动识别，手动填写优先）
          </label>
          <input
            id="sec-user-id"
            value={secUserId}
            onChange={(e) => setSecUserId(e.target.value)}
            placeholder="例如 MS4wLjABAAAA…"
            className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
          />
          <p className="mt-1 text-xs leading-5 text-ink-muted">
            留空时，同步会尝试用已配置的登录态自动确认本人标识；这只说明「会尝试」，
            不代表登录态可用或已经验证。自动识别失败时同步会明确报错，不会把失败当成空点赞。
            也可继续手动填写本人标识（填写后同步不再自动识别）。
          </p>
        </div>

        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            onClick={saveSchedule}
            disabled={saving}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-white transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Save className="h-4 w-4" />
            {saving ? '保存中…' : '保存配置'}
          </button>
          {saveMsg ? (
            <span className={`text-xs ${saveMsg.ok ? 'text-success' : 'text-danger'}`}>{saveMsg.text}</span>
          ) : null}
        </div>
      </Section>
    </div>
  );
}
