'use client';

import React, { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  CircleDot,
  Eye,
  GitCommit,
  MessageSquare,
  Send,
  Sparkles,
  Target,
  X,
} from 'lucide-react';
import type { AgentWorker, SessionMode } from '@/lib/types';
import { MODE_META, PHASES, formatTime, modeOf, progressPct, statusTone } from '@/lib/ui';

interface SessionPanelProps {
  worker: AgentWorker | null;
  onClose?: () => void;
  onAssist: (workerId: string, actionLabel: string, message: string) => Promise<void>;
  onSetMode: (workerId: string, mode: SessionMode) => Promise<void>;
  busy: boolean;
}

function actionTone(label: string): string {
  const l = label.toLowerCase();
  if (/(deny|reject|skip|^no\b|拒絕|略過|取消)/.test(l)) return 'bg-rose-500/90 hover:bg-rose-400 text-white';
  if (/(always|一律|永遠|in this conversation)/.test(l)) return 'bg-[#23293b] hover:bg-[#2d354d] text-slate-300 border border-[#323a52]';
  if (/(allow|accept|run|yes|允許|送出|執行|同意|恢復)/.test(l)) return 'bg-amber-400 hover:bg-amber-300 text-slate-950';
  return 'bg-[#23293b] hover:bg-[#2d354d] text-slate-100 border border-[#323a52]';
}

const ACTION_LABEL: Record<string, string> = {
  request_spec: '請代理提出驗收 spec',
  revise_spec: '請代理修改驗收 spec',
  await_spec_confirm: '等你確認驗收 spec',
  request_evidence: '要代理提出證據',
  next_task: '推進下一個工作',
  self_decide: '請代理自行決定做法',
  answer: '回答代理的問題',
  rollover: '開新對話接續',
  ask_goal: '詢問最終目標',
  ask_human: '交給你決定',
  approve: '處理允許',
  wait: '等代理回應',
  done: '已達成',
  new_goal: '開始新目標',
};

