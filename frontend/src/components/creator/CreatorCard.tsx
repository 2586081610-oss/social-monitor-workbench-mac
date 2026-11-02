/** 博主卡片：头像占位 / 名称 / 分组 / 平台 / 监控状态 / 检测间隔编辑 / 失败警示 / 最近作品缩略+状态徽标 / 启停 / 立即爬取 / 删除。 */
import { Link } from 'react-router-dom';
import { AlertTriangle, Pause, Play, RefreshCw, Trash2, User } from 'lucide-react';
import { useState } from 'react';
import { deleteCreator, updateCreator } from '../../services/creators';
import { pollNow } from '../../services/monitor';
import { useMutation } from '../../hooks/useApi';
import PlatformBadge from '../common/PlatformBadge';
import ConfirmDialog from '../common/ConfirmDialog';
import { formatDateTime, formatRelativeTime } from '../../utils/format';
import { STATUS_LABEL } from '../../utils/constants';
import type { Creator, WorkSummary } from '../../types/api.d';

interface CreatorCardProps {
  creator: Creator;
  groupName?: string;
  recentWorks?: WorkSummary[];
  onChanged: () => void;
}

/** 检测间隔预设；value=-1 表示进入自定义输入。 */
const INTERVAL_PRESETS: { label: string; value: number | null }[] = [
  { label: '跟随平台', value: null },
  { label: '30分钟', value: 1_800 },
  { label: '1小时', value: 3_600 },
  { label: '6小时', value: 21_600 },
  { label: '12小时', value: 43_200 },
  { label: '24小时', value: 86_400 },
  { label: '自定义…', value: -1 },
];

/** 最近作品状态二分：有笔记=已完成（绿）；NOTE_GENERATING 无笔记=AI 笔记未完成（灰，不转圈）；其余按状态色。 */
function recentWorkMeta(w: WorkSummary): { label: string; className: string } {
  if (w.note_path) return { label: '已完成', className: 'bg-success-light text-success' };
  if (w.status === 'NOTE_GENERATING') return { label: 'AI 笔记未完成', className: 'bg-sidebar text-ink-muted' };
  if (w.status === 'DONE') return { label: '已完成', className: 'bg-success-light text-success' };
  if (w.status === 'FAILED') return { label: '失败', className: 'bg-danger-light text-danger' };
  return { label: STATUS_LABEL[w.status] ?? w.status, className: 'bg-info-light text-info' };
}

