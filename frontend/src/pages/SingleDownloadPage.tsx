import { useEffect, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { Download, ExternalLink, LoaderCircle } from 'lucide-react';
import { downloadSingleLink } from '../services/singleDownload';
import { getWork } from '../services/works';
import { PLATFORM_LABEL, RUNNING_STATUSES, STATUS_LABEL } from '../utils/constants';
import type { Work } from '../types/api.d';

function friendlyError(error: unknown): string {
  const message = error instanceof Error ? error.message : '';
  if (message.includes('LINK_IDENTITY_UNSUPPORTED')) return '暂不支持这个链接，请检查是否为作品分享链接。';
  if (message.includes('LINK_REDIRECT_FAILED')) return '分享短链接无法解析，请检查网络后重试。';
  if (message.includes('SINGLE_LINK_RUNNER_UNAVAILABLE')) return '下载程序未能启动，请检查后端服务。';
  return message || '操作失败，请稍后重试。';
}

export default function SingleDownloadPage() {
  const [input, setInput] = useState('');
  const [work, setWork] = useState<Work | null>(null);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!work || !RUNNING_STATUSES.has(work.status)) return;
    const workId = work.id;
    let cancelled = false;
    const timer = window.setInterval(() => {
      getWork(workId).then((next) => {
        if (!cancelled) setWork(next);
      }).catch(() => {
        if (!cancelled) setError('暂时无法获取下载状态，请检查后端服务。');
      });
    }, 3000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [work?.id, work?.status]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = input.match(/https?:\/\/[^\s，。；、）】]+/i)?.[0] ?? input.trim();
    if (!url) { setError('请先粘贴作品链接。'); return; }
    setSubmitting(true);
    setError('');
    setMessage('');
    setWork(null);
    try {
      const result = await downloadSingleLink(url);
      setWork(result.work);
      setMessage(result.message);
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setSubmitting(false);
    }
  }

  const running = work ? RUNNING_STATUSES.has(work.status) : false;
  const done = work?.status === 'DOWNLOADED' || work?.status === 'DONE';

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">链接下载</h1>
        <p className="mt-1 text-sm text-ink-muted">粘贴一条作品链接，下载到本地归档。</p>
      </div>
      <section className="rounded-lg border border-line bg-surface p-5 shadow-card">
        <form onSubmit={handleSubmit} className="space-y-3">
          <label htmlFor="single-download-url" className="block text-sm font-medium text-ink">作品链接</label>
          <textarea
            id="single-download-url"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            rows={3}
            maxLength={2000}
            placeholder="粘贴小红书、推特/X 或 B站作品链接，也支持分享文字"
            className="w-full rounded-md border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-primary"
          />
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-ink-muted">每次只下载当前链接；部分作品可能需要登录或受平台限制。</p>
            <button type="submit" disabled={submitting} className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50">
              {submitting ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
              {submitting ? '正在提交' : '开始下载'}
            </button>
          </div>
        </form>
      </section>
      {error ? <p role="alert" className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</p> : null}
      {work ? (
        <section aria-live="polite" className="space-y-3 rounded-lg border border-line bg-surface p-5 shadow-card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-sm font-semibold text-ink">{PLATFORM_LABEL[work.platform] ?? work.platform} · {STATUS_LABEL[work.status] ?? work.status}</p>
              <p className="mt-1 text-xs text-ink-muted">{work.title}</p>
            </div>
            {running ? <span className="text-xs text-ink-muted">正在更新状态…</span> : null}
          </div>
          {work.status === 'FAILED' ? (
            <p className="text-sm text-danger">下载失败{work.last_error ? `：${work.last_error}` : '，请检查链接及平台访问状态。'}</p>
          ) : <p className="text-sm text-ink-secondary">{done ? '下载完成，可查看本地文件。' : message}</p>}
          <div className="flex flex-wrap gap-3 text-sm">
            {done ? <Link to={`/works/${work.id}/local`} className="inline-flex items-center gap-1 text-primary hover:underline">查看本地文件 <ExternalLink className="h-3.5 w-3.5" /></Link> : null}
            <Link to={`/works/${work.id}`} className="text-primary hover:underline">作品详情</Link>
          </div>
        </section>
      ) : null}
    </div>
  );
}
