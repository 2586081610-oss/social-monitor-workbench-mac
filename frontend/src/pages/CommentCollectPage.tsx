/** 小红书评论采集入口：提交一条作品链接，显示进度、结果入口与可重试状态。 */
import { useEffect, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, ExternalLink, LoaderCircle, MessageSquareText, RefreshCw } from 'lucide-react';
import { EmptyState, LoadingState } from '../components/common/StateViews';
import { ToneBadge } from '../components/common/StatusBadge';
import { useAsyncData } from '../hooks/useApi';
import { createCommentJob, getCommentJob, listCommentNotes, retryCommentJob } from '../services/xhsComments';
import type { XhsCommentJob, XhsCommentJobStatus, XhsCommentNoteSummary } from '../types/api.d';

const PHASE_LABEL: Record<string, string> = {
  queued: '排队中',
  starting: '准备采集',
  fetching_note: '获取作品信息',
  fetching_comments: '翻页采集主评论',
  fetching_replies: '采集回复',
  downloading_images: '下载评论图片',
  extracting_voice: '提取语音转写',
  writing_result: '写入结果',
  completed: '已完成',
  failed: '失败',
};

const JOB_STATUS: Record<XhsCommentJobStatus, { tone: 'neutral' | 'info' | 'success' | 'warning' | 'danger'; label: string }> = {
  queued: { tone: 'info', label: '排队中' },
  running: { tone: 'info', label: '采集中' },
  completed: { tone: 'success', label: '完成' },
  incomplete: { tone: 'warning', label: '不完整' },
  failed: { tone: 'danger', label: '失败' },
};

const COMPLETENESS = {
  complete: { tone: 'success' as const, label: '完整' },
  incomplete: { tone: 'warning' as const, label: '不完整' },
  failed: { tone: 'danger' as const, label: '失败' },
};

const VOICE_PAGE_MODE: Record<string, string> = {
  reused: '复用已打开页',
  opened: '已新开标签页',
  not_found: '未找到已打开页',
  cdp_unavailable: '浏览器调试端口未就绪',
  fixture: '模拟数据',
  opened_failed: '新开标签页失败',
};

function friendlyError(error: unknown): string {
  const message = error instanceof Error ? error.message : '';
  if (message.includes('LINK_IDENTITY_UNSUPPORTED') || message.includes('XHS_NOTE_REQUIRED')) {
    return '只支持小红书作品链接，请检查后重试。';
  }
  if (message.includes('LINK_REDIRECT_FAILED')) return '分享短链接无法解析，请检查网络后重试。';
  if (message.includes('VOICE_AUTHORIZATION_REQUIRED')) return '提取语音转写需要先勾选本次操作授权。';
  if (message.includes('COMMENT_JOB_ALREADY_RUNNING')) return '该作品已有采集任务在进行中。';
  return message || '操作失败，请稍后重试。';
}

function displayTime(value: string | null): string {
  if (!value) return '时间未知';
  return value.replace('T', ' ').slice(0, 16);
}

function NoteRow({ note }: { note: XhsCommentNoteSummary }) {
  const completeness = COMPLETENESS[note.completeness_status] ?? { tone: 'neutral' as const, label: note.completeness_status };
  return (
    <Link
      to={`/comments/${note.note_id}`}
      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line px-3 py-2 transition-colors hover:bg-sidebar"
    >
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-ink">{note.title || `作品 ${note.note_id}`}</p>
        <p className="mt-0.5 text-xs text-ink-muted">
          {displayTime(note.collected_at)} · 评论 {note.counts?.total_comments ?? 0}（主 {note.counts?.main_comments ?? 0} / 回复 {note.counts?.replies ?? 0}）
          {note.mode === 'fixture' ? ' · 模拟数据' : ''}
        </p>
      </div>
      <ToneBadge tone={completeness.tone} label={completeness.label} />
    </Link>
  );
}

