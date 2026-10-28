/** 个人来源（F2 通道）：来源状态、手动同步、定时发现配置（默认关闭）。
 *
 * 隐私约定：登录态 cookie 只在本机文件，页面不显示内容；同步失败原因如实展示。
 */
import { useEffect, useState } from 'react';
import { Heart, RefreshCw, Save } from 'lucide-react';
import { ErrorState, LoadingState } from '../components/common/StateViews';
import { ToneBadge } from '../components/common/StatusBadge';
import Toggle from '../components/common/Toggle';
import {
  getPersonalSourceStatus,
  putPersonalSourceSchedule,
  triggerPersonalSourceSync,
} from '../services/personalSource';
import type { PersonalScope } from '../services/personalSource';
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
      });
      setSaveMsg({
        ok: true,
        text: saved.enabled
          ? `已保存：定时发现开启，每 ${saved.interval_minutes} 分钟检查一次新增`
          : '已保存：定时发现保持关闭（手动同步仍可用）',
      });
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

  if (statusQuery.loading && !statusQuery.data) return <LoadingState />;
  if (statusQuery.error && !statusQuery.data) {
    return <ErrorState message={statusQuery.error.message} onRetry={statusQuery.refetch} />;
  }

  const status = statusQuery.data;
  const lastSync = status?.last_sync ?? null;
  const lastOk = lastSync?.status === 'ok';

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
            okLabel="本人 sec_user_id 已填"
            missingLabel="sec_user_id 未填（点赞列表不可用）"
          />
          <Readiness
            ok={!!status?.pipeline_enabled}
            okLabel="自动下载流水线开启"
            missingLabel="自动下载流水线关闭"
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
                  tone={lastOk ? 'success' : 'danger'}
                  label={lastOk ? '成功' : '失败'}
                />
                {lastSync.new_works > 0 ? <span className="ml-1 text-ink">新增作品 {lastSync.new_works}</span> : null}
                {lastSync.detail ? <span className="ml-1 text-danger">{lastSync.detail}</span> : null}
              </>
            ) : (
              '尚未执行过'
            )}
          </p>
          <p className="mt-1">
            自动下载仅在设置页流水线开启时进行；个人来源作品下载失败会如实标注原因，
            不会打开或播放作品网页兜底。
          </p>
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
        title="定时发现新增作品"
        desc="默认关闭。开启后由监控层按设定间隔读取列表并登记新作品；实际下载仍取决于流水线开关。"
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
            本人 sec_user_id（抖音网页版个人主页链接中的 sec_uid；读取点赞列表需要）
          </label>
          <input
            id="sec-user-id"
            value={secUserId}
            onChange={(e) => setSecUserId(e.target.value)}
            placeholder="例如 MS4wLjABAAAA…"
            className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
          />
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
