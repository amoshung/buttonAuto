'use client';

import React, { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Coffee,
  Code2,
  Globe,
  Monitor,
  Palette,
  PauseCircle,
  Send,
  Sparkles,
  Target,
  Terminal,
  XCircle,
} from 'lucide-react';
import { getDeskSlotById } from '@/lib/office-map';
import type { AgentWorker, OfficeUsage, SessionMode, ToolCategory, WorkerStatus } from '@/lib/types';
import type { StatusFilter } from './OfficeTopBar';

interface WorkerDrawerProps {
  workers: AgentWorker[];
  filteredWorkers: AgentWorker[];
  selectedWorker: AgentWorker | null;
  activeFilter: StatusFilter;
  onChangeFilter: (f: StatusFilter) => void;
  onSelectWorker: (id: string) => void;
  onSubmitAssist: (workerId: string, actionLabel: string, message: string) => Promise<void>;
  onSetMode: (workerId: string, mode: SessionMode) => Promise<void>;
  submittingAssist: boolean;
  usage?: OfficeUsage;
}

const MODE_META: { id: SessionMode; label: string; active: string }[] = [
  { id: 'auto', label: '自動駕駛', active: 'bg-emerald-500 text-slate-950' },
  { id: 'suggest', label: '只建議', active: 'bg-sky-500 text-slate-950' },
  { id: 'off', label: '關閉', active: 'bg-slate-500 text-slate-950' },
];

// 允許、送出這類會讓代理繼續的動作用主色；拒絕、略過用紅色；其他用一般按鈕
function actionTone(label: string): 'go' | 'stop' | 'plain' {
  const l = label.toLowerCase();
  if (/(deny|reject|skip|^no\b|拒絕|略過|取消)/.test(l)) return 'stop';
  if (/(always|一律|永遠|in this conversation)/.test(l)) return 'plain';
  if (/(allow|accept|run|yes|允許|送出|執行|同意)/.test(l)) return 'go';
  return 'plain';
}

const TONE_CLASS = {
  go: 'bg-amber-400 hover:bg-amber-300 text-slate-950',
  stop: 'bg-rose-500/90 hover:bg-rose-400 text-white',
  plain: 'bg-[#23293b] hover:bg-[#2d354d] text-slate-100 border border-[#323a52]',
};

const CATEGORY_META: Record<
  ToolCategory,
  { label: string; Icon: React.ComponentType<{ className?: string }> }
> = {
  ide: { label: 'IDE 開發環境', Icon: Code2 },
  browser: { label: '瀏覽器視窗', Icon: Globe },
  terminal: { label: '終端機 / CLI', Icon: Terminal },
  design: { label: '設計與繪圖', Icon: Palette },
  other: { label: '其他工具', Icon: Monitor },
};

const STATUS_META: Record<
  WorkerStatus,
  {
    label: string;
    textClass: string;
    Icon: React.ComponentType<{ className?: string }>;
  }
> = {
  working: {
    label: '工作中',
    textClass: 'text-emerald-400',
    Icon: CheckCircle2,
  },
  needs_help: {
    label: '需要協助',
    textClass: 'text-amber-400',
    Icon: AlertTriangle,
  },
  idle: {
    label: '待機休息',
    textClass: 'text-sky-400',
    Icon: Coffee,
  },
  paused: {
    label: '已暫停',
    textClass: 'text-indigo-400',
    Icon: PauseCircle,
  },
  error: {
    label: '發生異常',
    textClass: 'text-rose-400',
    Icon: XCircle,
  },
};

