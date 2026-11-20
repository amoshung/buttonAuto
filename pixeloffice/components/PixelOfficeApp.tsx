'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { officeApi, teamApi } from '@/lib/api';
import { soundFx, type AmbientSoundMode } from '@/lib/sound-fx';
import type { AgentWorker, OfficeSnapshot, SessionMode, TeamModelsView, TeamState } from '@/lib/types';
import { type Filter, copyTextToClipboard, matchesFilter } from '@/lib/ui';
import type { PersonaSlotKey } from '@/lib/pixelRoomScene';
import DispatchWizard from './DispatchWizard';
import ExceptionInbox from './ExceptionInbox';
import OfficeTopBar, { type BudgetSummary } from './OfficeTopBar';
import RoomGrid, { type GridMode } from './RoomGrid';
import SessionPanel from './SessionPanel';
import SetupWizardModal, { type PreflightReport } from './SetupWizardModal';
import UsageFleetDrawer from './UsageFleetDrawer';

interface PixelOfficeAppProps {
  initialSnapshot: OfficeSnapshot;
}

const FILTERS: Filter[] = ['all', 'needs', 'auto', 'suggest', 'off'];

function mapRoomPhase(phase: string): 'goal' | 'spec' | 'build' | 'done' {
  if (phase === 'spec' || phase === 'plan') return 'spec';
  if (phase === 'done') return 'done';
  if (phase === 'build' || phase === 'test' || phase === 'qa' || phase === 'review') return 'build';
  return 'goal';
}

function mapRoomStatus(roomStatus: string, needsYou: boolean): AgentWorker['status'] {
  if (roomStatus === 'error') return 'error';
  if (needsYou) return 'needs_help';
  if (roomStatus === 'paused') return 'paused';
  if (roomStatus === 'done') return 'idle';
  return 'working';
}

export function teamStateToSnapshot(team: TeamState): OfficeSnapshot {
  const inboxByRoom = new Map<string, TeamState['inbox']>();
  for (const item of team?.inbox ?? []) {
    const arr = inboxByRoom.get(item.room_id) ?? [];
    arr.push(item);
    inboxByRoom.set(item.room_id, arr);
  }
  const workers: AgentWorker[] = (team?.rooms ?? []).map((r, idx) => {
    const items = inboxByRoom.get(r.id) ?? [];
    const needsYou = r.status === 'waiting_user' || items.length > 0;
    const status = mapRoomStatus(r.status, needsYou);
    const mode: SessionMode = r.delegate ? 'auto' : 'suggest';
    const criteria = Array.isArray(r.criteria) ? r.criteria : [];
    const verifiedCount = criteria.filter((c) => c?.status === 'verified' || c?.status === 'pass' || Boolean(c?.done)).length;
    const activeSeat = r.impl_seat || r.impl_cli || r.seat || 'auto';
    const actText = String(r.activity || r.last_event || r.phase || r.status || '');
    return {
      id: r.id,
      name: r.name || r.slug || r.id,
      role: activeSeat,
      appName: activeSeat,
      windowTitle: r.goal || r.slug || r.id,
      category: 'terminal',
      status,
      activity: actText,
      helpPrompt: items[0]?.detail || items[0]?.text || items[0]?.title || r.error || '',
      quickActions: Array.isArray(items[0]?.options) ? items[0].options.map((o) => String(o?.label ?? o?.id ?? '')) : [],
      deskId: `desk-${(idx % 6) + 1}`,
      avatar: {
        hairStyle: 'short',
        hairColor: '#1e293b',
        shirtColor: needsYou ? '#f59e0b' : '#38bdf8',
        skinTone: '#f1c27d',
        accessory: 'none',
      },
      lastHeartbeat: r.updated_at || new Date().toISOString(),
      source: 'watcher',
      logs: [],
      pos: { x: 2 + (idx % 4) * 3, y: 3 + Math.floor(idx / 4) * 3 },
      path: [],
      facing: 'down',
      behaviorState: status === 'paused' ? 'at_lounge' : 'at_desk',
      celebrateUntil: 0,
      mode,
      phase: mapRoomPhase(r.phase || 'goal'),
      phaseLabel: r.phase || 'goal',
      goal: r.goal || '',
      verified: verifiedCount,
      core: criteria.length,
      statusLabel: needsYou ? items[0]?.title || '需要你處理' : actText,
      needsYou,
      pausedReason: r.error || '',
      acceptance: criteria.map((c) => ({
        text: String(c?.text ?? ''),
        status: String(c?.status || 'todo'),
        manual: !c?.check,
        checked: c?.status === 'verified' || c?.status === 'pass',
      })),
    };
  });
  return {
    workers,
    commands: [],
    lastUpdated: new Date().toISOString(),
    externalEventsCount: 0,
    running: !team?.paused_all,
  };
}

