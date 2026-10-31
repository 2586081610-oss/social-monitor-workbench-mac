/** 单作品评论结果：完整性说明、置顶、全部主评论与回复、图片与语音状态、报告。 */
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Download,
  ExternalLink,
  FileText,
  Image as ImageIcon,
  LoaderCircle,
  MessageSquareText,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import { ErrorState, LoadingState } from '../components/common/StateViews';
import { ToneBadge } from '../components/common/StatusBadge';
import { useAsyncData } from '../hooks/useApi';
import { fetchCommentImage, fetchCommentReport, getCommentResult } from '../services/xhsComments';
import type {
  XhsCommentItem,
  XhsCommentPicture,
  XhsCommentResult,
  XhsCommentVoice,
} from '../types/api.d';

const COMPLETENESS = {
  complete: { tone: 'success' as const, label: '采集完整' },
  incomplete: { tone: 'warning' as const, label: '采集不完整' },
  failed: { tone: 'danger' as const, label: '采集失败' },
};

const VOICE_TEXT: Record<string, string> = {
  not_requested: '本次未提取语音转写',
  not_extracted: '未取得平台语音转写',
  failed: '语音转写提取失败',
};

const VOICE_PAGE_MODE: Record<string, string> = {
  reused: '本次复用已打开的作品页，未新开页面',
  opened: '本次新开了一个作品标签页，提取后已关闭',
  not_found: '未找到已打开的作品页，未提取语音转写',
  cdp_unavailable: '浏览器调试端口未就绪，未提取语音转写',
  fixture: '模拟数据，未访问浏览器',
  opened_failed: '本次新开作品标签页失败，未提取语音转写',
};

function displayTime(value: string | null): string {
  if (!value) return '时间未知';
  return value.replace('T', ' ').slice(0, 16);
}

function filenameOf(localPath: string | null): string {
  if (!localPath) return '';
  return localPath.split('/').pop() ?? '';
}

