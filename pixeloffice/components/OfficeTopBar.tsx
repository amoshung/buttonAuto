'use client';

import React from 'react';
import { AlertTriangle, Pause, Play, WifiOff } from 'lucide-react';
import type { Filter } from '@/lib/ui';
import { MODE_META } from '@/lib/ui';

interface OfficeTopBarProps {
  filter: Filter;
  onChangeFilter: (f: Filter) => void;
  counts: Record<Filter, number>;
  running: boolean;
  connected: boolean;
  onToggleRunning: () => void;
}

export default function OfficeTopBar({ filter, onChangeFilter, counts, running, connected, onToggleRunning }: OfficeTopBarProps) {
  const tabs: { id: Filter; label: string; dot?: string }[] = [
    { id: 'all', label: '全部' },
    { id: 'needs', label: '需要你' },
    { id: 'auto', label: MODE_META.auto.label, dot: MODE_META.auto.dot },
    { id: 'suggest', label: MODE_META.suggest.label, dot: MODE_META.suggest.dot },
    { id: 'off', label: MODE_META.off.label, dot: MODE_META.off.dot },
  ];

  return (
    <header className="shrink-0 bg-[#141720] border-b border-[#252a3a]">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 sm:px-6 py-2.5">
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

        <div className="ml-auto flex items-center gap-2 sm:gap-3 order-2 sm:order-3">
          <a href="/classic/" className="hidden sm:inline text-xs text-slate-400 hover:text-slate-200 whitespace-nowrap">
            經典版
          </a>
          <button
            type="button"
            onClick={onToggleRunning}
            title="和主視窗的 F8 相同：暫停或繼續所有視窗的觀察與操作"
            className={`px-3 sm:px-4 py-1.5 sm:py-2 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap flex items-center gap-1.5 cursor-pointer text-slate-950 ${running ? 'bg-amber-400 hover:bg-amber-300' : 'bg-emerald-400 hover:bg-emerald-300'
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
                className={`px-2.5 sm:px-3 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap flex items-center gap-1.5 cursor-pointer ${active ? 'bg-[#23293b] text-slate-50' : 'text-slate-400 hover:text-slate-100'
                  } ${urgent && !active ? 'text-amber-300' : ''}`}
              >
                {t.dot && <span className={`w-1.5 h-1.5 rounded-full ${t.dot}`} aria-hidden="true" />}
                {t.id === 'needs' && <AlertTriangle className={`w-3.5 h-3.5 ${urgent ? 'text-amber-400' : ''}`} aria-hidden="true" />}
                <span>{t.label}</span>
                <span className={`tabular-nums text-[11px] font-mono ${urgent ? 'text-amber-400 font-semibold' : 'text-slate-500'}`}>
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
