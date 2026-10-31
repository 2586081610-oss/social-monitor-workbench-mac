/** 作品本地视图：原文件链接 + 本地原文件（预览/列表）+ AI 总结（笔记全文渲染）。 */
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, ExternalLink, FileText, Film, FolderOpen, Image as ImageIcon, RefreshCw } from 'lucide-react';
import { ErrorState, LoadingState } from '../components/common/StateViews';
import { useAsyncData } from '../hooks/useApi';
import { getWorkLocalView } from '../services/works';
import { listSummaryTargets, setDefaultSummaryTarget } from '../services/summaryTargets';
import type { SummaryTargetsData } from '../services/summaryTargets';
import { formatBytes } from '../utils/format';
import type { LocalMediaFile } from '../types/api.d';

const IMAGE_EXT = /\.(jpe?g|png|webp|gif|avif)$/i;
const VIDEO_EXT = /\.(mp4|webm|mov|mkv|m4v)$/i;

function isImage(f: LocalMediaFile) {
  return IMAGE_EXT.test(f.name);
}
function isVideo(f: LocalMediaFile) {
  return VIDEO_EXT.test(f.name);
}

/** 简易 markdown 渲染（标题/图片/列表/表格/段落/粗体），供笔记全文展示 */
function SimpleMarkdown({ text }: { text: string }) {
  const lines = text.split('\n');
  const blocks: React.ReactNode[] = [];
  let table: string[] = [];
  let list: string[] = [];
  let key = 0;

  const flush = () => {
    if (list.length) {
      blocks.push(
        <ul key={`ul-${key++}`} className="my-2 list-disc space-y-1 pl-5">
          {list.map((li, i) => (
            <li key={i}>{renderInline(li.replace(/^[-*]\s*/, ''))}</li>
          ))}
        </ul>,
      );
      list = [];
    }
    if (table.length) {
      blocks.push(renderTable(table));
      table = [];
    }
  };

  function renderTable(rows: string[]) {
    const parseRow = (r: string) => r.split('|').slice(1, -1).map((c) => c.trim());
    const head = parseRow(rows[0]);
    const body = rows.slice(2).map(parseRow);
    return (
      <table key={`t-${key++}`} className="my-3 w-full border-collapse text-sm">
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} className="border border-line bg-sidebar px-2 py-1 text-left font-medium text-ink">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, i) => (
            <tr key={i}>
              {row.map((c, j) => (
                <td key={j} className="border border-line px-2 py-1 text-ink-secondary">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  function renderInline(s: string) {
    const img = s.match(/!\[([^\]]*)\]\(([^)]+)\)/);
    if (img) {
      return <img src={img[2]} alt={img[1]} className="my-2 max-w-full rounded-lg" loading="lazy" />;
    }
    const link = s.match(/\[([^\]]+)\]\(([^)]+)\)/);
    if (link) {
      return (
        <a href={link[2]} target="_blank" rel="noreferrer" className="text-primary hover:underline">
          {link[1]}
        </a>
      );
    }
    const parts = s.split(/(\*\*[^*]+\*\*)/g);
    return parts.map((p, i) => (p.startsWith('**') ? <strong key={i}>{p.slice(2, -2)}</strong> : p));
  }

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      flush();
      continue;
    }
    if (line.startsWith('|')) {
      if (!table.length) table = [];
      table.push(line);
      continue;
    }
    flush();
    if (/^###\s/.test(line)) blocks.push(<h3 key={key++} className="mt-4 text-base font-semibold text-ink">{line.replace(/^###\s*/, '')}</h3>);
    else if (/^##\s/.test(line)) blocks.push(<h2 key={key++} className="mt-5 text-lg font-semibold text-ink">{line.replace(/^##\s*/, '')}</h2>);
    else if (/^#\s/.test(line)) blocks.push(<h1 key={key++} className="mt-6 text-xl font-semibold text-ink">{line.replace(/^#\s*/, '')}</h1>);
    else if (/^[-*]\s/.test(line)) list.push(line);
    else if (/^!\[/.test(line)) blocks.push(<div key={key++}>{renderInline(line)}</div>);
    else if (/^---+\s*$/.test(line)) blocks.push(<hr key={key++} className="my-3 border-line" />);
    else blocks.push(<p key={key++} className="my-1.5 leading-relaxed text-ink-secondary">{renderInline(line)}</p>);
  }
  flush();
  return <div className="space-y-0.5">{blocks}</div>;
}

/** 文件查看类型：转写按字幕渲染，CSV 按表格，MD 按排版，JSON 格式化，其余原样。 */
type ViewerKind = 'transcript' | 'csv' | 'md' | 'json' | 'text';

function viewerKindOf(f: LocalMediaFile): ViewerKind {
  if (f.label === '语音转写') return 'transcript';
  const dot = f.name.lastIndexOf('.');
  const suffix = dot >= 0 ? f.name.slice(dot).toLowerCase() : '';
  if (suffix === '.csv') return 'csv';
  if (suffix === '.md') return 'md';
  if (suffix === '.json') return 'json';
  return 'text';
}

function formatSeconds(s: number): string {
  const total = Math.max(0, Math.floor(s));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

/** 极简 CSV 解析：处理引号包裹与字段内换行（作品标题含 # 标签换行的实际场景）。 */
function parseCsv(text: string): string[][] {
  const body = text.replace(/^\uFEFF/, '');
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let inQuotes = false;
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (inQuotes) {
      if (ch === '"') {
        if (body[i + 1] === '"') {
          cell += '"';
          i++;
        } else inQuotes = false;
      } else cell += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\n') {
      row.push(cell.replace(/\r$/, ''));
      rows.push(row);
      row = [];
      cell = '';
    } else cell += ch;
  }
  if (cell !== '' || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter((r) => r.some((c) => c.trim() !== ''));
}

/** 本地文件网页内查看弹窗：取文件内容并按类型渲染；原始文件仍可另开标签。 */
function FileViewerModal({ file, onClose }: { file: LocalMediaFile; onClose: () => void }) {
  const [state, setState] = useState<{ loading: boolean; error: string | null; text: string | null }>({
    loading: true,
    error: null,
    text: null,
  });

  useEffect(() => {
    let alive = true;
    setState({ loading: true, error: null, text: null });
    fetch(file.url)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((t) => {
        if (alive) setState({ loading: false, error: null, text: t });
      })
      .catch((e: unknown) => {
        if (alive) setState({ loading: false, error: e instanceof Error ? e.message : String(e), text: null });
      });
    return () => {
      alive = false;
    };
  }, [file.url]);

  const kind = viewerKindOf(file);
  let body: React.ReactNode = null;
  if (state.loading) {
    body = <p className="text-sm text-ink-muted">加载中…</p>;
  } else if (state.error || state.text === null) {
    body = <p className="text-sm text-red-600">读取失败：{state.error ?? '未知错误'}</p>;
  } else if (kind === 'transcript') {
    let rendered: React.ReactNode = null;
    try {
      const parsed = JSON.parse(state.text) as {
        engine?: string;
        created_at?: string;
        segments?: { start: number; end: number; text: string }[];
      };
      if (Array.isArray(parsed.segments)) {
        rendered = (
          <div className="space-y-1">
            <p className="mb-3 text-xs text-ink-muted">
              转写引擎：{parsed.engine ?? '未知'} · 共 {parsed.segments.length} 句
            </p>
            {parsed.segments.map((seg, i) => (
              <div key={i} className="flex gap-3 text-sm">
                <span className="shrink-0 font-mono text-xs text-ink-muted">
                  {formatSeconds(seg.start)} – {formatSeconds(seg.end)}
                </span>
                <span className="text-ink">{seg.text}</span>
              </div>
            ))}
          </div>
        );
      }
    } catch {
      rendered = null;
    }
    body = rendered ?? <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-xs text-ink-secondary">{state.text}</pre>;
  } else if (kind === 'csv') {
    const rows = parseCsv(state.text);
    const header = rows[0] ?? [];
    body = (
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="bg-sidebar">
            {header.map((h, i) => (
              <th key={i} className="border border-line px-2 py-1 text-left font-medium text-ink">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(1).map((r, ri) => (
            <tr key={ri}>
              {header.map((_, ci) => (
                <td key={ci} className="border border-line px-2 py-1 align-top text-ink-secondary">
                  {r[ci] ?? ''}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    );
  } else if (kind === 'md') {
    body = <SimpleMarkdown text={state.text} />;
  } else if (kind === 'json') {
    let pretty = state.text;
    try {
      pretty = JSON.stringify(JSON.parse(state.text), null, 2);
    } catch {
      /* 非 JSON 原样显示 */
    }
    body = <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-xs text-ink-secondary">{pretty}</pre>;
  } else {
    body = <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-xs text-ink-secondary">{state.text}</pre>;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-lg border border-line bg-surface shadow-card"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <span className="whitespace-nowrap rounded bg-sidebar px-1.5 py-0.5 text-xs text-ink-secondary">{file.label ?? '其他'}</span>
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-secondary">{file.name}</span>
          <a href={file.url} target="_blank" rel="noreferrer" title="在新标签打开原文件" className="shrink-0 text-ink-muted hover:text-primary">
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
          <button type="button" onClick={onClose} className="shrink-0 text-ink-muted hover:text-primary" aria-label="关闭">
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4">{body}</div>
      </div>
    </div>
  );
}

/** 总结交付目标设置：默认工具可改，素材就绪清单投递到默认目标目录。 */
function SummaryTargetCard() {
  const [data, setData] = useState<SummaryTargetsData | null>(null);
  const [selected, setSelected] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    listSummaryTargets()
      .then((d) => {
        if (!alive) return;
        setData(d);
        setSelected(d.default_target);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  const save = async () => {
    if (!selected) return;
    setSaving(true);
    setMessage(null);
    try {
      const d = await setDefaultSummaryTarget(selected);
      setData(d);
      setMessage(`默认总结工具已设为 ${d.default_target}，后续素材就绪清单将投递到该目标目录`);
    } catch {
      setMessage('保存失败，请稍后重试');
    } finally {
      setSaving(false);
    }
  };

  if (failed) {
    return (
      <section className="rounded-lg border border-line bg-surface p-4 shadow-card">
        <h2 className="mb-2 text-sm font-semibold text-ink">总结交付目标</h2>
        <p className="text-sm text-ink-muted">目标配置读取失败（默认仍为 workbuddy）</p>
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-line bg-surface p-4 shadow-card">
      <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-ink">
        <FileText className="h-4 w-4" />
        总结交付目标
      </h2>
      <p className="mb-3 text-xs text-ink-muted">
        素材就绪清单（含转写/抽帧/来源信息）投递到默认工具的目录，由该工具撰写总结；完成后回执自动留痕。
      </p>
      <div className="flex items-center gap-3">
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="rounded border border-line bg-background px-2 py-1.5 text-sm text-ink"
          aria-label="默认总结工具"
        >
          {data?.targets.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}
              {t.kind === 'workbuddy' ? '（Obsidian 笔记层）' : ''}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={save}
          disabled={saving || !selected || selected === data?.default_target}
          className="rounded border border-line px-3 py-1.5 text-sm text-ink disabled:opacity-50"
        >
          {saving ? '保存中…' : '设为默认工具'}
        </button>
      </div>
      {message ? <p className="mt-2 text-xs text-ink-muted">{message}</p> : null}
    </section>
  );
}

export default function WorkLocalViewPage() {
  const { id } = useParams<{ id: string }>();
  const workId = Number(id);
  const view = useAsyncData(() => getWorkLocalView(workId), [workId]);

  const data = view.data;
  const mediaFiles = data?.media_files ?? [];
  const videos = mediaFiles.filter(isVideo);
  const images = mediaFiles.filter(isImage);
  const others = mediaFiles.filter((f) => !isVideo(f) && !isImage(f));
  const [viewerFile, setViewerFile] = useState<LocalMediaFile | null>(null);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link to={`/works/${workId}`} className="mb-1 inline-flex items-center gap-1 text-xs text-ink-muted transition-colors hover:text-primary">
            <ArrowLeft className="h-3.5 w-3.5" />
            返回作品详情
          </Link>
          <h1 className="text-lg font-semibold text-ink">{data?.title ?? '作品本地视图'}</h1>
          {data?.platform ? (
            <p className="text-xs text-ink-muted">
              平台：{data.platform} · {mediaFiles.length} 个本地文件
            </p>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => view.refetch()}
          className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-ink-secondary transition-colors hover:bg-sidebar"
        >
          <RefreshCw className="h-4 w-4" />
          刷新
        </button>
      </div>

      {view.loading ? (
        <LoadingState label="加载本地视图…" />
      ) : view.error ? (
        <ErrorState message={view.error.message} onRetry={view.refetch} />
      ) : data ? (
        <>
          {/* ① 原文件链接 */}
          <section className="rounded-lg border border-line bg-surface p-4 shadow-card">
            <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-ink">
              <ExternalLink className="h-4 w-4" />
              ① 博主作品原文件链接
            </h2>
            {data.url ? (
              <div className="flex flex-wrap items-center gap-2">
                <code className="flex-1 break-all rounded bg-sidebar px-2 py-1 font-mono text-xs text-ink-secondary">{data.url}</code>
                <a
                  href={data.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm text-white transition-colors hover:bg-primary-hover"
                >
                  打开原链接
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
            ) : (
              <p className="text-sm text-ink-muted">该作品无原始链接</p>
            )}
          </section>

          {/* ② 本地原文件 */}
          <section className="rounded-lg border border-line bg-surface p-4 shadow-card">
            <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-ink">
              <FolderOpen className="h-4 w-4" />
              ② 下载到本地的原文件
            </h2>
            {mediaFiles.length === 0 ? (
              <p className="text-sm text-ink-muted">本地暂无文件（作品未下载）</p>
            ) : (
              <div className="space-y-4">
                {videos.map((v) => (
                  <div key={v.path} className="space-y-1">
                    <span className="inline-block rounded bg-sidebar px-1.5 py-0.5 text-xs text-ink-secondary">{v.label ?? '视频文件'}</span>
                    <video src={v.url} controls className="max-h-96 w-full rounded-lg bg-black" preload="metadata" />
                  </div>
                ))}
                {images.length ? (
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                    {images.map((im) => (
                      <a key={im.path} href={im.url} target="_blank" rel="noreferrer" className="group relative aspect-square overflow-hidden rounded-lg border border-line">
                        <img src={im.url} alt={im.name} loading="lazy" className="h-full w-full object-cover transition-transform group-hover:scale-105" />
                        <span className="absolute bottom-0 left-0 right-0 truncate bg-black/50 px-1.5 py-0.5 text-[10px] text-white">{im.name}</span>
                      </a>
                    ))}
                  </div>
                ) : null}
                {others.length ? (
                  <table className="w-full border-collapse text-sm">
                    <thead>
                      <tr className="bg-sidebar">
                        <th className="border border-line px-2 py-1.5 text-left font-medium text-ink">类型</th>
                        <th className="border border-line px-2 py-1.5 text-left font-medium text-ink">文件</th>
                        <th className="border border-line px-2 py-1.5 text-right font-medium text-ink">大小</th>
                      </tr>
                    </thead>
                    <tbody>
                      {others.map((f) => (
                        <tr key={f.path}>
                          <td className="border border-line px-2 py-1">
                            <span className="whitespace-nowrap rounded bg-sidebar px-1.5 py-0.5 text-xs text-ink-secondary">{f.label ?? '其他'}</span>
                          </td>
                          <td className="border border-line px-2 py-1">
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                onClick={() => setViewerFile(f)}
                                title="在页面内查看"
                                className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-ink-secondary hover:text-primary"
                              >
                                <FileText className="h-3.5 w-3.5 shrink-0" />
                                <span className="break-all">{f.path}</span>
                              </button>
                              <a href={f.url} target="_blank" rel="noreferrer" title="在新标签打开原文件" className="shrink-0 text-ink-muted hover:text-primary">
                                <ExternalLink className="h-3 w-3" />
                              </a>
                            </div>
                          </td>
                          <td className="border border-line px-2 py-1 text-right font-mono text-xs text-ink-muted">{formatBytes(f.size)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : null}
              </div>
            )}
          </section>

          {/* ②·五 总结交付目标（默认工具可改，素材就绪清单投递到默认目标） */}
          <SummaryTargetCard />

          {/* ③ AI 总结 */}
          <section className="rounded-lg border border-line bg-surface p-4 shadow-card">
            <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-ink">
              <ImageIcon className="h-4 w-4" />
              ③ AI 总结
            </h2>
            {data.note_content ? (
              <div className="max-w-3xl">
                <SimpleMarkdown text={data.note_content} />
              </div>
            ) : (
              <p className="text-sm text-ink-muted">AI 笔记未完成（{data.note_path ? data.note_path : '无笔记记录'}）</p>
            )}
          </section>
        </>
      ) : null}

      {data ? (
        <p className="flex items-center gap-1 text-[11px] text-ink-muted">
          <Film className="h-3 w-3" />
          媒体由本地静态服务提供（/media/…），仅本机可访问
        </p>
      ) : null}

      {viewerFile ? <FileViewerModal file={viewerFile} onClose={() => setViewerFile(null)} /> : null}
    </div>
  );
}
