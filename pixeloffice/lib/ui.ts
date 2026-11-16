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
  { id: 'goal', step: '①', label: '確認目標', hint: '助理推測或請代理說明最終目標' },
  { id: 'spec', step: '②', label: '驗收 spec', hint: '和代理討論出可檢查的驗收條件' },
  { id: 'build', step: '③', label: '實作與查核', hint: '代理實作、助理要證據並在本機查核' },
  { id: 'done', step: '④', label: '已達成', hint: '所有自動化條件都有證據' },
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