export default function CommentCollectPage() {
  const [input, setInput] = useState('');
  const [includeImages, setIncludeImages] = useState(true);
  const [includeVoice, setIncludeVoice] = useState(false);
  const [voiceAuthorized, setVoiceAuthorized] = useState(false);
  const [allowOpenPage, setAllowOpenPage] = useState(false);
  const [job, setJob] = useState<XhsCommentJob | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const notes = useAsyncData(() => listCommentNotes(), []);

  const running = job ? job.status === 'queued' || job.status === 'running' : false;

  useEffect(() => {
    if (!job || !running) return;
    const jobId = job.job_id;
    let cancelled = false;
    const timer = window.setInterval(() => {
      getCommentJob(jobId)
        .then((next) => {
          if (!cancelled) setJob(next);
        })
        .catch(() => {
          if (!cancelled) setError('暂时无法获取采集进度，请检查后端服务。');
        });
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [job?.job_id, running]);

  useEffect(() => {
    if (job && !running) notes.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.status]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = input.match(/https?:\/\/[^\s，。；、）】]+/i)?.[0] ?? input.trim();
    if (!url) {
      setError('请先粘贴小红书作品链接。');
      return;
    }
    if (includeVoice && !voiceAuthorized) {
      setError('提取语音转写需要先勾选本次操作授权。');
      return;
    }
    if (allowOpenPage && !voiceAuthorized) {
      setError('允许新开页面前需要先勾选本次操作授权。');
      return;
    }
    setSubmitting(true);
    setError('');
    setJob(null);
    try {
      const created = await createCommentJob({
        url,
        include_images: includeImages,
        include_voice: includeVoice,
        voice_authorized: voiceAuthorized,
        voice_allow_open_page: allowOpenPage,
      });
      setJob(created);
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRetry() {
    if (!job) return;
    if (job.include_voice && !voiceAuthorized) {
      setError('该任务包含语音转写；重新采集前请勾选本次操作授权，或关闭语音提取后重新提交。');
      return;
    }
    setRetrying(true);
    setError('');
    try {
      setJob(await retryCommentJob(job.job_id, voiceAuthorized, allowOpenPage));
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setRetrying(false);
    }
  }

  const statusMeta = job ? JOB_STATUS[job.status] : null;
  const progress = job?.progress ?? {};
  const terminal = job ? !running : false;
  const canView = job ? job.result_available && (job.status === 'completed' || job.status === 'incomplete') : false;

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">小红书评论</h1>
        <p className="mt-1 text-sm text-ink-muted">粘贴一条小红书作品链接，采集该作品的评论与回复，按点赞数查看并保存本地结果。</p>
      </div>

      <section className="rounded-lg border border-line bg-surface p-5 shadow-card">
        <form onSubmit={handleSubmit} className="space-y-3">
          <label htmlFor="xhs-comment-url" className="block text-sm font-medium text-ink">作品链接</label>
          <textarea
            id="xhs-comment-url"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            rows={3}
            maxLength={2000}
            placeholder="粘贴小红书作品链接或分享文字"
            className="w-full rounded-md border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-primary"
          />
          <div className="space-y-2 text-sm text-ink-secondary">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={includeImages} onChange={(event) => setIncludeImages(event.target.checked)} />
              同时下载评论图片（保存到该作品的评论目录）
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={includeVoice}
                onChange={(event) => {
                  setIncludeVoice(event.target.checked);
                  if (!event.target.checked) {
                    setVoiceAuthorized(false);
                    setAllowOpenPage(false);
                  }
                }}
              />
              同时提取平台已有的语音转写
            </label>
            {includeVoice ? (
              <div className="rounded-md border border-warning/30 bg-warning-light px-3 py-2 text-xs leading-5 text-warning">
                <p className="flex items-center gap-1 font-medium">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  语音转写会打开该作品页面并使用已登录会话，可能产生浏览记录
                </p>
                <label className="mt-1 flex items-center gap-2 text-warning">
                  <input
                    type="checkbox"
                    checked={voiceAuthorized}
                    onChange={(event) => {
                      setVoiceAuthorized(event.target.checked);
                      if (!event.target.checked) setAllowOpenPage(false);
                    }}
                  />
                  我授权本次打开作品页提取转写（仅这一次）
                </label>
                <label className={`mt-1 flex items-start gap-2 text-warning ${voiceAuthorized ? '' : 'opacity-60'}`}>
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={allowOpenPage}
                    disabled={!voiceAuthorized}
                    onChange={(event) => setAllowOpenPage(event.target.checked)}
                  />
                  <span>如果未找到已打开的作品页，允许新开一个标签页（会新开作品页，可能产生浏览记录；提取后关闭该页）</span>
                </label>
              </div>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-ink-muted">每次只采集当前链接；登录失效、平台限制或分页中断时会明确标记为不完整。</p>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
            >
              {submitting ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <MessageSquareText className="h-4 w-4" />}
              {submitting ? '正在提交' : '开始采集评论'}
            </button>
          </div>
        </form>
      </section>

      {error ? <p role="alert" className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</p> : null}

      {job && statusMeta ? (
        <section aria-live="polite" className="space-y-3 rounded-lg border border-line bg-surface p-5 shadow-card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-sm font-semibold text-ink">
                作品 {job.note_id}
                {job.mode === 'fixture' ? <span className="ml-2 rounded bg-warning-light px-1.5 py-0.5 text-[11px] font-normal text-warning">模拟数据（未访问真实平台）</span> : null}
              </p>
              <p className="mt-1 text-xs text-ink-muted">{PHASE_LABEL[job.phase] ?? job.phase} · {displayTime(job.updated_at)}</p>
            </div>
            <div className="flex items-center gap-2">
              <ToneBadge tone={statusMeta.tone} label={statusMeta.label} />
              {running ? <LoaderCircle className="h-4 w-4 animate-spin text-primary" /> : null}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
            <div className="rounded-md bg-sidebar px-3 py-2">
              <p className="text-ink-muted">已获取页数</p>
              <p className="mt-1 text-sm font-medium text-ink">{progress.pages_fetched ?? 0}</p>
            </div>
            <div className="rounded-md bg-sidebar px-3 py-2">
              <p className="text-ink-muted">主评论</p>
              <p className="mt-1 text-sm font-medium text-ink">{progress.main_comments ?? 0}</p>
            </div>
            <div className="rounded-md bg-sidebar px-3 py-2">
              <p className="text-ink-muted">回复</p>
              <p className="mt-1 text-sm font-medium text-ink">{progress.replies_fetched ?? 0}</p>
            </div>
            <div className="rounded-md bg-sidebar px-3 py-2">
              <p className="text-ink-muted">图片（成功 / 失败）</p>
              <p className="mt-1 text-sm font-medium text-ink">{progress.images_done ?? 0} / {progress.images_failed ?? 0}</p>
            </div>
          </div>

          {progress.voice_page_mode !== undefined || progress.voice_rows !== undefined ? (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-muted">
              {progress.voice_page_mode ? (
                <span>语音页面：{VOICE_PAGE_MODE[progress.voice_page_mode] ?? progress.voice_page_mode}</span>
              ) : null}
              {progress.voice_rows !== undefined ? <span>语音转写：{progress.voice_rows} 条</span> : null}
              {progress.voice_associated !== undefined ? <span>已关联：{progress.voice_associated} 条</span> : null}
            </div>
          ) : null}

          {job.status === 'failed' && job.error ? <p className="text-sm text-danger">{job.error}</p> : null}
          {job.status === 'incomplete' ? (
            <p className="text-sm text-warning">采集已结束，但结果不完整；请进入结果页查看具体原因，可重新采集补全。</p>
          ) : null}
          {job.status === 'completed' ? <p className="text-sm text-success">采集完成，可查看完整评论列表与报告。</p> : null}

          <div className="flex flex-wrap items-center gap-3 text-sm">
            {canView ? (
              <Link to={`/comments/${job.note_id}`} className="inline-flex items-center gap-1 text-primary hover:underline">
                查看完整结果 <ExternalLink className="h-3.5 w-3.5" />
              </Link>
            ) : null}
            {terminal && (job.status === 'failed' || job.status === 'incomplete') ? (
              <button
                type="button"
                onClick={handleRetry}
                disabled={retrying}
                className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-sm text-ink-secondary transition-colors hover:bg-sidebar disabled:opacity-50"
              >
                {retrying ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                重新采集
              </button>
            ) : null}
            <Link to="/download" className="text-xs text-ink-muted hover:text-primary">作品下载入口</Link>
          </div>
        </section>
      ) : null}

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-ink">已保存的评论结果</h2>
          <button type="button" onClick={() => notes.refetch()} className="inline-flex items-center gap-1 text-xs text-ink-muted hover:text-primary">
            <RefreshCw className="h-3.5 w-3.5" /> 刷新
          </button>
        </div>
        {notes.loading ? (
          <LoadingState label="加载已保存结果…" />
        ) : notes.data && notes.data.items.length ? (
          <div className="space-y-2">
            {notes.data.items.map((note) => <NoteRow key={note.note_id} note={note} />)}
          </div>
        ) : (
          <EmptyState title="还没有评论结果" hint="提交一条小红书作品链接后，结果会保存在本地，刷新页面仍可打开。" />
        )}
      </section>
    </div>
  );
}
