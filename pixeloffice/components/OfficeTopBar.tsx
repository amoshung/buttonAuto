'use client';

import React from 'react';
import { Pause, Play, WifiOff } from 'lucide-react';

export type StatusFilter = 'all' | 'help' | 'working' | 'idle';

interface OfficeTopBarProps {
  activeFilter: StatusFilter;
  onChangeFilter: (filter: StatusFilter) => void;
  counts: { all: number; help: number; working: number; idle: number };
  running: boolean;
  connected: boolean;
  onToggleRunning: () => void;
}

export default function OfficeTopBar({
  activeFilter,
  onChangeFilter,
  counts,
  running,
  connected,
  onToggleRunning,
}: OfficeTopBarProps) {
  const filters: { id: StatusFilter; label: string; count: number }[] = [
    { id: 'all', label: '全部視窗', count: counts.all },
    { id: 'help', label: '需協助', count: counts.help },
    { id: 'working', label: '工作中', count: counts.working },
    { id: 'idle', label: '待機休息', count: counts.idle },
  ];

  return (
    <header className="flex items-center justify-between gap-8 px-6 py-3 bg-[#141720] border-b border-[#252a3a] shrink-0">
      <div className="flex items-center gap-3 shrink-0">
        <span className="text-lg font-bold tracking-tight text-slate-100 whitespace-nowrap font-pixel">
          AI 工程助理 · 像素辦公室
        </span>
        {!connected && (
          <span className="flex items-center gap-1 text-[11px] text-rose-300" title="和 AI 工程助理的連線中斷，正在重試">
            <WifiOff className="w-3.5 h-3.5" aria-hidden="true" />
            連線中斷
          </span>
        )}
      </div>

      <nav
        aria-label="視窗狀態篩選"
        className="hidden md:flex items-center gap-1 p-1 bg-[#0e1017] border border-[#232838] rounded-lg"
      >
        {filters.map((item) => {
          const isActive = activeFilter === item.id;
          const isUrgent = item.id === 'help' && item.count > 0;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onChangeFilter(item.id)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap shrink-0 flex items-center gap-1.5 cursor-pointer focus-visible:outline-2 focus-visible:outline-amber-400 ${isActive ? 'bg-[#23293b] text-slate-50 shadow-sm' : 'text-slate-400 hover:text-slate-100'
                }`}
            >
              <span>{item.label}</span>
              <span
                className={`tabular-nums text-[11px] font-mono ${isUrgent ? 'text-amber-400 font-semibold' : isActive ? 'text-slate-300' : 'text-slate-500'
                  }`}
              >
                {item.count}
              </span>
            </button>
          );
        })}
      </nav>

      <div className="flex items-center gap-3 shrink-0">
        <a
          href="/classic/"
          className="text-xs text-slate-400 hover:text-slate-200 whitespace-nowrap"
          title="改用原本的清單式儀表板"
        >
          經典版
        </a>
        <button
          type="button"
          onClick={onToggleRunning}
          title="和主視窗的 F8 相同：暫停或繼續所有視窗的觀察與操作"
          className={`px-4 py-2 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap shrink-0 flex items-center gap-2 cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-400 ${running
              ? 'text-slate-950 bg-amber-400 hover:bg-amber-300'
              : 'text-slate-950 bg-emerald-400 hover:bg-emerald-300'
            }`}
        >
          {running ? <Pause className="w-3.5 h-3.5" aria-hidden="true" /> : <Play className="w-3.5 h-3.5" aria-hidden="true" />}
          <span>{running ? '全部暫停 (F8)' : '繼續運作 (F8)'}</span>
        </button>
      </div>
    </header>
  );
}