export default function SessionPanel({ worker, onClose, onAssist, onSetMode, busy }: SessionPanelProps) {
  const [tab, setTab] = useState<'overview' | 'spec' | 'log'>('overview');
  const [text, setText] = useState('');

  if (!worker) {
    return (
      <div className="h-full flex items-center justify-center p-8 text-sm text-slate-500">
        點選辦公室裡的同事或下方的卡片
      </div>
    );
  }
  const w = worker;
  const mode = modeOf(w);
  const phaseIdx = Math.max(0, PHASES.findIndex((p) => p.id === (w.phase ?? 'goal')));
  const actions = w.quickActions ?? [];
  const acc = w.acceptance ?? [];

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim() || busy) return;
    await onAssist(w.id, '自訂協助指令', text.trim());
    setText('');
  };

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* 抬頭：名稱、模式、狀態 */}
      <div className="shrink-0 px-4 sm:px-5 pt-4 pb-3 border-b border-[#252a3a]">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="text-[11px] text-slate-500 font-mono truncate">{w.appName} · {w.id.split('|')[1] ?? ''}</div>
            <h2 className="text-lg font-bold text-slate-50 truncate">{w.name}</h2>
            <div className={`text-xs mt-0.5 flex items-center gap-1 ${statusTone(w)}`}>
              {w.needsYou ? <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" /> : <CircleDot className="w-3.5 h-3.5" aria-hidden="true" />}
              <span>{w.statusLabel}</span>
              <span className="text-slate-600">· 更新於 {formatTime(w.lastHeartbeat)}</span>
            </div>
          </div>
          {onClose && (
            <button type="button" onClick={onClose} aria-label="關閉詳情" className="p-1.5 rounded text-slate-400 hover:text-white hover:bg-[#23293b] cursor-pointer lg:hidden">
              <X className="w-5 h-5" aria-hidden="true" />
            </button>
          )}
        </div>

        <div className="mt-3 flex items-center gap-1 bg-[#0e1017] p-1 rounded-lg border border-[#232838]" role="radiogroup" aria-label="助理模式">
          {(['auto', 'suggest', 'off'] as SessionMode[]).map((m) => {
            const active = mode === m;
            const locked = Boolean(w.modeLocked) && m !== 'off';
            return (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={active}
                disabled={locked}
                title={locked ? '這是 AI 工程助理自己的專案，固定關閉' : undefined}
                onClick={() => !active && onSetMode(w.id, m)}
                className={`flex-1 px-2 py-1.5 text-xs font-semibold rounded-md transition-colors cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed ${
                  active ? MODE_META[m].on : 'text-slate-400 hover:text-slate-100'
                }`}
              >
                {MODE_META[m].label}
              </button>
            );
          })}
        </div>

        {/* 階段 */}
        <ol className="mt-3 grid grid-cols-4 gap-1" aria-label="開發階段">
          {PHASES.map((p, i) => (
            <li key={p.id} title={p.hint} className="min-w-0">
              <div className={`h-1 rounded ${i < phaseIdx ? 'bg-emerald-400' : i === phaseIdx ? 'bg-amber-400' : 'bg-[#262c3d]'}`} />
              <div className={`mt-1 text-[10px] truncate ${i === phaseIdx ? 'text-amber-200 font-semibold' : 'text-slate-500'}`}>
                {p.step} {p.label}
              </div>
            </li>
          ))}
        </ol>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {/* 需要你／可以做的事 */}
        {(w.helpPrompt || w.approvalCommand || actions.length > 0) && (
          <section className={`px-4 sm:px-5 py-4 border-b border-[#252a3a] ${w.needsYou ? 'bg-[#211b14]' : ''}`}>
            <h3 className="text-xs font-bold flex items-center gap-1.5 mb-2">
              {w.needsYou ? (
                <>
                  <AlertTriangle className="w-4 h-4 text-amber-400" aria-hidden="true" />
                  <span className="text-amber-300">需要你處理</span>
                </>
              ) : (
                <>
                  <Sparkles className="w-3.5 h-3.5 text-slate-400" aria-hidden="true" />
                  <span className="text-slate-300">可以做的事</span>
                </>
              )}
            </h3>
            {w.approvalCommand && (
              <>
                <div className="text-[11px] text-slate-400 mb-1">
                  代理想執行{mode === 'auto' && !w.needsYou ? '（自動駕駛會先由助理判斷）' : ''}：
                </div>
                <pre className="mb-3 text-[11px] leading-relaxed text-slate-200 bg-[#0e1017] border border-[#2a3044] rounded-md p-2.5 whitespace-pre-wrap break-all max-h-48 overflow-y-auto font-mono">
                  {w.approvalCommand}
                </pre>
              </>
            )}
            {w.helpPrompt && (
              <p className="text-sm text-amber-50/90 leading-relaxed mb-3 whitespace-pre-wrap break-words">{w.helpPrompt}</p>
            )}
            <div className="flex flex-wrap gap-2">
              {actions.map((a) => (
                <button
                  key={a}
                  type="button"
                  disabled={busy}
                  onClick={() => onAssist(w.id, a, '')}
                  className={`max-w-full px-3 py-1.5 rounded-md text-xs font-semibold transition-colors cursor-pointer disabled:opacity-50 text-left break-words ${actionTone(a)}`}
                >
                  {a}
                </button>
              ))}
            </div>
          </section>
        )}

        {/* 分頁 */}
        <div className="sticky top-0 z-10 bg-[#161923] px-4 sm:px-5 pt-3 border-b border-[#252a3a]">
          <div className="flex gap-4 text-xs" role="tablist">
            {(
              [
                ['overview', '概況'],
                ['spec', `驗收 ${w.core ? `${w.verified}/${w.core}` : ''}`],
                ['log', '紀錄'],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={`pb-2 border-b-2 -mb-px cursor-pointer ${tab === id ? 'border-amber-400 text-slate-50 font-semibold' : 'border-transparent text-slate-400 hover:text-slate-200'}`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {tab === 'overview' && (
          <div className="px-4 sm:px-5 py-4 space-y-4">
            <Field icon={<Target className="w-3.5 h-3.5" aria-hidden="true" />} label="目標">
              <p className="text-sm text-slate-100 leading-relaxed">{w.goal || '（尚未設定目標；自動駕駛會先推測）'}</p>
              {(w.core ?? 0) > 0 && (
                <div className="mt-2 flex items-center gap-2">
                  <div className="flex-1 h-1.5 rounded bg-[#23293b] overflow-hidden">
                    <div className="h-full bg-emerald-400" style={{ width: `${progressPct(w)}%` }} />
                  </div>
                  <span className="text-[11px] font-mono tabular-nums text-slate-400">{progressPct(w)}%</span>
                </div>
              )}
            </Field>

            <Field icon={<Eye className="w-3.5 h-3.5" aria-hidden="true" />} label="代理目前在做">
              <p className="text-sm text-slate-200 leading-relaxed">{w.activity}</p>
            </Field>

            {w.nextStep?.reason && (
              <Field icon={<Sparkles className="w-3.5 h-3.5" aria-hidden="true" />} label="助理的下一步">
                <p className="text-sm text-slate-200">
                  <span className="text-amber-200">{ACTION_LABEL[w.nextStep.action] ?? w.nextStep.action}</span>
                  <span className="text-slate-400">：{w.nextStep.reason}</span>
                </p>
                {w.nextStep.prompt && (
                  <pre className="mt-2 text-[11px] leading-relaxed text-slate-300 bg-[#0e1017] border border-[#242938] rounded-md p-2.5 whitespace-pre-wrap break-words max-h-40 overflow-y-auto font-mono">
                    {w.nextStep.prompt}
                  </pre>
                )}
                {mode === 'auto' && w.hourCap ? (
                  <div className="mt-1.5 text-[11px] text-slate-500 font-mono tabular-nums">
                    這小時自動送出 {w.hourSends ?? 0}/{w.hourCap}
                  </div>
                ) : null}
              </Field>
            )}

            {(w.lastIssues?.length ?? 0) > 0 && (
              <Field icon={<AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />} label="上次查核發現的問題">
                <ul className="text-xs text-amber-100/80 space-y-1 list-disc pl-4">
                  {w.lastIssues!.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              </Field>
            )}

            {(w.commits?.length ?? 0) > 0 && (
              <Field icon={<GitCommit className="w-3.5 h-3.5" aria-hidden="true" />} label="里程碑 commit">
                <ul className="space-y-1">
                  {w.commits!.map((c) => (
                    <li key={c.hash} className="text-xs flex gap-2 min-w-0">
                      <span className="font-mono text-sky-300 shrink-0">{c.hash.slice(0, 7)}</span>
                      <span className="text-slate-300 truncate">{c.desc}</span>
                      <span className="ml-auto shrink-0 text-[10px] text-slate-500">{c.check}</span>
                    </li>
                  ))}
                </ul>
              </Field>
            )}

            {w.chatTitle && (
              <Field icon={<MessageSquare className="w-3.5 h-3.5" aria-hidden="true" />} label="綁定的對話">
                <div className="font-mono text-xs text-slate-300 bg-[#0e1017] border border-[#242938] rounded-md px-2.5 py-1.5 break-all">
                  {w.chatTitle}
                </div>
              </Field>
            )}
          </div>
        )}

        {tab === 'spec' && (
          <div className="px-4 sm:px-5 py-4">
            {acc.length === 0 ? (
              <p className="text-sm text-slate-500">驗收條件還沒提出。到了「② 驗收 spec」階段，助理會請代理提出並審查。</p>
            ) : (
              <ul className="space-y-2">
                {acc.map((a, i) => {
                  const done = a.manual ? a.checked : a.status === 'verified';
                  return (
                    <li key={`${i}-${a.text}`} className="flex gap-2 text-xs leading-relaxed">
                      {done ? (
                        <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" aria-label="已查核" />
                      ) : a.status === 'claimed' ? (
                        <CircleDot className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" aria-label="代理宣稱完成，待查核" />
                      ) : (
                        <Circle className="w-4 h-4 text-slate-600 shrink-0 mt-0.5" aria-label="尚未完成" />
                      )}
                      <span className={done ? 'text-slate-400' : 'text-slate-200'}>
                        {a.manual && <span className="mr-1 text-[10px] px-1 rounded bg-[#23293b] text-slate-400">手動</span>}
                        {a.text}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            <p className="mt-4 text-[11px] text-slate-500">
              自動化條件由助理依測試報告與本機 git 查核；標「手動」的請到桌面版詳情雙擊打勾。
            </p>
          </div>
        )}

        {tab === 'log' && (
          <ul className="px-4 sm:px-5 py-4 space-y-2.5">
            {w.logs.length === 0 && <li className="text-sm text-slate-500">還沒有紀錄</li>}
            {w.logs.map((log) => (
              <li key={log.id} className="flex items-start gap-2.5 text-xs leading-relaxed">
                <span className="font-mono tabular-nums text-[11px] text-slate-500 shrink-0 mt-0.5">{formatTime(log.timestamp)}</span>
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
        )}
      </div>

      {/* 傳話給代理 */}
      <form onSubmit={send} className="shrink-0 flex gap-2 px-4 sm:px-5 py-3 border-t border-[#252a3a] bg-[#12151e]">
        <label htmlFor="agent-message" className="sr-only">
          傳話給代理
        </label>
        <input
          id="agent-message"
          type="text"
          value={text}
          disabled={mode === 'off'}
          onChange={(e) => setText(e.target.value)}
          placeholder={mode === 'off' ? '模式是「關閉」：助理不會操作這個視窗' : '傳話給代理（輸入到 IDE 聊天框並送出）'}
          className="flex-1 min-w-0 bg-[#0e1017] border border-[#2a3044] focus:border-amber-400 rounded-md px-3 py-2 text-xs text-slate-100 placeholder:text-slate-500 focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={!text.trim() || busy || mode === 'off'}
          aria-label="送出"
          className="px-3.5 py-2 rounded-md bg-slate-100 hover:bg-white disabled:bg-slate-700 disabled:text-slate-400 text-slate-950 text-xs font-semibold flex items-center gap-1.5 cursor-pointer shrink-0"
        >
          <Send className="w-3.5 h-3.5" aria-hidden="true" />
          <span className="hidden sm:inline">送出</span>
        </button>
      </form>
    </div>
  );
}

function Field({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] font-medium text-slate-400 mb-1 flex items-center gap-1">
        {icon}
        {label}
      </div>
      {children}
    </div>
  );
}
