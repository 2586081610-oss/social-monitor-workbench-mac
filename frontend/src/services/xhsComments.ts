import { getJson, postJson, requestBlob } from './http';
import type { XhsCommentJob, XhsCommentNoteList, XhsCommentResult } from '../types/api.d';

export interface CreateCommentJobInput {
  url: string;
  include_images: boolean;
  include_voice: boolean;
  voice_authorized: boolean;
  voice_allow_open_page: boolean;
}

export function createCommentJob(input: CreateCommentJobInput): Promise<XhsCommentJob> {
  return postJson<XhsCommentJob>('/api/v1/xhs-comments/jobs', input);
}

export function getCommentJob(jobId: string): Promise<XhsCommentJob> {
  return getJson<XhsCommentJob>(`/api/v1/xhs-comments/jobs/${encodeURIComponent(jobId)}`);
}

export function retryCommentJob(
  jobId: string,
  voiceAuthorized = false,
  voiceAllowOpenPage = false,
): Promise<XhsCommentJob> {
  return postJson<XhsCommentJob>(`/api/v1/xhs-comments/jobs/${encodeURIComponent(jobId)}/retry`, {
    voice_authorized: voiceAuthorized,
    voice_allow_open_page: voiceAllowOpenPage,
  });
}

export function listCommentNotes(): Promise<XhsCommentNoteList> {
  return getJson<XhsCommentNoteList>('/api/v1/xhs-comments/notes');
}

export function getCommentResult(noteId: string): Promise<XhsCommentResult> {
  return getJson<XhsCommentResult>(`/api/v1/xhs-comments/notes/${encodeURIComponent(noteId)}`);
}

export function fetchCommentReport(noteId: string): Promise<Blob> {
  return requestBlob(`/api/v1/xhs-comments/notes/${encodeURIComponent(noteId)}/report.md`);
}

export function fetchCommentImage(noteId: string, filename: string): Promise<Blob> {
  return requestBlob(
    `/api/v1/xhs-comments/notes/${encodeURIComponent(noteId)}/images/${encodeURIComponent(filename)}`,
  );
}
