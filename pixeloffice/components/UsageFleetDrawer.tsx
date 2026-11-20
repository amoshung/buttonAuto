'use client';

import React, { useEffect, useRef, useState } from 'react';
import { Activity, Cpu, RefreshCw, RotateCcw, X } from 'lucide-react';
import { teamApi } from '@/lib/api';
import type { TeamSeatStatus, TeamUsageSummary } from '@/lib/types';

interface UsageFleetDrawerProps {
  open: boolean;
  fleet: Record<string, TeamSeatStatus>;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export default function UsageFleetDrawer({ open, fleet, onClose, onToast }: UsageFleetDrawerProps) {
  const [usage, setUsage] = useState<TeamUsageSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const prevFocusRef = useRef<HTMLElement | null>(null);
  const closeBtnRef = useRef<HTMLButtonElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (open) {
      prevFocusRef.current = document.activeElement as HTMLElement | null;
      setTimeout(() => closeBtnRef.current?.focus(), 0);
      teamApi.usage().then(setUsage);
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onCloseRef.current();
        }
      };
      window.addEventListener('keydown', onKey);
      return () => window.removeEventListener('keydown', onKey);
    } else if (prevFocusRef.current) {
      prevFocusRef.current.focus();
      prevFocusRef.current = null;
    }
  }, [open]);

  if (!open) return null;

  const probeAll = async () => {
    setBusy(true);
    try {
      await teamApi.detectModels();
      for (const [seat, info] of Object.entries(fleet || {})) {
        if (info.exhausted) {
          await teamApi.fleetProbe(seat);
        }
      }
      onToast('已觸發座位與模型重新偵測');
    } finally {
      setBusy(false);
    }
  };

  const resetSeat = async (seat: string) => {
    setBusy(true);
    try {
      const r = await teamApi.fleetReset(seat);
      onToast(r.text);
    } finally {
      setBusy(false);
    }
  };

  const seats = Object.entries(fleet || {});

  return (
    <div
      className="fixed inset-0 z-40 bg-black/60 backdrop-blur-xs flex justify-end"
      role="dialog"
      aria-modal="true"
      aria-label="座位與用量抽屜"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-md h-full bg-[#141720] border-l border-[#262b3d] flex flex-col shadow-2xl">
        <div className="px-5 py-4 border-b border-[#252a3a] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Cpu className="w-4 h-4 text-sky-400" aria-hidden="true" />
            <h2 className="text-sm font-bold text-slate-100">座位狀態與模型用量</h2>
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-100 hover:bg-[#23293b] cursor-pointer"
            aria-label="關閉座位與用量抽屜"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-5 text-xs">
          <section className="space-y-2.5">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold text-slate-200">CLI 與模型座位池</h3>
              <button
                type="button"
                disabled={busy}
                onClick={probeAll}
                className="px-2.5 py-1 rounded-md bg-[#23293b] hover:bg-[#2f3750] text-slate-200 flex items-center gap-1 cursor-pointer disabled:opacity-50"
              >
                <RefreshCw className="w-3 h-3" />
                立即探測
              </button>
            </div>
            <div className="space-y-2">
              {seats.length === 0 ? (
                <div className="text-slate-500">尚無座位資訊</div>
              ) : (
                seats.map(([seatKey, info]) => {
                  const ok = info.state === 'ok';
                  return (
                    <div
                      key={seatKey}
                      className="rounded-xl bg-[#1a1e2b] border border-[#2a3044] p-3 flex items-center justify-between gap-2"
                    >
                      <div className="min-w-0">
                        <div className="font-semibold text-slate-100 flex items-center gap-2">
                          <span>{info.label || seatKey}</span>
                          <span
                            className={`text-[10px] px-1.5 py-0.5 rounded ${
                              ok ? 'bg-emerald-500/20 text-emerald-300' : 'bg-rose-500/20 text-rose-300'
                            }`}
                          >
                            {ok ? '可用' : info.state === 'exhausted' ? '額度已滿' : info.state}
                          </span>
                        </div>
                        {info.until && <div className="text-[11px] text-slate-400 mt-0.5">重置探測：{info.until}</div>}
                        {info.reason && <div className="text-[11px] text-amber-300 mt-0.5">{info.reason}</div>}
                      </div>
                      {!ok && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => resetSeat(seatKey)}
                          className="px-2 py-1 rounded bg-[#252c40] hover:bg-[#313a54] text-slate-200 flex items-center gap-1 cursor-pointer"
                        >
                          <RotateCcw className="w-3 h-3" />
                          重置
                        </button>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </section>

          <section className="space-y-2.5">
            <div className="flex items-center gap-1.5 font-semibold text-slate-200">
              <Activity className="w-3.5 h-3.5 text-emerald-400" />
              <h3>今日呼叫與用量統計</h3>
            </div>
            {usage ? (
              <pre className="text-[11px] font-mono bg-[#0e1017] border border-[#232838] rounded-xl p-3 text-slate-300 overflow-x-auto">
                {JSON.stringify(usage, null, 2)}
              </pre>
            ) : (
              <div className="text-slate-500">載入用量中…</div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
