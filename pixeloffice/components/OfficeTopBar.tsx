'use client';

import React, { useState } from 'react';
import {
  AlertTriangle,
  Cpu,
  Flame,
  Inbox,
  Pause,
  Play,
  Plus,
  ShieldAlert,
  Volume2,
  VolumeX,
  WifiOff,
  Wrench,
  CloudRain,
} from 'lucide-react';
import type { AmbientSoundMode } from '@/lib/sound-fx';
import type { Filter } from '@/lib/ui';
import { MODE_META } from '@/lib/ui';

export interface BudgetSummary {
  daily_token_limit: number;
  today_tokens: number;
  exceeded: boolean;
  running_rooms: number;
  total_rooms: number;
}

interface OfficeTopBarProps {
  filter: Filter;
  onChangeFilter: (f: Filter) => void;
  counts: Record<Filter, number>;
  running: boolean;
  connected: boolean;
  onToggleRunning: () => void;
  inboxCount?: number;
  onOpenInbox?: () => void;
  onOpenWizard?: () => void;
  onOpenFleet?: () => void;
  onOpenSetup?: () => void;
  needsSetup?: boolean;
  budget?: BudgetSummary | null;
  onSaveBudget?: (limit: number) => Promise<void>;
  soundMuted?: boolean;
  ambientMode?: AmbientSoundMode;
  onCycleAudio?: () => void;
}

function formatCompactTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
}

