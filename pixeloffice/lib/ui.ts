// 介面共用的分類、顏色與文字（模式、階段、篩選）
import type { AgentWorker, SessionMode } from './types';

export type Filter = 'all' | 'needs' | 'auto' | 'suggest' | 'off';

export const MODE_META: Record<SessionMode, { label: string; short: string; dot: string; pill: string; ring: string; on: string }> = {
  auto: {
    label: '自動駕駛',
    short: '自動',
    dot: 'bg-emerald-400',
    pill: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
    ring: 'border-l-emerald-400',
    on: 'bg-emerald-500 text-slate-950',
  },
  suggest: {
    label: '只建議',
    short: '建議',
    dot: 'bg-sky-400',
    pill: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
    ring: 'border-l-sky-400',
    on: 'bg-sky-500 text-slate-950',
  },
  off: {
    label: '關閉',
    short: '關閉',
    dot: 'bg-slate-500',
    pill: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
    ring: 'border-l-slate-600',
    on: 'bg-slate-500 text-slate-950',
  },
};

export const PHASES: { id: NonNullable<AgentWorker['phase']>; step: string; label: string; hint: string }[] = [
  { id: 'goal', step: '①', label: '目標與 CE 計畫', hint: '設定房間目標並跟隨 docs/plans/ (ce-plan) 里程碑' },
  { id: 'build', step: '②', label: 'CE 實作與驗證', hint: '依 ce-work / ce-debug 實作並通過測試關卡' },
  { id: 'done', step: '③', label: '已完成 (LLM Wiki 沉澱)', hint: '通過 ce-code-review 並將知識寫入 LLM Wiki' },
];

export const modeOf = (w: AgentWorker): SessionMode => w.mode ?? 'off';

export function matchesFilter(w: AgentWorker, f: Filter): boolean {
  if (f === 'all') return true;
  if (f === 'needs') return Boolean(w.needsYou);
  return modeOf(w) === f;
}

// 狀態標籤的顏色：需要你（琥珀）、出錯（玫瑰）、工作中（綠）、達成（紫）、其他（灰）
export function statusTone(w: AgentWorker): string {
  if (w.needsYou) return 'text-amber-300';
  if (w.status === 'error') return 'text-rose-300';
  if (w.phase === 'done') return 'text-violet-300';
  if (w.status === 'working') return 'text-emerald-300';
  if (modeOf(w) === 'off') return 'text-slate-500';
  return 'text-slate-300';
}

export function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleTimeString('zh-TW', { hour12: false, hour: '2-digit', minute: '2-digit' });
  } catch {
    return '--:--';
  }
}

export function progressPct(w: AgentWorker): number {
  return w.core ? Math.round((100 * (w.verified ?? 0)) / w.core) : 0;
}

export async function copyTextToClipboard(text: string): Promise<boolean> {
  const payload = String(text ?? '').trim();
  if (!payload) return false;
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(payload);
      return true;
    }
  } catch {
    // fallback below
  }
  try {
    if (typeof document !== 'undefined') {
      const ta = document.createElement('textarea');
      ta.value = payload;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    }
  } catch {
    // ignore
  }
  return false;
}

