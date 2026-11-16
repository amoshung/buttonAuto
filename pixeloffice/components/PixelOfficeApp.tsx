'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { officeApi } from '@/lib/api';
import { soundFx } from '@/lib/sound-fx';
import type { AgentWorker, OfficeSnapshot, SessionMode } from '@/lib/types';
import OfficeTopBar, { type StatusFilter } from './OfficeTopBar';
import PixelOfficeCanvas from './PixelOfficeCanvas';
import WorkerDrawer from './WorkerDrawer';

interface PixelOfficeAppProps {
  initialSnapshot: OfficeSnapshot;
}

const isHelp = (w: AgentWorker) => w.status === 'needs_help' || w.status === 'error';

export default function PixelOfficeApp({ initialSnapshot }: PixelOfficeAppProps) {
  const [snapshot, setSnapshot] = useState<OfficeSnapshot>(initialSnapshot);
  const [activeFilter, setActiveFilter] = useState<StatusFilter>('all');
  const [selectedWorkerId, setSelectedWorkerId] = useState<string | null>(() => {
    const firstHelp = initialSnapshot.workers.find(isHelp);
    return firstHelp?.id ?? initialSnapshot.workers[0]?.id ?? null;
  });
  const [soundMuted, setSoundMuted] = useState(false);
  const [submittingAssist, setSubmittingAssist] = useState(false);
  const [toastMsg, setToastMsg] = useState<string | null>(null);
  const [connected, setConnected] = useState(true);

  const prevHelpIdsRef = useRef<Set<string>>(new Set(initialSnapshot.workers.filter(isHelp).map((w) => w.id)));

  const showToast = useCallback((msg: string) => setToastMsg(msg), []);

  useEffect(() => {
    if (!toastMsg) return;
    const t = setTimeout(() => setToastMsg(null), 3200);
    return () => clearTimeout(t);
  }, [toastMsg]);

  const applyIncomingSnapshot = useCallback(
    (next: OfficeSnapshot) => {
      if (!next?.workers) return;
      const currentHelpIds = new Set(next.workers.filter(isHelp).map((w) => w.id));
      for (const id of currentHelpIds) {
        if (!prevHelpIdsRef.current.has(id)) {
          soundFx.playHelpAlert();
          const target = next.workers.find((w) => w.id === id);
          if (target) showToast(`${target.name} 需要您的協助`);
          break;
        }
      }
      prevHelpIdsRef.current = currentHelpIds;
      setSnapshot(next);
    },
    [showToast]
  );

  // 即時更新：SSE；斷線時改成每 3 秒輪詢，並每 15 秒試著重新接上 SSE
  useEffect(() => {
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const stopPolling = () => {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
    };
    const startPolling = () => {
      if (pollTimer || disposed) return;
      pollTimer = setInterval(async () => {
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
        stopPolling();
      };
      es.onmessage = (event) => {
        try {
          applyIncomingSnapshot(JSON.parse(event.data) as OfficeSnapshot);
        } catch {
          // 忽略格式不對的訊息
        }
      };
      es.onerror = () => {
        es?.close();
        es = null;
        startPolling();
        retryTimer = setTimeout(connect, 15000);
      };
    };
    connect();
    return () => {
      disposed = true;
      es?.close();
      stopPolling();
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [applyIncomingSnapshot]);

  const counts = useMemo(() => {
    const ws = snapshot.workers;
    return {
      all: ws.length,
      help: ws.filter(isHelp).length,
      working: ws.filter((w) => w.status === 'working').length,
      idle: ws.filter((w) => w.status === 'idle' || w.status === 'paused').length,
    };
  }, [snapshot.workers]);

  const filterList = useCallback(
    (filter: StatusFilter) => {
      const ws = snapshot.workers;
      if (filter === 'help') return ws.filter(isHelp);
      if (filter === 'working') return ws.filter((w) => w.status === 'working');
      if (filter === 'idle') return ws.filter((w) => w.status === 'idle' || w.status === 'paused');
      return ws;
    },
    [snapshot.workers]
  );
  const filteredWorkers = useMemo(() => filterList(activeFilter), [activeFilter, filterList]);

  const selectedWorker: AgentWorker | null = useMemo(
    () => snapshot.workers.find((w) => w.id === selectedWorkerId) ?? filteredWorkers[0] ?? snapshot.workers[0] ?? null,
    [filteredWorkers, selectedWorkerId, snapshot.workers]
  );

  const handleJumpNextHelp = useCallback(() => {
    const helpWorkers = snapshot.workers.filter(isHelp);
    if (helpWorkers.length === 0) return;
    const currIdx = helpWorkers.findIndex((w) => w.id === selectedWorkerId);
    setSelectedWorkerId(helpWorkers[(currIdx + 1) % helpWorkers.length].id);
  }, [selectedWorkerId, snapshot.workers]);

  const running = snapshot.running !== false;

  const handleToggleRunning = useCallback(async () => {
    const r = await officeApi.setRunning(!running);
    showToast(r.text);
    if (r.snapshot) applyIncomingSnapshot(r.snapshot);
  }, [applyIncomingSnapshot, running, showToast]);

  // 快捷鍵：[N] 下一位需要協助的夥伴
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === 'n' || e.key === 'N') {
        e.preventDefault();
        handleJumpNextHelp();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [handleJumpNextHelp]);

  const handleSubmitAssist = async (workerId: string, actionLabel: string, message: string) => {
    setSubmittingAssist(true);
    try {
      const r = await officeApi.assist(workerId, actionLabel, message);
      if (r.snapshot) applyIncomingSnapshot(r.snapshot);
      if (r.ok) soundFx.playAssistConfirm();
      showToast(r.text);
    } finally {
      setSubmittingAssist(false);
    }
  };

  const handleSetMode = async (workerId: string, mode: SessionMode) => {
    const r = await officeApi.setMode(workerId, mode);
    if (r.snapshot) applyIncomingSnapshot(r.snapshot);
    showToast(r.text);
  };

  const handleToggleSound = () => {
    setSoundMuted((prev) => {
      soundFx.setMuted(!prev);
      return !prev;
    });
  };

  const handleChangeFilter = (filter: StatusFilter) => {
    setActiveFilter(filter);
    const list = filterList(filter);
    if (list.length > 0 && !list.some((w) => w.id === selectedWorkerId)) setSelectedWorkerId(list[0].id);
  };

  return (
    <div className="h-screen w-screen flex flex-col bg-[#12141c] text-slate-100 overflow-hidden">
      <OfficeTopBar
        activeFilter={activeFilter}
        onChangeFilter={handleChangeFilter}
        counts={counts}
        running={running}
        connected={connected}
        onToggleRunning={handleToggleRunning}
      />

      <main className="flex-1 min-h-0 flex flex-col lg:flex-row overflow-hidden">
        <PixelOfficeCanvas
          workers={snapshot.workers}
          selectedWorkerId={selectedWorker?.id ?? null}
          onSelectWorker={(id) => setSelectedWorkerId(id)}
          onJumpNextHelp={handleJumpNextHelp}
          needsHelpCount={counts.help}
          soundMuted={soundMuted}
          onToggleSound={handleToggleSound}
        />

        <WorkerDrawer
          workers={snapshot.workers}
          filteredWorkers={filteredWorkers}
          selectedWorker={selectedWorker}
          activeFilter={activeFilter}
          onChangeFilter={handleChangeFilter}
          onSelectWorker={(id) => setSelectedWorkerId(id)}
          onSubmitAssist={handleSubmitAssist}
          onSetMode={handleSetMode}
          submittingAssist={submittingAssist}
          usage={snapshot.usage}
        />
      </main>

      {toastMsg && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-5 left-1/2 -translate-x-1/2 z-40 px-4 py-2 rounded-lg bg-[#1d2333] border border-amber-400/50 text-xs font-medium text-slate-100 shadow-xl"
        >
          {toastMsg}
        </div>
      )}
    </div>
  );
}