export default function OfficeTopBar({
  filter,
  onChangeFilter,
  counts,
  running,
  connected,
  onToggleRunning,
  inboxCount = 0,
  onOpenInbox,
  onOpenWizard,
  onOpenFleet,
  onOpenSetup,
  needsSetup = false,
  budget,
  onSaveBudget,
  soundMuted = false,
  ambientMode = 'off',
  onCycleAudio,
}: OfficeTopBarProps) {
  const [budgetOpen, setBudgetOpen] = useState(false);
  const [savingBudget, setSavingBudget] = useState(false);

  const tabs: { id: Filter; label: string; dot?: string }[] = [
    { id: 'all', label: '全部' },
    { id: 'needs', label: '需要你' },
    { id: 'auto', label: MODE_META.auto.label, dot: MODE_META.auto.dot },
    { id: 'suggest', label: MODE_META.suggest.label, dot: MODE_META.suggest.dot },
    { id: 'off', label: MODE_META.off.label, dot: MODE_META.off.dot },
  ];

  const audioLabel = soundMuted
    ? '靜音'
    : ambientMode === 'rain'
      ? '細雨'
      : ambientMode === 'hearth'
        ? '地爐'
        : '音效';

  const AudioIcon = soundMuted
    ? VolumeX
    : ambientMode === 'rain'
      ? CloudRain
      : ambientMode === 'hearth'
        ? Flame
        : Volume2;

  const limit = budget?.daily_token_limit ?? 0;
  const used = budget?.today_tokens ?? 0;
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;

  return (
    <header className="shrink-0 bg-[#141720] border-b border-[#252a3a]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 sm:px-6 py-2.5">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-base sm:text-lg font-bold tracking-tight text-slate-100 whitespace-nowrap">
            AI 工程助理
          </span>
          <span className="hidden sm:inline text-xs text-slate-500 font-pixel whitespace-nowrap">PIXEL OFFICE</span>
          {!connected && (
            <span className="flex items-center gap-1 text-[11px] text-rose-300 whitespace-nowrap" title="和 AI 工程助理的連線中斷，正在重試">
              <WifiOff className="w-3.5 h-3.5" aria-hidden="true" />
              連線中斷
            </span>
          )}
        </div>

        {/* 多專案總覽與 Token 成本護欄膠囊 */}
        {budget && (
          <div className="relative order-4 xl:order-2">
            <button
              type="button"
              onClick={() => setBudgetOpen((v) => !v)}
              className={`px-2.5 py-1 text-[11px] rounded-lg border flex items-center gap-2 cursor-pointer transition-colors whitespace-nowrap ${
                budget.exceeded
                  ? 'bg-rose-500/15 border-rose-500/45 text-rose-200'
                  : 'bg-[#0f131d] border-[#262e42] text-slate-300 hover:border-sky-500/40'
              }`}
              title="多專案總覽與每日 Token 成本護欄（點擊設定上限）"
            >
              <span className="text-emerald-300 font-semibold">執行 {budget.running_rooms}/{budget.total_rooms} 房</span>
              <span className="text-slate-600">|</span>
              <span className="font-mono tabular-nums">
                今日 {formatCompactTokens(used)}
                {limit > 0 ? ` / ${formatCompactTokens(limit)}` : ' tok'}
              </span>
              {budget.exceeded && <ShieldAlert className="w-3.5 h-3.5 text-rose-400" />}
            </button>

            {budgetOpen && onSaveBudget && (
              <div className="absolute left-0 mt-1.5 z-40 w-64 rounded-xl bg-[#151a27] border border-[#2d364d] shadow-2xl p-3 text-xs space-y-2.5">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-slate-100">每日 Token 成本護欄</span>
                  <button
                    type="button"
                    onClick={() => setBudgetOpen(false)}
                    className="text-slate-400 hover:text-white text-[11px] cursor-pointer"
                  >
                    關閉
                  </button>
                </div>
                <p className="text-[11px] text-slate-400">
                  超過每日上限時自動暫停全部房間，避免無人看管時燒過頭。
                </p>
                {limit > 0 && (
                  <div className="space-y-1">
                    <div className="flex justify-between text-[10px] font-mono text-slate-300">
                      <span>今日已用 {used.toLocaleString()}</span>
                      <span>{pct}%</span>
                    </div>
                    <div className="h-1.5 w-full rounded-full bg-[#0d1018] overflow-hidden">
                      <div
                        className={`h-full rounded-full ${pct >= 90 ? 'bg-rose-400' : 'bg-sky-400'}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                )}
                <div className="grid grid-cols-2 gap-1.5">
                  {[
                    { label: '無上限', val: 0 },
                    { label: '200K / 日', val: 200_000 },
                    { label: '500K / 日', val: 500_000 },
                    { label: '1M / 日', val: 1_000_000 },
                  ].map((opt) => (
                    <button
                      key={opt.val}
                      type="button"
                      disabled={savingBudget}
                      onClick={async () => {
                        setSavingBudget(true);
                        await onSaveBudget(opt.val);
                        setSavingBudget(false);
                        setBudgetOpen(false);
                      }}
                      className={`px-2 py-1.5 rounded-lg border text-[11px] font-medium cursor-pointer ${
                        limit === opt.val
                          ? 'bg-sky-500/20 border-sky-400 text-sky-200'
                          : 'bg-[#0f131d] border-[#283045] text-slate-300 hover:border-slate-500'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        <div className="ml-auto flex items-center gap-1.5 sm:gap-2 order-2 sm:order-3">
          {onCycleAudio && (
            <button
              type="button"
              onClick={onCycleAudio}
              className={`px-2 py-1.5 text-xs font-medium rounded-lg border transition-colors whitespace-nowrap flex items-center gap-1 cursor-pointer ${
                soundMuted
                  ? 'bg-[#1a1e2b] border-[#282e42] text-slate-400 hover:text-slate-200'
                  : ambientMode !== 'off'
                    ? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-200'
                    : 'bg-[#1a1e2b] border-[#282e42] text-slate-300 hover:text-white'
              }`}
              title="切換環境音與微音效：靜音 → 微音效 → 雨天細雨 → 地爐柴火"
            >
              <AudioIcon className="w-3.5 h-3.5" aria-hidden="true" />
              <span className="hidden xl:inline">{audioLabel}</span>
            </button>
          )}

          {onOpenSetup && (
            <button
              type="button"
              onClick={onOpenSetup}
              className={`relative px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors whitespace-nowrap flex items-center gap-1 cursor-pointer ${
                needsSetup
                  ? 'bg-amber-500/15 border-amber-500/45 text-amber-200 hover:bg-amber-500/25'
                  : 'bg-[#1a1e2b] border-[#282e42] text-slate-300 hover:text-white'
              }`}
              title="環境健檢、模型 IP/Port 設定與安全設定匯出／匯入"
            >
              <Wrench className="w-3.5 h-3.5" aria-hidden="true" />
              <span className="hidden lg:inline">環境健檢</span>
              {needsSetup && (
                <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" aria-hidden="true" />
              )}
            </button>
          )}

          {onOpenInbox && (
            <button
              type="button"
              onClick={onOpenInbox}
              className={`px-2.5 sm:px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors whitespace-nowrap flex items-center gap-1.5 cursor-pointer ${
                inboxCount > 0
                  ? 'bg-amber-500/15 border-amber-500/40 text-amber-200 hover:bg-amber-500/25'
                  : 'bg-[#1a1e2b] border-[#282e42] text-slate-300 hover:text-white'
              }`}
            >
              <Inbox className="w-3.5 h-3.5" aria-hidden="true" />
              <span>收件匣</span>
              <span className="font-mono tabular-nums text-[11px]">{inboxCount}</span>
            </button>
          )}
          {onOpenWizard && (
            <button
              type="button"
              onClick={onOpenWizard}
              className="px-2.5 sm:px-3 py-1.5 text-xs font-semibold rounded-lg bg-sky-500 hover:bg-sky-400 text-slate-950 transition-colors whitespace-nowrap flex items-center gap-1 cursor-pointer"
            >
              <Plus className="w-3.5 h-3.5" aria-hidden="true" />
              <span className="hidden sm:inline">新增房間</span>
            </button>
          )}
          {onOpenFleet && (
            <button
              type="button"
              onClick={onOpenFleet}
              className="px-2.5 py-1.5 text-xs font-medium rounded-lg bg-[#1a1e2b] border border-[#282e42] text-slate-300 hover:text-white transition-colors whitespace-nowrap flex items-center gap-1 cursor-pointer"
              title="查看座位狀態與今日用量"
            >
              <Cpu className="w-3.5 h-3.5" aria-hidden="true" />
              <span className="hidden md:inline">座位／用量</span>
            </button>
          )}
          <button
            type="button"
            onClick={onToggleRunning}
            title="和主視窗的 F8 相同：暫停或繼續所有視窗的觀察與操作"
            className={`px-3 sm:px-4 py-1.5 sm:py-2 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap flex items-center gap-1.5 cursor-pointer text-slate-950 ${
              running ? 'bg-amber-400 hover:bg-amber-300' : 'bg-emerald-400 hover:bg-emerald-300'
            }`}
          >
            {running ? <Pause className="w-3.5 h-3.5" aria-hidden="true" /> : <Play className="w-3.5 h-3.5" aria-hidden="true" />}
            <span>{running ? '全部暫停' : '繼續運作'}</span>
            <span className="hidden md:inline opacity-70">(F8)</span>
          </button>
        </div>

        <nav
          aria-label="依模式篩選"
          className="order-3 sm:order-2 w-full sm:w-auto flex items-center gap-1 p-1 bg-[#0e1017] border border-[#232838] rounded-lg overflow-x-auto"
        >
          {tabs.map((t) => {
            const active = filter === t.id;
            const urgent = t.id === 'needs' && counts.needs > 0;
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => onChangeFilter(t.id)}
                className={`px-2.5 sm:px-3 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap flex items-center gap-1.5 cursor-pointer ${
                  active ? 'bg-[#23293b] text-slate-50' : 'text-slate-400 hover:text-slate-100'
                } ${urgent && !active ? 'text-amber-300' : ''}`}
              >
                {t.dot && <span className={`w-2 h-2 rounded-full ${t.dot}`} aria-hidden="true" />}
                {urgent && <AlertTriangle className="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />}
                <span>{t.label}</span>
                <span
                  className={`text-[11px] font-mono tabular-nums px-1.5 py-0.5 rounded ${
                    urgent ? 'bg-amber-500/20 text-amber-200' : 'bg-[#1a1f2e] text-slate-400'
                  }`}
                >
                  {counts[t.id]}
                </span>
              </button>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