// 版面：
// - 寬螢幕（≥1024px）：左邊「像素辦公室＋開發流程看板」，右邊固定的詳情面板
// - 中小螢幕：辦公室可收合、看板改成單欄或雙欄；點卡片時詳情從右側滑出（手機全螢幕）
export default function PixelOfficeApp({ initialSnapshot }: PixelOfficeAppProps) {
  const safeInit: OfficeSnapshot = {
    workers: initialSnapshot?.workers ?? [],
    commands: initialSnapshot?.commands ?? [],
    lastUpdated: initialSnapshot?.lastUpdated ?? new Date().toISOString(),
    externalEventsCount: initialSnapshot?.externalEventsCount ?? 0,
    running: initialSnapshot?.running,
    usage: initialSnapshot?.usage,
  };
  const [snapshot, setSnapshot] = useState<OfficeSnapshot>(safeInit);
  const [filter, setFilter] = useState<Filter>('all');
  const [selectedId, setSelectedId] = useState<string | null>(
    () => safeInit.workers.find((w) => w.needsYou)?.id ?? safeInit.workers[0]?.id ?? null
  );
  const [panelOpen, setPanelOpen] = useState(false); // 只影響中小螢幕
  const [busy, setBusy] = useState(false);
  const [toastMsg, setToastMsg] = useState<string | null>(null);
  const [connected, setConnected] = useState(true);
  const [teamState, setTeamState] = useState<TeamState>({ rooms: [], inbox: [], fleet: {} });
  const [modelsView, setModelsView] = useState<TeamModelsView | null>(null);
  const [inboxOpen, setInboxOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [fleetOpen, setFleetOpen] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  const [preflightReport, setPreflightReport] = useState<PreflightReport | null>(null);
  const [budgetSummary, setBudgetSummary] = useState<BudgetSummary | null>(null);
  const [soundMuted, setSoundMuted] = useState<boolean>(() => soundFx.isMuted());
  const [ambientMode, setAmbientMode] = useState<AmbientSoundMode>(() => soundFx.getAmbientMode());
  const [gridMode, setGridMode] = useState<GridMode>('auto');
  const [selectedPersonaSlot, setSelectedPersonaSlot] = useState<PersonaSlotKey | null>(null);
  const prevNeedsRef = useRef<Set<string>>(new Set(safeInit.workers.filter((w) => w.needsYou).map((w) => w.id)));
  const prevDoneRef = useRef<Set<string>>(new Set(safeInit.workers.filter((w) => w.phase === 'done').map((w) => w.id)));

  const refreshPreflight = useCallback(async (autoOpenIfNeeded = false) => {
    const raw = await teamApi.preflight();
    if (!raw) return;
    const report = raw as unknown as PreflightReport;
    setPreflightReport(report);
    if (autoOpenIfNeeded && report.needs_setup) {
      try {
        if (!window.sessionStorage.getItem('pixel_office_setup_prompted_v1')) {
          window.sessionStorage.setItem('pixel_office_setup_prompted_v1', '1');
          setSetupOpen(true);
        }
      } catch {
        setSetupOpen(true);
      }
    }
  }, []);

  const refreshBudget = useCallback(async () => {
    const b = await teamApi.budget();
    if (b && b.ok) {
      setBudgetSummary(b);
    }
  }, []);

  useEffect(() => {
    void refreshPreflight(true);
    void refreshBudget();
    const timer = window.setInterval(() => {
      void refreshBudget();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [refreshPreflight, refreshBudget]);

  const handleCycleAudio = useCallback(() => {
    // 依序循環：微音效 -> 雨天細雨 -> 地爐柴火 -> 靜音 -> 微音效
    if (soundMuted) {
      soundFx.setMuted(false);
      soundFx.setAmbientMode('off');
      setSoundMuted(false);
      setAmbientMode('off');
      soundFx.playKeyClick();
      setToastMsg('🔊 已開啟微音效');
    } else if (ambientMode === 'off') {
      soundFx.setAmbientMode('rain');
      setAmbientMode('rain');
      setToastMsg('🌧️ 已開啟雨天細雨環境音');
    } else if (ambientMode === 'rain') {
      soundFx.setAmbientMode('hearth');
      setAmbientMode('hearth');
      setToastMsg('🔥 已開啟地爐柴火環境音');
    } else {
      soundFx.setAmbientMode('off');
      soundFx.setMuted(true);
      setAmbientMode('off');
      setSoundMuted(true);
      setToastMsg('🔇 已靜音');
    }
  }, [soundMuted, ambientMode]);

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
      const doneSet = new Set(next.workers.filter((w) => w.phase === 'done').map((w) => w.id));
      for (const id of doneSet) {
        if (!prevDoneRef.current.has(id)) {
          soundFx.playTaskCompleteChime();
          const target = next.workers.find((w) => w.id === id);
          if (target) showToast(`🎐 「${target.name}」任務已完成！`);
          break;
        }
      }
      prevDoneRef.current = doneSet;
      setSnapshot(next);
    },
    [showToast]
  );

  // 即時更新：SSE /api/stream (event: team)；斷線時改每 3 秒輪詢 /api/team/state
  const refreshModels = useCallback(async () => {
    const m = await teamApi.models();
    if (m) setModelsView(m);
  }, []);

  const applyTeamState = useCallback(
    (team: TeamState) => {
      setTeamState(team);
      applyIncomingSnapshot(teamStateToSnapshot(team));
    },
    [applyIncomingSnapshot]
  );

  useEffect(() => {
    let es: EventSource | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    teamApi.state().then((t) => {
      if (!disposed && t) applyTeamState(t);
    });
    teamApi.models().then((m) => {
      if (!disposed && m) {
        setModelsView(m);
        const seats = m.seats ?? {};
        const needsDetect = ['kiro', 'agy', 'claude'].some(
          (s) => seats[s] && (!seats[s].models || seats[s].models!.length === 0)
        );
        if (needsDetect) {
          void teamApi.detectModels('all').then(() => {
            setTimeout(() => {
              if (!disposed) void refreshModels();
            }, 1200);
          });
        }
      }
    });
    const stopPoll = () => {
      if (poll) clearInterval(poll);
      poll = null;
    };
    const startPoll = () => {
      if (poll || disposed) return;
      poll = setInterval(async () => {
        const next = await teamApi.state();
        setConnected(Boolean(next));
        if (next) applyTeamState(next);
      }, 3000);
    };
    const connect = () => {
      if (disposed) return;
      es = new EventSource('/api/stream');
      es.onopen = () => {
        setConnected(true);
        stopPoll();
      };
      es.addEventListener('team', (ev) => {
        try {
          applyTeamState(JSON.parse((ev as MessageEvent).data) as TeamState);
        } catch {
          // 忽略格式不對的訊息
        }
      });
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
  }, [applyTeamState, refreshModels]);

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
  const selectedRoom = useMemo(
    () => (teamState.rooms ?? []).find((r) => r.id === selected?.id) ?? null,
    [selected?.id, teamState.rooms]
  );
  const selectedInbox = useMemo(
    () =>
      (teamState.inbox ?? []).filter(
        (i) => i.room_id === selected?.id || (Array.isArray(i.rooms) && selected?.id && i.rooms.includes(selected.id))
      ),
    [selected?.id, teamState.inbox]
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
      else {
        const next = await teamApi.state();
        if (next) applyTeamState(next);
      }
      if (r.ok && okSound) soundFx.playAssistConfirm();
      showToast(r.text);
    } finally {
      setBusy(false);
    }
  };

  const onAssist = (id: string, label: string, message: string) =>
    run(() => teamApi.sendRoom(id, message || label), true);
  const onSetMode = (id: string, mode: SessionMode) =>
    run(() => teamApi.setRoomDelegate(id, mode === 'auto'));

  return (
    <div className="h-dvh w-screen flex flex-col bg-[#12141c] text-slate-100 overflow-hidden">
      <OfficeTopBar
        filter={filter}
        onChangeFilter={setFilter}
        counts={counts}
        running={running}
        connected={connected}
        onToggleRunning={() => run(() => officeApi.setRunning(!running))}
        inboxCount={(teamState.inbox ?? []).length}
        onOpenInbox={() => setInboxOpen(true)}
        onOpenWizard={() => setWizardOpen(true)}
        onOpenFleet={() => setFleetOpen(true)}
        onOpenSetup={() => {
          void refreshPreflight(false);
          setSetupOpen(true);
        }}
        needsSetup={Boolean(preflightReport?.needs_setup)}
        budget={budgetSummary}
        onSaveBudget={async (limit) => {
          const r = await teamApi.saveBudget(limit);
          if (r.ok) {
            showToast(limit > 0 ? `已設定每日成本護欄：${limit.toLocaleString()} tokens` : '已關閉每日 Token 上限');
            await refreshBudget();
          } else {
            showToast(r.text || '設定失敗');
          }
        }}
        soundMuted={soundMuted}
        ambientMode={ambientMode}
        onCycleAudio={handleCycleAudio}
      />

      <div className="flex-1 min-h-0 flex">
        <main className="flex-1 min-w-0 flex flex-col min-h-0 overflow-hidden">
          {!running && (
            <div className="mx-3 sm:mx-4 mt-3 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-xs text-amber-200 shrink-0">
              已全部暫停：助理不會觀察也不會操作任何視窗。按右上角「繼續運作」或主視窗的 F8 繼續。
            </div>
          )}

          {/* 房間總覽與格線切換列（自動 / 1x1 / 2x2 / 3x3 自動 Fit 畫面） */}
          <div className="px-3 sm:px-4 pt-2 pb-1 flex flex-wrap items-center justify-between gap-2 shrink-0">
            <div className="flex items-center gap-2">
              <span className="px-2.5 py-1 rounded-md bg-[#23293b] border border-[#2f374f] text-slate-100 text-[11px] font-semibold">
                房間總覽
              </span>
              {(teamState.archived_rooms?.length ?? 0) > 0 && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-[10px] text-slate-400">📦 已封存：</span>
                  {teamState.archived_rooms!.slice(0, 4).map((ar) => (
                    <button
                      key={ar.id}
                      type="button"
                      onClick={async () => {
                        const res = await teamApi.handoffRoom(ar.id);
                        showToast(res.text || '已從封存紀錄開啟新房間接續開發');
                        const next = await teamApi.state();
                        if (next) applyTeamState(next);
                        if (res.room?.id) select(res.room.id);
                      }}
                      className="px-2 py-0.5 rounded bg-[#151926] hover:bg-emerald-500/20 border border-[#2a3147] hover:border-emerald-500/40 text-slate-300 hover:text-emerald-300 text-[10px] flex items-center gap-1 cursor-pointer"
                      title={`從封存房間 ${ar.name} (${ar.id}) 接續開發開新房`}
                    >
                      <span className="truncate max-w-[130px]">{ar.name}</span>
                      <span className="text-emerald-300 font-bold">➕接續</span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div className="flex items-center gap-1 p-0.5 rounded-lg bg-[#0e1017] border border-[#232838] text-[11px]" role="group" aria-label="房間格線版面">
              {(
                [
                  ['auto', '自動'],
                  ['1x1', '1×1'],
                  ['2x2', '2×2'],
                  ['3x3', '3×3'],
                ] as const
              ).map(([gm, label]) => (
                <button
                  key={gm}
                  type="button"
                  onClick={() => setGridMode(gm)}
                  className={`px-2 py-1 rounded-md font-mono cursor-pointer transition-colors ${
                    gridMode === gm ? 'bg-sky-500/20 text-sky-300 font-semibold' : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <RoomGrid
            rooms={(teamState.rooms ?? []).filter((r) => visible.some((v) => v.id === r.id))}
            workers={visible}
            inbox={teamState.inbox ?? []}
            selectedId={selected?.id ?? null}
            selectedPersonaSlot={selectedPersonaSlot}
            gridMode={gridMode}
            onSelect={select}
            onSelectPersona={(rid, slot) => {
              select(rid);
              setSelectedPersonaSlot(slot);
            }}
            onToast={showToast}
            onOpenWizard={() => setWizardOpen(true)}
            onOpenSetup={() => {
              void refreshPreflight(false);
              setSetupOpen(true);
            }}
            onStartDemo={async () => {
              const res = await teamApi.startDemo('bold');
              showToast(res.text || '已啟動示範房間');
              const next = await teamApi.state();
              if (next) applyTeamState(next);
              if (res.room?.id) select(res.room.id);
            }}
            onAnswerInbox={async (itemId, ans, text) => {
              const r = await teamApi.answerInbox(itemId, ans, text || '');
              if (r.ok) soundFx.playAssistConfirm();
              showToast(r.text || '已送出審核回覆');
              const next = await teamApi.state();
              if (next) applyTeamState(next);
            }}
            onTogglePauseRoom={async (rid, paused) => {
              const res = paused ? await teamApi.resumeRoom(rid) : await teamApi.pauseRoom(rid);
              showToast(res.text);
              const next = await teamApi.state();
              if (next) applyTeamState(next);
            }}
          />
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
            fixed inset-y-0 right-0 z-40 w-full sm:w-[480px] transition-transform duration-200
            ${panelOpen ? 'translate-x-0' : 'translate-x-full'}
            lg:static lg:z-auto lg:translate-x-0 lg:w-[480px] xl:w-[540px] 2xl:w-[580px] lg:shrink-0`}
        >
          <SessionPanel
            worker={selected}
            room={selectedRoom}
            inboxItems={selectedInbox}
            modelsView={modelsView}
            selectedPersonaSlot={selectedPersonaSlot}
            onSelectPersonaSlot={setSelectedPersonaSlot}
            onClose={() => setPanelOpen(false)}
            onSelectRoom={(rid) => select(rid)}
            onAssist={onAssist}
            onSetMode={onSetMode}
            onRoomUpdated={async () => {
              const next = await teamApi.state();
              if (next) applyTeamState(next);
            }}
            onRefreshModels={refreshModels}
            onToast={showToast}
            busy={busy}
          />
        </aside>
      </div>

      {toastMsg && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-5 left-1/2 -translate-x-1/2 z-50 max-w-[90vw] px-4 py-2 rounded-lg bg-[#1d2333] border border-amber-400/50 text-xs font-medium text-slate-100 shadow-xl flex items-center gap-3"
        >
          <span className="select-text break-words">{toastMsg}</span>
          <button
            type="button"
            onClick={() => void copyTextToClipboard(toastMsg)}
            className="px-2 py-0.5 rounded bg-[#283149] hover:bg-[#343f5e] text-[10px] font-semibold text-amber-300 shrink-0 cursor-pointer"
            title="複製此通知訊息"
          >
            複製
          </button>
        </div>
      )}

      <ExceptionInbox
        open={inboxOpen}
        items={teamState.inbox ?? []}
        rooms={teamState.rooms ?? []}
        onClose={() => setInboxOpen(false)}
        onAnswer={async (id, ans) => {
          const r = await teamApi.answerInbox(id, ans);
          showToast(r.text);
          const next = await teamApi.state();
          if (next) applyTeamState(next);
        }}
        onSelectRoom={(rid) => select(rid)}
      />

      <DispatchWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        onCreated={async (rid) => {
          const next = await teamApi.state();
          if (next) applyTeamState(next);
          if (rid) select(rid);
        }}
        onToast={showToast}
      />

      <UsageFleetDrawer
        open={fleetOpen}
        fleet={teamState.fleet ?? {}}
        onClose={() => setFleetOpen(false)}
        onToast={showToast}
      />

      <SetupWizardModal
        open={setupOpen}
        report={preflightReport}
        onClose={() => setSetupOpen(false)}
        onRefresh={async () => {
          await refreshPreflight(false);
          await refreshModels();
        }}
        onStartDemo={async () => {
          const res = await teamApi.startDemo('bold');
          showToast(res.text || '已啟動 Demo 展示房間');
          const next = await teamApi.state();
          if (next) applyTeamState(next);
          if (res.room?.id) select(res.room.id);
        }}
        onToast={showToast}
      />
    </div>
  );
}