export default function CreatorCard({ creator, groupName, recentWorks = [], onChanged }: CreatorCardProps) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const toggleMutation = useMutation((enabled: boolean) =>
    updateCreator(creator.id, { enabled }),
  );
  const deleteMutation = useMutation((id: number) => deleteCreator(id));
  const [pollPending, setPollPending] = useState(false);
  const [pollTriggered, setPollTriggered] = useState(false);

  // 检测间隔：预设下拉 + 自定义分钟输入（保存即生效，next_poll_at 由后端按上次成功时间重排）
  const isCustomInterval =
    creator.poll_interval_seconds != null &&
    !INTERVAL_PRESETS.some((p) => p.value === creator.poll_interval_seconds);
  const [customMode, setCustomMode] = useState(isCustomInterval);
  const [customMinutes, setCustomMinutes] = useState(
    creator.poll_interval_seconds ? Math.max(1, Math.round(creator.poll_interval_seconds / 60)) : 60,
  );
  const [intervalSaving, setIntervalSaving] = useState(false);

  const saveInterval = async (seconds: number | null) => {
    setIntervalSaving(true);
    try {
      await updateCreator(creator.id, { poll_interval_seconds: seconds });
      onChanged();
    } catch {
      /* 错误由页面统一提示 */
    } finally {
      setIntervalSaving(false);
    }
  };

  const handleIntervalSelect = async (raw: string) => {
    if (raw === '-1') {
      setCustomMode(true);
      return;
    }
    setCustomMode(false);
    await saveInterval(raw === '' ? null : Number(raw));
  };

  const handleCustomSave = async () => {
    const minutes = Math.max(1, Math.round(Number(customMinutes) || 0));
    setCustomMinutes(minutes);
    await saveInterval(minutes * 60);
  };

  const handleToggle = async () => {
    try {
      await toggleMutation.run(!creator.enabled);
      onChanged();
    } catch {
      /* 错误由卡片顶部统一提示，此处静默 */
    }
  };

  const handlePoll = async () => {
    if (pollPending) return;
    setPollPending(true);
    try {
      await pollNow({ creator_id: creator.id });
      setPollTriggered(true);
      window.setTimeout(() => setPollTriggered(false), 2000);
      onChanged();
    } catch {
      /* 失败静默，可重试 */
    } finally {
      setPollPending(false);
    }
  };

  const handleDelete = async () => {
    try {
      await deleteMutation.run(creator.id);
      setConfirmOpen(false);
      onChanged();
    } catch {
      setConfirmOpen(false);
    }
  };

  return (
    <div className="flex flex-col rounded-lg border border-line bg-surface p-4 shadow-card transition-colors hover:border-primary/40">
      <div className="flex items-start gap-3">
        {/* 头像占位：平台首字 */}
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-sidebar text-sm font-semibold text-ink-secondary">
          {creator.name.slice(0, 1) || <User className="h-5 w-5" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Link to={`/creators/${creator.id}`} className="truncate-1 text-sm font-semibold text-ink hover:text-primary">
              {creator.name}
            </Link>
            <PlatformBadge platform={creator.platform} />
          </div>
          <p className="mt-0.5 truncate text-xs text-ink-muted">{groupName ?? '未分组'}</p>
        </div>
        <button
          type="button"
          onClick={handleToggle}
          disabled={toggleMutation.loading}
          className={`inline-flex items-center gap-1 rounded-full px-2 py-1 text-xs font-medium transition-colors disabled:opacity-60 ${
            creator.enabled
              ? 'bg-success-light text-success hover:bg-success hover:text-white'
              : 'bg-sidebar text-ink-muted hover:bg-danger hover:text-white'
          }`}
          title={creator.enabled ? '点击停用监控' : '点击启用监控'}
        >
          {creator.enabled ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />}
          {creator.enabled ? '监控中' : '已停用'}
        </button>
      </div>

      <dl className="mt-3 grid grid-cols-3 gap-2 text-xs text-ink-secondary">
        <div>
          <dt className="text-ink-muted">最近轮询</dt>
          <dd className="truncate-1">{formatRelativeTime(creator.last_poll_at)}</dd>
        </div>
        <div>
          <dt className="text-ink-muted">下次检测</dt>
          <dd className="truncate-1" title={creator.next_poll_at ?? undefined}>
            {creator.next_poll_at ? formatDateTime(creator.next_poll_at) : '待首轮检测'}
          </dd>
        </div>
        <div>
          <dt className="text-ink-muted">最近作品</dt>
          <dd className="truncate-1">{creator.last_work_at ? formatDateTime(creator.last_work_at) : '—'}</dd>
        </div>
      </dl>

      {/* 检测间隔编辑：预设下拉（含自定义分钟数） */}
      <div className="mt-3 flex items-center gap-2 text-xs">
        <span className="shrink-0 text-ink-muted">检测间隔</span>
        <select
          value={customMode ? '-1' : (creator.poll_interval_seconds ?? '')}
          onChange={(e) => void handleIntervalSelect(e.target.value)}
          disabled={intervalSaving}
          className="min-w-0 flex-1 rounded-md border border-line bg-surface px-2 py-1 text-xs text-ink transition-colors focus:border-primary disabled:opacity-60"
          title="该博主多久自动检测一次新作品；失败后统一 30 分钟重试"
        >
          {INTERVAL_PRESETS.map((p) => (
            <option key={p.label} value={p.value ?? ''}>
              {p.label}
            </option>
          ))}
        </select>
        {customMode && (
          <>
            <input
              type="number"
              min={1}
              value={customMinutes}
              onChange={(e) => setCustomMinutes(Number(e.target.value))}
              className="w-16 rounded-md border border-line bg-surface px-2 py-1 text-xs text-ink focus:border-primary"
              aria-label="自定义检测间隔（分钟）"
            />
            <span className="shrink-0 text-ink-muted">分钟</span>
            <button
              type="button"
              onClick={() => void handleCustomSave()}
              disabled={intervalSaving}
              className="shrink-0 rounded-md bg-primary px-2 py-1 text-xs font-medium text-white transition-colors hover:bg-primary-hover disabled:opacity-60"
            >
              保存
            </button>
          </>
        )}
      </div>

      {/* 连续检测失败警示（≥2 次） */}
      {(creator.poll_fail_streak ?? 0) >= 2 && (
        <div className="mt-3 flex items-start gap-2 rounded-md border border-danger/40 bg-danger-light px-3 py-2 text-xs text-danger">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div className="min-w-0">
            <p className="font-semibold">连续 {creator.poll_fail_streak} 次检测失败，每 30 分钟自动重试</p>
            {creator.last_poll_error && (
              <p className="mt-0.5 break-all text-[11px] opacity-80">{creator.last_poll_error}</p>
            )}
          </div>
        </div>
      )}

      {/* 最近作品缩略 + 状态徽标 */}
      <div className="mt-3 space-y-1.5">
        {recentWorks.length === 0 ? (
          <p className="text-xs text-ink-muted">暂无作品记录</p>
        ) : (
          recentWorks.slice(0, 3).map((w) => {
            const meta = recentWorkMeta(w);
            return (
              <Link key={w.id} to={`/works/${w.id}`} className="flex items-center gap-2 rounded-md px-1 py-0.5 transition-colors hover:bg-sidebar">
                <span className="h-8 w-12 shrink-0 rounded bg-sidebar" aria-hidden="true" />
                <span className="truncate-1 min-w-0 flex-1 text-xs text-ink-secondary">{w.title}</span>
                <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${meta.className}`}>{meta.label}</span>
              </Link>
            );
          })
        )}
      </div>

      <div className="mt-3 flex items-center justify-between gap-2 border-t border-line pt-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handlePoll}
            disabled={pollPending}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1.5 text-xs text-ink-secondary transition-colors hover:border-primary/40 hover:text-primary disabled:cursor-not-allowed disabled:opacity-50"
            title="立即爬取该博主最新作品"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${pollPending ? 'animate-spin' : ''}`} />
            {pollTriggered ? '已触发' : '立即爬取'}
          </button>
          <Link
            to={`/creators/${creator.id}`}
            className="rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-primary-hover"
          >
            查看作品
          </Link>
        </div>
        <button
          type="button"
          onClick={() => setConfirmOpen(true)}
          className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1.5 text-xs text-ink-muted transition-colors hover:border-danger/40 hover:text-danger"
        >
          <Trash2 className="h-3.5 w-3.5" />
          删除
        </button>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="删除博主"
        message={`确定删除博主「${creator.name}」？其作品记录将保留（creator 解除关联），监控任务一并移除。`}
        loading={deleteMutation.loading}
        onConfirm={handleDelete}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}