function CommentImage({ noteId, picture, alt }: { noteId: string; picture: XhsCommentPicture; alt: string }) {
  const [src, setSrc] = useState('');
  const [failed, setFailed] = useState(false);
  const filename = filenameOf(picture.local_path);

  useEffect(() => {
    if (!filename) return;
    let cancelled = false;
    let objectUrl = '';
    fetchCommentImage(noteId, filename)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [noteId, filename]);

  if (picture.status === 'downloaded' && src) {
    return (
      <a href={src} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-md border border-line">
        <img src={src} alt={alt} loading="lazy" className="max-h-48 w-full object-cover" />
      </a>
    );
  }
  if (picture.status === 'downloaded' && failed) {
    return <p className="rounded-md bg-danger-light px-2 py-1 text-xs text-danger">图片文件读取失败</p>;
  }
  const label =
    picture.status === 'failed'
      ? `下载失败（${picture.error ?? '未知原因'}）`
      : picture.status === 'skipped'
        ? '本次未请求下载'
        : '图片未下载';
  return (
    <div className="flex items-center gap-1.5 rounded-md bg-sidebar px-2 py-1 text-xs text-ink-muted">
      <ImageIcon className="h-3.5 w-3.5" />
      {label}
    </div>
  );
}

function VoiceBlock({ voice, contentStatus }: { voice: XhsCommentVoice; contentStatus: XhsCommentItem['content_status'] }) {
  if (voice.status === 'transcript_available' && voice.text) {
    return (
      <div className="rounded-md border border-info/30 bg-info-light px-3 py-2 text-sm text-ink-secondary">
        <p className="flex flex-wrap items-center gap-1.5 text-xs font-medium text-info">
          平台语音转写
          {voice.association === 'heuristic' ? <span className="rounded bg-warning-light px-1.5 py-0.5 text-[10px] text-warning">关联待核对</span> : null}
          {voice.carried_from_previous ? <span className="rounded bg-sidebar px-1.5 py-0.5 text-[10px] text-ink-muted">沿用上次结果</span> : null}
        </p>
        <p className="mt-1 whitespace-pre-wrap break-words">{voice.text}</p>
        {voice.evidence ? <p className="mt-1 text-[11px] text-ink-muted">依据：{voice.evidence}</p> : null}
      </div>
    );
  }
  if (contentStatus === 'empty' || voice.status === 'failed') {
    return (
      <p className="flex items-center gap-1.5 text-xs text-ink-muted">
        <AlertTriangle className="h-3.5 w-3.5" />
        {voice.status === 'failed' ? VOICE_TEXT.failed : '内容为空（可能为语音/表情/图片评论，未核实）；'}{voice.status !== 'failed' ? VOICE_TEXT[voice.status] ?? '' : ''}
      </p>
    );
  }
  return null;
}

function PictureGrid({ noteId, comment }: { noteId: string; comment: XhsCommentItem }) {
  if (!comment.pictures.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {comment.pictures.map((picture, index) => (
        <div key={`${picture.url}-${index}`} className="w-32">
          <CommentImage noteId={noteId} picture={picture} alt={`评论图片 ${index + 1}`} />
        </div>
      ))}
    </div>
  );
}

function replyDepth(reply: XhsCommentItem, mainId: string, byId: Map<string, XhsCommentItem>): number {
  let depth = 1;
  let current = reply.parent_comment_id;
  const seen = new Set<string>([reply.comment_id]);
  while (current && current !== mainId && byId.has(current) && !seen.has(current)) {
    seen.add(current);
    depth += 1;
    current = byId.get(current)?.parent_comment_id ?? '';
  }
  return depth;
}

function ReplyList({
  noteId,
  mainId,
  replies,
  byId,
}: {
  noteId: string;
  mainId: string;
  replies: XhsCommentItem[];
  byId: Map<string, XhsCommentItem>;
}) {
  if (!replies.length) return null;
  return (
    <div className="mt-3 space-y-2 border-l border-line pl-3">
      <p className="text-xs text-ink-muted">回复（{replies.length} 条，按点赞数降序）</p>
      {replies.map((reply) => (
        <div
          key={reply.comment_id}
          className="rounded-md bg-canvas px-3 py-2"
          style={{ marginLeft: `${Math.min(replyDepth(reply, mainId, byId) - 1, 3) * 10}px` }}
        >
          <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
            <span className="font-medium text-ink-secondary">{reply.nickname || '匿名用户'}</span>
            <span>{reply.like_count} 赞</span>
            <span>{displayTime(reply.created_at)}</span>
            {reply.in_latest === false ? <span className="rounded bg-warning-light px-1.5 py-0.5 text-[10px] text-warning">本次未返回</span> : null}
          </div>
          <p className="mt-1 whitespace-pre-wrap break-words text-sm text-ink-secondary">
            {reply.content || '（无文本内容）'}
          </p>
          <PictureGrid noteId={noteId} comment={reply} />
          <div className="mt-1"><VoiceBlock voice={reply.voice} contentStatus={reply.content_status} /></div>
        </div>
      ))}
    </div>
  );
}

function CommentThread({
  noteId,
  main,
  replies,
  byId,
  pinned,
}: {
  noteId: string;
  main: XhsCommentItem;
  replies: XhsCommentItem[];
  byId: Map<string, XhsCommentItem>;
  pinned?: boolean;
}) {
  return (
    <article className={`rounded-lg border bg-surface p-4 shadow-card ${pinned ? 'border-warning/40' : 'border-line'}`}>
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        <span className="text-sm font-semibold text-ink">{main.nickname || '匿名用户'}</span>
        <span className="inline-flex items-center gap-1"><CheckCircle2 className="h-3 w-3" />{main.like_count} 赞</span>
        <span>{displayTime(main.created_at)}</span>
        <span className="font-mono text-[10px]">ID {main.comment_id}</span>
        {pinned ? <ToneBadge tone={main.pinned.status === 'confirmed' ? 'success' : 'warning'} label={main.pinned.status === 'confirmed' ? '平台置顶' : '可能置顶'} /> : null}
        {main.in_latest === false ? <ToneBadge tone="warning" label="本次未返回" /> : null}
      </div>
      {pinned && main.pinned.evidence ? <p className="mt-1 text-[11px] text-ink-muted">依据：{main.pinned.evidence}</p> : null}
      <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-ink-secondary">
        {main.content || '（无文本内容）'}
      </p>
      <PictureGrid noteId={noteId} comment={main} />
      <div className="mt-2"><VoiceBlock voice={main.voice} contentStatus={main.content_status} /></div>
      <ReplyList noteId={noteId} mainId={main.comment_id} replies={replies} byId={byId} />
    </article>
  );
}

export default function CommentResultPage() {
  const { noteId = '' } = useParams<{ noteId: string }>();
  const view = useAsyncData(() => getCommentResult(noteId), [noteId], Boolean(noteId));
  const [reportBusy, setReportBusy] = useState(false);
  const [reportError, setReportError] = useState('');

  async function handleReport(download: boolean) {
    setReportBusy(true);
    setReportError('');
    try {
      const blob = await fetchCommentReport(noteId);
      const url = URL.createObjectURL(blob);
      if (download) {
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `xhs_comments_${noteId}.md`;
        anchor.click();
      } else {
        window.open(url, '_blank', 'noopener');
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch {
      setReportError('报告读取失败，请稍后重试。');
    } finally {
      setReportBusy(false);
    }
  }

  const result = view.data as XhsCommentResult | null;
  const comments = result?.comments ?? [];
  const byId = new Map(comments.map((comment) => [comment.comment_id, comment]));
  const pinned = result?.pinned_comment_id ? comments.find((comment) => comment.comment_id === result.pinned_comment_id) : undefined;
  const mains = comments.filter((comment) => !comment.parent_comment_id);
  const replies = comments.filter((comment) => comment.parent_comment_id);

  function rootId(comment: XhsCommentItem): string {
    let current = comment;
    const seen = new Set<string>([comment.comment_id]);
    while (current.parent_comment_id && byId.has(current.parent_comment_id) && !seen.has(current.parent_comment_id)) {
      seen.add(current.parent_comment_id);
      current = byId.get(current.parent_comment_id)!;
    }
    return current.comment_id;
  }

  const mainIds = new Set(mains.map((main) => main.comment_id));
  const repliesFor = (mainId: string) => replies.filter((reply) => rootId(reply) === mainId);
  const orphanReplies = replies.filter((reply) => !mainIds.has(rootId(reply)));
  const pinnedReplies = pinned ? repliesFor(pinned.comment_id) : [];
  const orderedMains = mains.filter((main) => main.comment_id !== pinned?.comment_id);

  const completeness = result?.completeness;
  const statusMeta = completeness ? COMPLETENESS[completeness.status] ?? COMPLETENESS.incomplete : null;
  const voiceRun = result?.voice_run;
  const voicePageModeText = voiceRun?.requested && voiceRun.page_mode ? VOICE_PAGE_MODE[voiceRun.page_mode] ?? null : null;
  const voiceFailureReason =
    voiceRun && voiceRun.requested && (voiceRun.status === 'not_extracted' || voiceRun.status === 'failed')
      ? voiceRun.failure_reason ?? null
      : null;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link to="/comments" className="mb-1 inline-flex items-center gap-1 text-xs text-ink-muted transition-colors hover:text-primary">
            <ArrowLeft className="h-3.5 w-3.5" />
            返回评论采集
          </Link>
          <h1 className="text-lg font-semibold text-ink">{result?.note_meta?.title || `作品 ${noteId}`}</h1>
          {result ? (
            <p className="mt-1 text-xs text-ink-muted">
              采集于 {displayTime(result.collected_at)}（北京时间）
              {result.collector.mode === 'fixture' ? ' · 模拟数据（未访问真实平台）' : ' · 真实平台采集'}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => view.refetch()}
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-ink-secondary transition-colors hover:bg-sidebar"
          >
            <RefreshCw className="h-4 w-4" /> 刷新
          </button>
          <button
            type="button"
            onClick={() => handleReport(false)}
            disabled={reportBusy}
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-ink-secondary transition-colors hover:bg-sidebar disabled:opacity-50"
          >
            {reportBusy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />} 查看报告
          </button>
          <button
            type="button"
            onClick={() => handleReport(true)}
            disabled={reportBusy}
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-ink-secondary transition-colors hover:bg-sidebar disabled:opacity-50"
          >
            <Download className="h-4 w-4" /> 下载报告
          </button>
        </div>
      </div>

      {reportError ? <p className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{reportError}</p> : null}

      {view.loading ? (
        <LoadingState label="加载评论结果…" />
      ) : view.error ? (
        <ErrorState message={view.error.message.includes('COMMENTS_NOT_COLLECTED') || view.error.message.includes('NOT_FOUND') ? '该作品还没有保存的评论结果。' : view.error.message} onRetry={view.refetch} />
      ) : result && completeness && statusMeta ? (
        <>
          <section className={`rounded-lg border p-4 shadow-card ${completeness.status === 'complete' ? 'border-success/30 bg-success-light' : completeness.status === 'incomplete' ? 'border-warning/30 bg-warning-light' : 'border-danger/30 bg-danger-light'}`}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                {completeness.status === 'complete' ? <CheckCircle2 className="h-5 w-5 text-success" /> : completeness.status === 'incomplete' ? <AlertTriangle className="h-5 w-5 text-warning" /> : <XCircle className="h-5 w-5 text-danger" />}
                <p className="text-sm font-semibold text-ink">{statusMeta.label}</p>
              </div>
              <ToneBadge tone={statusMeta.tone} label={statusMeta.label} />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
              <p className="text-ink-secondary">评论 <strong>{result.counts.total_comments}</strong>（主 {result.counts.main_comments} / 回复 {result.counts.replies}）</p>
              <p className="text-ink-secondary">分页 {completeness.pages_fetched} 页，结束：{completeness.page_end_reached ? '是' : '否'}</p>
              <p className="text-ink-secondary">平台总数：{completeness.platform_total ?? '未返回'}</p>
              <p className="text-ink-secondary">图片 {result.counts.images_downloaded}/{result.counts.images_total}，失败 {result.counts.images_failed}</p>
            </div>
            {completeness.reason_texts?.length ? (
              <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-warning">
                {completeness.reason_texts.map((text) => <li key={text}>{text}</li>)}
              </ul>
            ) : null}
            {completeness.warning_texts?.length || completeness.warnings?.length ? (
              <p className="mt-2 text-xs text-ink-muted">
                提示：{(completeness.warning_texts?.length ? completeness.warning_texts : completeness.warnings).join('；')}
              </p>
            ) : null}
            {result.completeness.reply_shortfall?.length ? (
              <p className="mt-2 text-xs text-ink-muted">有 {result.completeness.reply_shortfall.length} 条主评论的平台回复数多于本次已关联数，已标记为不完整。</p>
            ) : null}
            <p className="mt-3 break-all text-xs text-ink-muted">
              来源：<a href={result.source_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">{result.source_url}</a>
            </p>
          </section>

          {voiceRun?.requested ? (
            <section className="rounded-lg border border-line bg-surface p-4 text-xs leading-5 text-ink-muted shadow-card">
              <p className="flex items-center gap-1.5 font-medium text-ink-secondary">
                <MessageSquareText className="h-4 w-4" />语音转写
              </p>
              {voicePageModeText ? <p className="mt-1">{voicePageModeText}</p> : null}
              <p className="mt-1">本次取得 {voiceRun.extracted} 条，已关联 {voiceRun.associated} 条。</p>
              {voiceFailureReason ? (
                <p className="mt-2 flex items-start gap-1.5 rounded-md bg-warning-light px-3 py-2 text-warning">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>未提取原因：{voiceFailureReason}</span>
                </p>
              ) : null}
            </section>
          ) : null}

          {pinned ? (
            <section className="space-y-2">
              <h2 className="text-sm font-semibold text-ink">置顶评论</h2>
              <CommentThread
                noteId={noteId}
                main={pinned}
                replies={pinnedReplies}
                byId={byId}
                pinned
              />
            </section>
          ) : null}

          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold text-ink">全部主评论（按点赞数降序）</h2>
              <span className="text-xs text-ink-muted">共 {orderedMains.length} 条{completeness.status !== 'complete' ? '（当前结果不完整）' : ''}</span>
            </div>
            {orderedMains.length ? (
              orderedMains.map((main) => (
                <CommentThread
                  key={main.comment_id}
                  noteId={noteId}
                  main={main}
                  replies={repliesFor(main.comment_id)}
                  byId={byId}
                />
              ))
            ) : (
              <p className="rounded-lg border border-line bg-surface px-4 py-6 text-sm text-ink-muted">本次没有采集到主评论。</p>
            )}
          </section>

          {orphanReplies.length ? (
            <section className="space-y-2">
              <h2 className="text-sm font-semibold text-ink">未关联到主评论的回复</h2>
              <div className="rounded-lg border border-line bg-surface p-4 shadow-card">
                {orphanReplies.map((reply) => (
                  <div key={reply.comment_id} className="border-b border-line py-2 last:border-0">
                    <p className="text-xs text-ink-muted">{reply.nickname || '匿名用户'} · {reply.like_count} 赞 · {displayTime(reply.created_at)}</p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm text-ink-secondary">{reply.content || '（无文本内容）'}</p>
                  </div>
                ))}
              </div>
            </section>
          ) : null}

          {result.voice_run?.unassociated?.length ? (
            <section className="rounded-lg border border-warning/30 bg-warning-light p-4">
              <h2 className="flex items-center gap-1.5 text-sm font-semibold text-warning"><AlertTriangle className="h-4 w-4" />未关联的语音转写</h2>
              <ul className="mt-2 space-y-1 text-xs leading-5 text-ink-secondary">
                {result.voice_run.unassociated.map((row, index) => (
                  <li key={`${row.text}-${index}`}>{row.nickname || '未知用户'}：{row.text}</li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="rounded-lg border border-line bg-surface p-4 text-xs leading-5 text-ink-muted">
            <p className="flex items-center gap-1.5 font-medium text-ink-secondary"><MessageSquareText className="h-4 w-4" />说明</p>
            <p className="mt-1">评论图片仅保存原图，AI 图片内容识别未接入（状态为未分析），不会编造图片文字。</p>
            <p>语音仅使用平台网页已有转写；未取得或无法可靠关联的项目按状态保留，未使用本地转写。</p>
            {result.note_meta?.author ? <p>作品作者：{result.note_meta.author}</p> : null}
            <p className="mt-1">
              <Link to="/comments" className="inline-flex items-center gap-1 text-primary hover:underline">采集另一条作品 <ExternalLink className="h-3 w-3" /></Link>
            </p>
          </section>
        </>
      ) : null}
    </div>
  );
}
