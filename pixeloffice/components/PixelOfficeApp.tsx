'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { officeApi } from '@/lib/api';
import { soundFx } from '@/lib/sound-fx';
import type { AgentWorker, OfficeSnapshot, SessionMode } from '@/lib/types';
import { type Filter, matchesFilter } from '@/lib/ui';
import OfficeTopBar from './OfficeTopBar';
import PipelineBoard from './PipelineBoard';
import PixelOfficeCanvas from './PixelOfficeCanvas';
import SessionPanel from './SessionPanel';

interface PixelOfficeAppProps {
  initialSnapshot: OfficeSnapshot;
}

const FILTERS: Filter[] = ['all', 'needs', 'auto', 'suggest', 'off'];

// 版面：
// - 寬螢幕（≥1024px）：左邊「像素辦公室＋開發流程看板」，右邊固定的詳情面板
// - 中小螢幕：辦公室可收合、看板改成單欄或雙欄；點卡片時詳情從右側滑出（手機全螢幕）
export default function PixelOfficeApp({ initialSnapshot }: PixelOfficeAppProps) {
  const [snapshot, setSnapshot] = useState<OfficeSnapshot>(initialSnapshot);
  const [filter, setFilter] = useState<Filter>('all');
  const [selectedId, setSelectedId] = useState<string | null>(
    () => initialSnapshot.workers.find((w) => w.needsYou)?.id ?? initialSnapshot.workers[0]?.id ?? null
  );
  const [panelOpen, setPanelOpen] = useState(false); // 只影響中小螢幕
  const [officeOpen, setOfficeOpen] = useState(true);
  const [soundMuted, setSoundMuted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [toastMsg, setToastMsg] = useState<string | null>(null);
  const [connected, setConnected] = useState(true);
  const prevNeedsRef = useRef<Set<string>>(new Set(initialSnapshot.workers.filter((w) => w.needsYou).map((w) => w.id)));

  useEffect(() => {
    const saved = window.localStorage.getItem('aea-office-open');
    if (saved !== null) setOfficeOpen(saved === '1');
    else if (window.innerWidth < 640) setOfficeOpen(false); // 手機預設先看看板
  }, []);
  const toggleOffice = () =>
    setOfficeOpen((v) => {
      window.localStorage.setItem('aea-office-open', v ? '0' : '1');
      return !v;
    });

  const showToast = useCallback((msg: string) => setToastMsg(msg), []);
  useEffect(() => {
    if (!toastMsg) return;
    const t = setTimeout(() => setToastMsg(null), 3200);
    return () => clearTimeout(t);
  }, [toastMsg]);

  const applyIncomingSnapshot = useCallback(
    (next: OfficeSnapshot) => {
      if (!next?.workers) return;
      const needs = new Set(next.workers.filter((w) => w.needsYou).map((w) => w.id));
      for (const id of needs) {
        if (!prevNeedsRef.current.has(id)) {
          soundFx.playHelpAlert();
          const target = next.workers.find((w) => w.id === id);
          if (target) showToast(`${target.name}：${target.statusLabel ?? '需要你'}`);
          break;
        }
      }
      prevNeedsRef.current = needs;
      setSnapshot(next);
    },
    [showToast]
  );

  // 即時更新：SSE；斷線時改成每 3 秒輪詢，並每 15 秒試著重新接上
  useEffect(() => {
    let es: EventSource | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    const stopPoll = () => {
      if (poll) clearInterval(poll);
      poll = null;
    };
    const startPoll = () => {
      if (poll || disposed) return;
      poll = setInterval(async () => {
        const next = await officeApi.state();
        setConnected(Boolean(next));
        if (next) applyIncomingSnapshot(next);
      }, 3000);
    };
    const connect = () => {
      if (disposed) return;
      es = new EventSource('/api/watcher/stream');
      es.onopen = () => {
        setConnected(true);
        stopPoll();
      };
      es.onmessage = (ev) => {
        try {
          applyIncomingSnapshot(JSON.parse(ev.data) as OfficeSnapshot);
        } catch {
          // 忽略格式不對的訊息
        }
      };
      es.onerror = () => {
        es?.close();
        es = null;
        startPoll();
        retry = setTimeout(connect, 15000);
      };
    };
    connect();
    return () => {
      disposed = true;
      es?.close();
      stopPoll();
      if (retry) clearTimeout(retry);
    };
  }, [applyIncomingSnapshot]);

  const counts = useMemo(() => {
    const c = {} as Record<Filter, number>;
    for (const f of FILTERS) c[f] = snapshot.workers.filter((w) => matchesFilter(w, f)).length;
    return c;
  }, [snapshot.workers]);

  const visible = useMemo(() => snapshot.workers.filter((w) => matchesFilter(w, filter)), [filter, snapshot.workers]);
  const selected: AgentWorker | null = useMemo(
    () => snapshot.workers.find((w) => w.id === selectedId) ?? visible[0] ?? snapshot.workers[0] ?? null,
    [selectedId, snapshot.workers, visible]
  );

  const select = useCallback((id: string) => {
    setSelectedId(id);
    setPanelOpen(true);
  }, []);

  const jumpNextNeeds = useCallback(() => {
    const list = snapshot.workers.filter((w) => w.needsYou);
    if (list.length === 0) return;
    const i = list.findIndex((w) => w.id === selectedId);
    select(list[(i + 1) % list.length].id);
  }, [select, selectedId, snapshot.workers]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === 'n' || e.key === 'N') {
        e.preventDefault();
        jumpNextNeeds();
      } else if (e.key === 'Escape') {
        setPanelOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [jumpNextNeeds]);

  const running = snapshot.running !== false;
  const run = async (fn: () => Promise<{ ok: boolean; text: string; snapshot?: OfficeSnapshot }>, okSound = false) => {
    setBusy(true);
    try {
      const r = await fn();
      if (r.snapshot) applyIncomingSnapshot(r.snapshot);
      if (r.ok && okSound) soundFx.playAssistConfirm();
      showToast(r.text);
    } finally {
      setBusy(false);
    }
  };

  const onAssist = (id: string, label: string, message: string) => run(() => officeApi.assist(id, label, message), true);
  const onSetMode = (id: string, mode: SessionMode) => run(() => officeApi.setMode(id, mode));

  return (
    <div className="h-dvh w-screen flex flex-col bg-[#12141c] text-slate-100 overflow-hidden">
      <OfficeTopBar
        filter={filter}
        onChangeFilter={setFilter}
        counts={counts}
        running={running}
        connected={connected}
        onToggleRunning={() => run(() => officeApi.setRunning(!running))}
      />

      <div className="flex-1 min-h-0 flex">
        <main className="flex-1 min-w-0 overflow-y-auto">
          {!running && (
            <div className="mx-3 sm:mx-4 mt-3 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-xs text-amber-200">
              已全部暫停：助理不會觀察也不會操作任何視窗。按右上角「繼續運作」或主視窗的 F8 繼續。
            </div>
          )}

          <section className="px-3 sm:px-4 pt-3" aria-label="像素辦公室">
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-xs font-semibold text-slate-400">
                辦公室 <span className="text-slate-600">· 左區自動駕駛、右區只建議、關閉的在茶水間</span>
              </h2>
              <button
                type="button"
                onClick={toggleOffice}
                className="text-xs text-slate-400 hover:text-slate-100 flex items-center gap-1 cursor-pointer"
              >
                {officeOpen ? <ChevronUp className="w-3.5 h-3.5" aria-hidden="true" /> : <ChevronDown className="w-3.5 h-3.5" aria-hidden="true" />}
                {officeOpen ? '收起' : '展開'}
              </button>
            </div>
            {officeOpen && (
              <PixelOfficeCanvas
                className="h-[clamp(220px,42vh,520px)] rounded-xl border border-[#232838]"
                workers={snapshot.workers}
                selectedWorkerId={selected?.id ?? null}
                onSelectWorker={select}
                onJumpNextHelp={jumpNextNeeds}
                needsHelpCount={counts.needs}
                soundMuted={soundMuted}
                onToggleSound={() =>
                  setSoundMuted((v) => {
                    soundFx.setMuted(!v);
                    return !v;
                  })
                }
              />
            )}
          </section>

          <div className="px-3 sm:px-4 pt-4 pb-2 flex items-baseline justify-between gap-2">
            <h2 className="text-xs font-semibold text-slate-400">開發流程</h2>
            {snapshot.usage?.today && (
              <span className="text-[11px] text-slate-500 font-mono tabular-nums truncate" title="今天：助理代按／自動送出／查核報告／交給你">
                今天 代按 {snapshot.usage.today.auto_clicks} · 送出 {snapshot.usage.today.auto_sends} · 查核 {snapshot.usage.today.verifies} · 交給你 {snapshot.usage.today.asks}
              </span>
            )}
          </div>
          {visible.length === 0 ? (
            <p className="px-4 pb-6 text-sm text-slate-500">這個分類目前沒有視窗。</p>
          ) : (
            <PipelineBoard workers={visible} selectedId={selected?.id ?? null} onSelect={select} />
          )}
        </main>

        {/* 詳情：寬螢幕固定在右邊；中小螢幕從右側滑出 */}
        <div
          className={`fixed inset-0 z-30 bg-black/60 lg:hidden transition-opacity ${panelOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
          onClick={() => setPanelOpen(false)}
          aria-hidden="true"
        />
        <aside
          aria-label="視窗詳情"
          className={`bg-[#161923] border-l border-[#252a3a] flex flex-col
            fixed inset-y-0 right-0 z-40 w-full sm:w-[420px] transition-transform duration-200
            ${panelOpen ? 'translate-x-0' : 'translate-x-full'}
            lg:static lg:z-auto lg:translate-x-0 lg:w-[400px] xl:w-[440px] lg:shrink-0`}
        >
          <SessionPanel worker={selected} onClose={() => setPanelOpen(false)} onAssist={onAssist} onSetMode={onSetMode} busy={busy} />
        </aside>
      </div>

      {toastMsg && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-5 left-1/2 -translate-x-1/2 z-50 max-w-[90vw] px-4 py-2 rounded-lg bg-[#1d2333] border border-amber-400/50 text-xs font-medium text-slate-100 shadow-xl"
        >
          {toastMsg}
        </div>
      )}
    </div>
  );
}
