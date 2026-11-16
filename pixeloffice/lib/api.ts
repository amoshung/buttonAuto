// 和 AI 工程助理（dashboard.py）溝通。讀取用 cookie 驗證；會動手的 POST 另外帶 X-Token 標頭（防跨站請求）。
import type { OfficeSnapshot, SessionMode } from './types';

function token(): string {
  if (typeof document === 'undefined') return '';
  return document.querySelector('meta[name="aea-token"]')?.getAttribute('content') ?? '';
}

export interface ActionResult {
  ok: boolean;
  text: string;
  snapshot?: OfficeSnapshot;
}

async function post(path: string, body: unknown): Promise<ActionResult> {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Token': token() },
      body: JSON.stringify(body),
    });
    const data = (await res.json().catch(() => ({}))) as Partial<ActionResult>;
    return { ok: Boolean(data.ok), text: data.text ?? (res.ok ? '已送出' : `失敗（${res.status}）`), snapshot: data.snapshot };
  } catch (e) {
    return { ok: false, text: `連不上 AI 工程助理：${String(e)}` };
  }
}

export const officeApi = {
  async state(): Promise<OfficeSnapshot | null> {
    try {
      const res = await fetch('/api/watcher/state', { cache: 'no-store' });
      return res.ok ? ((await res.json()) as OfficeSnapshot) : null;
    } catch {
      return null;
    }
  },
  assist: (workerId: string, actionLabel: string, message: string) =>
    post('/api/watcher/assist', { workerId, actionLabel, message }),
  setMode: (workerId: string, mode: SessionMode) => post('/api/office/mode', { workerId, mode }),
  setRunning: (running: boolean) => post('/api/office/running', { running }),
};