function formatTime(iso: string) {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString('zh-TW', {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return '--:--:--';
  }
}

export default function WorkerDrawer({
  filteredWorkers,
  selectedWorker,
  activeFilter,
  onChangeFilter,
  onSelectWorker,
  onSubmitAssist,
  onSetMode,
  submittingAssist,
  usage,
}: WorkerDrawerProps) {
  const [replyText, setReplyText] = useState('');
  const [drawerTab, setDrawerTab] = useState<'inspect' | 'roster'>('inspect');

  const handleSendCustom = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedWorker || !replyText.trim() || submittingAssist) return;
    await onSubmitAssist(selectedWorker.id, '自訂協助指令', replyText.trim());
    setReplyText('');
  };

  const selectedStatus = selectedWorker ? STATUS_META[selectedWorker.status] : null;
  const selectedCategory = selectedWorker
    ? CATEGORY_META[selectedWorker.category]
    : null;
  const deskSlot = selectedWorker ? getDeskSlotById(selectedWorker.deskId) : null;

  return (
    <aside
      aria-label="視窗夥伴控制台"
      className="w-full lg:w-[400px] xl:w-[430px] shrink-0 bg-[#161923] border-t lg:border-t-0 lg:border-l border-[#252a3a] flex flex-col h-[46vh] lg:h-full overflow-hidden"
    >
      {/* Drawer Switch Tabs */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#252a3a] shrink-0">
        <div className="flex items-center gap-1 bg-[#0e1017] p-1 rounded-lg border border-[#232838]">
          <button
            type="button"
            onClick={() => setDrawerTab('inspect')}
            className={`px-3 py-1 text-xs font-medium rounded-md transition-colors whitespace-nowrap cursor-pointer ${
              drawerTab === 'inspect'
                ? 'bg-[#23293b] text-slate-100'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            夥伴詳情與協助
          </button>
          <button
            type="button"
            onClick={() => setDrawerTab('roster')}
            className={`px-3 py-1 text-xs font-medium rounded-md transition-colors whitespace-nowrap cursor-pointer ${
              drawerTab === 'roster'
                ? 'bg-[#23293b] text-slate-100'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            視窗總覽 ({filteredWorkers.length})
          </button>
        </div>

        {usage && (
          <div
            className="text-[11px] text-slate-400 font-mono tabular-nums whitespace-nowrap"
            title="今天：助理代按／自動送出／查核報告／交給你"
          >
            代按 {usage.today.auto_clicks} · 送出 {usage.today.auto_sends} · 查核 {usage.today.verifies}
          </div>
        )}
      </div>

      {/* Mobile-only filter strip (since top bar nav hides on <md) */}
      <div className="flex md:hidden items-center gap-1 px-4 py-2 border-b border-[#252a3a] overflow-x-auto">
        {(
          [
            { id: 'all', label: '全部' },
            { id: 'help', label: '需協助' },
            { id: 'working', label: '工作中' },
            { id: 'idle', label: '待機' },
          ] as const
        ).map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => onChangeFilter(f.id)}
            className={`px-2.5 py-1 text-xs rounded whitespace-nowrap cursor-pointer ${
              activeFilter === f.id
                ? 'bg-[#23293b] text-white font-medium'
                : 'text-slate-400'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Main Drawer Body */}
      <div className="flex-1 overflow-y-auto">
        {drawerTab === 'inspect' && selectedWorker && selectedStatus && selectedCategory ? (
          <div className="divide-y divide-[#222736]">
            {/* Worker Identity Section */}
            <section className="p-5">
              <div className="flex items-start justify-between gap-3 mb-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-xs text-slate-400 mb-1">
                    <selectedCategory.Icon className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                    <span className="truncate">{selectedWorker.appName}</span>
                    <span aria-hidden="true">·</span>
                    <span className="font-mono tabular-nums">{deskSlot?.label}</span>
                  </div>
                  <h2 className="text-base font-bold text-slate-50 truncate">
                    {selectedWorker.name}
                  </h2>
                  <p className="text-xs text-slate-400 mt-0.5">
                    {selectedWorker.role} · 更新於{' '}
                    <span
                      className="font-mono tabular-nums"
                      suppressHydrationWarning
                    >
                      {formatTime(selectedWorker.lastHeartbeat)}
                    </span>
                  </p>
                </div>

                <div
                  className={`flex items-center gap-1.5 text-xs font-semibold shrink-0 ${selectedStatus.textClass}`}
                >
                  <selectedStatus.Icon className="w-4 h-4" aria-hidden="true" />
                  <span>{selectedStatus.label}</span>
                </div>
              </div>

              {/* 模式：每個視窗一個 */}
              <div className="mt-3 flex items-center justify-between gap-2">
                <span className="text-[11px] font-medium text-slate-400">助理模式</span>
                <div className="flex items-center gap-1 bg-[#0e1017] p-1 rounded-lg border border-[#232838]">
                  {MODE_META.map((m) => {
                    const active = (selectedWorker.mode ?? 'off') === m.id;
                    return (
                      <button
                        key={m.id}
                        type="button"
                        disabled={selectedWorker.modeLocked && m.id !== 'off'}
                        title={selectedWorker.modeLocked ? '這是 AI 工程助理自己的專案，固定關閉' : undefined}
                        onClick={() => !active && onSetMode(selectedWorker.id, m.id)}
                        className={`px-2.5 py-1 text-xs font-semibold rounded-md transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed ${
                          active ? m.active : 'text-slate-400 hover:text-slate-100'
                        }`}
                      >
                        {m.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* 目標與階段 */}
              <div className="mt-3">
                <div className="text-[11px] font-medium text-slate-400 mb-1 flex items-center gap-1">
                  <Target className="w-3 h-3" aria-hidden="true" />
                  目標 · {selectedWorker.phaseLabel ?? '—'}
                </div>
                <p className="text-sm text-slate-100 leading-relaxed">
                  {selectedWorker.goal || '（尚未設定目標）'}
                </p>
                {(selectedWorker.core ?? 0) > 0 && (
                  <div className="mt-2">
                    <div className="h-1.5 rounded bg-[#23293b] overflow-hidden">
                      <div
                        className="h-full bg-emerald-400"
                        style={{
                          width: `${Math.round((100 * (selectedWorker.verified ?? 0)) / (selectedWorker.core ?? 1))}%`,
                        }}
                      />
                    </div>
                    <div className="text-[11px] text-slate-400 mt-1 font-mono tabular-nums">
                      驗收 {selectedWorker.verified}/{selectedWorker.core} 條有證據
                      {selectedWorker.mode === 'auto' && selectedWorker.hourCap
                        ? ` · 這小時自動送出 ${selectedWorker.hourSends ?? 0}/${selectedWorker.hourCap}`
                        : ''}
                    </div>
                  </div>
                )}
              </div>

              {/* Active Window Title */}
              <div className="mt-3">
                <div className="text-[11px] font-medium text-slate-400 mb-1">
                  綁定的對話
                </div>
                <div className="font-mono text-xs text-slate-200 bg-[#0e1017] border border-[#242938] rounded-md px-3 py-2 break-all">
                  {selectedWorker.windowTitle}
                </div>
              </div>

              {/* Current Activity */}
              <div className="mt-3">
                <div className="text-[11px] font-medium text-slate-400 mb-1">
                  代理目前在做
                </div>
                <p className="text-sm text-slate-200 leading-relaxed">
                  {selectedWorker.activity}
                </p>
              </div>
            </section>

            {/* Human-in-the-Loop Assistance Section */}
            <section
              className={`p-5 ${
                selectedWorker.status === 'needs_help' ||
                selectedWorker.status === 'error'
                  ? 'bg-[#211b14]'
                  : ''
              }`}
            >
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-xs font-bold tracking-wide text-slate-200 flex items-center gap-1.5">
                  {selectedWorker.status === 'needs_help' ||
                  selectedWorker.status === 'error' ? (
                    <>
                      <AlertTriangle
                        className="w-4 h-4 text-amber-400"
                        aria-hidden="true"
                      />
                      <span className="text-amber-300">需要您的協助與決策</span>
                    </>
                  ) : (
                    <>
                      <Sparkles
                        className="w-3.5 h-3.5 text-slate-400"
                        aria-hidden="true"
                      />
                      <span>遠端協作與指令介入</span>
                    </>
                  )}
                </h3>
              </div>

              {selectedWorker.helpPrompt ? (
                <p className="text-sm text-amber-100 leading-relaxed mb-3">
                  {selectedWorker.helpPrompt}
                </p>
              ) : (
                <p className="text-xs text-slate-400 leading-relaxed mb-3">
                  目前不需要你。可以在下面打字直接傳話給代理，或請助理開新對話接續。
                </p>
              )}

              {/* Quick Actions */}
              <div className="flex flex-wrap gap-2 mb-3">
                {(selectedWorker.quickActions ?? []).map((action) => (
                  <button
                    key={action}
                    type="button"
                    disabled={submittingAssist}
                    onClick={() => onSubmitAssist(selectedWorker.id, action, '')}
                    className={`px-3 py-1.5 rounded-md text-xs font-semibold transition-colors cursor-pointer disabled:opacity-50 text-left ${
                      TONE_CLASS[actionTone(action)]
                    }`}
                  >
                    {action}
                  </button>
                ))}
              </div>

              {/* Custom Instruction Input */}
              <form onSubmit={handleSendCustom} className="flex gap-2">
                <label htmlFor="assist-reply-input" className="sr-only">
                  輸入協助指令或回覆訊息
                </label>
                <input
                  id="assist-reply-input"
                  type="text"
                  value={replyText}
                  onChange={(e) => setReplyText(e.target.value)}
                  placeholder="打字傳話給代理（會輸入到 IDE 的聊天框並送出）..."
                  className="flex-1 min-w-0 bg-[#0e1017] border border-[#2a3044] focus:border-amber-400 rounded-md px-3 py-2 text-xs text-slate-100 placeholder:text-slate-500 focus:outline-none"
                />
                <button
                  type="submit"
                  disabled={!replyText.trim() || submittingAssist}
                  aria-label="送出協助指令"
                  className="px-3.5 py-2 rounded-md bg-slate-100 hover:bg-white disabled:bg-slate-700 disabled:text-slate-400 text-slate-950 text-xs font-semibold flex items-center gap-1.5 transition-colors cursor-pointer shrink-0"
                >
                  <Send className="w-3.5 h-3.5" aria-hidden="true" />
                  <span>送出</span>
                </button>
              </form>
            </section>

            {/* Activity Timeline */}
            <section className="p-5">
              <h3 className="text-xs font-semibold text-slate-400 mb-3">
                最近事件紀錄
              </h3>
              <ul className="space-y-2.5">
                {selectedWorker.logs.slice(0, 6).map((log) => (
                  <li
                    key={log.id}
                    className="flex items-start gap-2.5 text-xs leading-relaxed"
                  >
                    <span
                      className="font-mono tabular-nums text-[11px] text-slate-500 shrink-0 mt-0.5"
                      suppressHydrationWarning
                    >
                      {formatTime(log.timestamp)}
                    </span>
                    <span
                      className={
                        log.type === 'assisted'
                          ? 'text-emerald-300'
                          : log.type === 'help_request' || log.type === 'error'
                            ? 'text-amber-300'
                            : 'text-slate-300'
                      }
                    >
                      {log.message}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        ) : (
          /* Roster View (or fallback when no worker selected) */
          <div className="divide-y divide-[#222736]">
            {filteredWorkers.length === 0 ? (
              <div className="p-8 text-center">
                <CheckCircle2
                  className="w-8 h-8 text-emerald-400 mx-auto mb-2"
                  aria-hidden="true"
                />
                <p className="text-sm font-semibold text-slate-200">
                  目前沒有符合篩選條件的視窗
                </p>
                <p className="text-xs text-slate-400 mt-1 mb-4">
                  所有開著的 IDE 目前都運作順暢。
                </p>
                <button
                  type="button"
                  onClick={() => onChangeFilter('all')}
                  className="px-3.5 py-1.5 rounded-md bg-[#23293b] hover:bg-[#2d354d] text-xs font-medium text-slate-100 cursor-pointer"
                >
                  檢視全部視窗
                </button>
              </div>
            ) : (
              filteredWorkers.map((worker) => {
                const st = STATUS_META[worker.status];
                const isSelected = selectedWorker?.id === worker.id;
                return (
                  <button
                    key={worker.id}
                    type="button"
                    onClick={() => {
                      onSelectWorker(worker.id);
                      setDrawerTab('inspect');
                    }}
                    className={`w-full text-left px-5 py-3.5 transition-colors cursor-pointer flex items-start justify-between gap-3 ${
                      isSelected ? 'bg-[#1e2333]' : 'hover:bg-[#1a1e2b]'
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold text-slate-100 truncate">
                          {worker.name}
                        </span>
                        <span className="text-[11px] text-slate-500 font-mono truncate">
                          {worker.appName}
                        </span>
                      </div>
                      <p className="text-xs text-slate-400 truncate mt-0.5">
                        {worker.helpPrompt || worker.activity}
                      </p>
                    </div>
                    <span
                      className={`flex items-center gap-1 text-xs font-medium shrink-0 ${st.textClass}`}
                    >
                      <st.Icon className="w-3.5 h-3.5" aria-hidden="true" />
                      <span>{st.label}</span>
                    </span>
                  </button>
                );
              })
            )}
          </div>
        )}
      </div>

      {/* Compact Bottom Roster Strip when in Inspect tab for 1-click switching */}
      {drawerTab === 'inspect' && (
        <div className="border-t border-[#252a3a] bg-[#12151e] px-4 py-2.5 shrink-0">
          <div className="flex items-center justify-between text-[11px] text-slate-400 mb-2">
            <span>
              辦公室夥伴 ({filteredWorkers.length}) · 點擊切換焦點
            </span>
            <button
              type="button"
              onClick={() => setDrawerTab('roster')}
              className="text-amber-300 hover:text-amber-200 cursor-pointer"
            >
              展開列表
            </button>
          </div>
          {filteredWorkers.length === 0 ? (
            <div className="text-xs text-slate-400 py-1 flex items-center justify-between">
              <span>目前無符合條件的夥伴</span>
              <button
                type="button"
                onClick={() => onChangeFilter('all')}
                className="text-amber-300 hover:underline cursor-pointer"
              >
                顯示全部
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-1.5 overflow-x-auto pb-0.5">
              {filteredWorkers.map((w) => {
                const isSel = selectedWorker?.id === w.id;
                const isAlert = w.status === 'needs_help' || w.status === 'error';
                return (
                  <button
                    key={w.id}
                    type="button"
                    onClick={() => onSelectWorker(w.id)}
                    className={`px-2.5 py-1.5 rounded-md text-xs font-medium whitespace-nowrap shrink-0 flex items-center gap-1.5 border transition-colors cursor-pointer ${
                      isSel
                        ? 'bg-[#252c40] border-amber-400/70 text-white'
                        : isAlert
                          ? 'bg-[#261d14] border-amber-500/50 text-amber-200 hover:bg-[#302418]'
                          : 'bg-[#181c28] border-[#262c3d] text-slate-300 hover:text-white'
                    }`}
                  >
                    {isAlert && (
                      <AlertTriangle
                        className="w-3 h-3 text-amber-400 shrink-0"
                        aria-hidden="true"
                      />
                    )}
                    <span>{w.name}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}
