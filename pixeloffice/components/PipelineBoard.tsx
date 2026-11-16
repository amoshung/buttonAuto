'use client';

import React from 'react';
import { AlertTriangle, GitCommit, MessageSquare } from 'lucide-react';
import type { AgentWorker } from '@/lib/types';
import { MODE_META, PHASES, modeOf, progressPct, statusTone } from '@/lib/ui';

interface PipelineBoardProps {
  workers: AgentWorker[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

// 開發流程看板：每個視窗依「目標 → 驗收 spec → 實作與查核 → 已達成」放在對應的欄位
export default function PipelineBoard({ workers, selectedId, onSelect }: PipelineBoardProps) {
  return (
    <section aria-label="開發流程" className="px-3 sm:px-4 pb-4">
      <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 2xl:grid-cols-4">
        {PHASES.map((p) => {
          const items = workers.filter((w) => (w.phase ?? 'goal') === p.id);
          return (
            <div key={p.id} className="min-w-0 rounded-xl bg-[#141720] border border-[#232838] flex flex-col">
              <div className="flex items-baseline justify-between gap-2 px-3 pt-2.5 pb-2 border-b border-[#1e2332]">
                <div className="min-w-0">
                  <div className="text-xs font-semibold text-slate-200">
                    {p.step} {p.label}
                    <span className="ml-1.5 font-mono tabular-nums text-slate-500">{items.length}</span>
                  </div>
                  <div className="text-[11px] text-slate-500 truncate">{p.hint}</div>
                </div>
              </div>
              <div className="p-2 space-y-2">
                {items.length === 0 ? (
                  <div className="text-[11px] text-slate-600 px-1 py-2">（沒有）</div>
                ) : (
                  items.map((w) => <SessionCard key={w.id} w={w} selected={w.id === selectedId} onSelect={onSelect} />)
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function SessionCard({ w, selected, onSelect }: { w: AgentWorker; selected: boolean; onSelect: (id: string) => void }) {
  const mode = modeOf(w);
  const meta = MODE_META[mode];
  const pct = progressPct(w);
  return (
    <button
      type="button"
      onClick={() => onSelect(w.id)}
      className={`w-full text-left rounded-lg border-l-4 ${meta.ring} border border-[#262c3d] px-3 py-2 transition-colors cursor-pointer ${
        selected ? 'bg-[#232a3d] border-r-amber-400/60 border-t-amber-400/60 border-b-amber-400/60' : 'bg-[#181c28] hover:bg-[#1d2231]'
      } ${w.needsYou ? 'shadow-[inset_0_0_0_1px_rgba(251,191,36,0.45)]' : ''} ${mode === 'off' ? 'opacity-60' : ''}`}
    >
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-sm font-semibold text-slate-100 truncate">{w.name}</span>
        <span className="text-[10px] text-slate-500 font-mono shrink-0">{w.appName}</span>
        <span className={`ml-auto shrink-0 text-[10px] px-1.5 py-0.5 rounded border ${meta.pill}`}>{meta.label}</span>
      </div>
      <div className={`mt-1 text-xs flex items-center gap-1 ${statusTone(w)}`}>
        {w.needsYou && <AlertTriangle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />}
        <span className="truncate">{w.statusLabel}</span>
      </div>
      <p className="mt-1 text-[11px] text-slate-400 line-clamp-2 leading-relaxed">{w.goal || w.activity}</p>
      {(w.core ?? 0) > 0 && (
        <div className="mt-1.5 flex items-center gap-2">
          <div className="flex-1 h-1 rounded bg-[#262c3d] overflow-hidden">
            <div className="h-full bg-emerald-400" style={{ width: `${pct}%` }} />
          </div>
          <span className="text-[10px] font-mono tabular-nums text-slate-500">
            {w.verified}/{w.core}
          </span>
        </div>
      )}
      <div className="mt-1.5 flex items-center gap-3 text-[10px] text-slate-500">
        {w.chatTitle && (
          <span className="flex items-center gap-1 min-w-0">
            <MessageSquare className="w-3 h-3 shrink-0" aria-hidden="true" />
            <span className="truncate">{w.chatTitle}</span>
          </span>
        )}
        {(w.commits?.length ?? 0) > 0 && (
          <span className="flex items-center gap-1 shrink-0 font-mono">
            <GitCommit className="w-3 h-3" aria-hidden="true" />
            {w.commits![0].hash.slice(0, 7)}
          </span>
        )}
      </div>
    </button>
  );
}
