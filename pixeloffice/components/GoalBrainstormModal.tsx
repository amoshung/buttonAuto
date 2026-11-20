'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Check, FileText, Send, Sparkles, Trash2, X } from 'lucide-react';
import { teamApi } from '@/lib/api';
import type {
  BrainstormMessage,
  BrainstormResponse,
  SaveBrainstormPlanResponse,
  TeamModelsView,
  TeamRoom,
} from '@/lib/types';
import { filterCallableSeatChoices, seatModelOptions } from './SessionPanel';

interface GoalBrainstormModalProps {
  open: boolean;
  room: TeamRoom;
  goalDraft: string;
  modelsView: TeamModelsView | null;
  onClose: () => void;
  onApplied: (res: SaveBrainstormPlanResponse) => Promise<void> | void;
  onToast: (msg: string) => void;
}

const STORAGE_PREFIX = 'pixeloffice.brainstorm.v1.';

export function pickDefaultBrainstormSeat(
  view: TeamModelsView | null | undefined,
  isDemo = false,
): { seat: string; model: string } {
  const choices = filterCallableSeatChoices(view).filter((c) => c.id !== 'auto');
  const hasGlm = isDemo || choices.some((c) => c.id === 'glm');
  if (hasGlm) {
    const glmOpts = seatModelOptions(view, 'glm');
    return { seat: 'glm', model: glmOpts[0]?.id || 'glm-5.3' };
  }
  const first = choices[0];
  if (first) {
    const opts = seatModelOptions(view, first.id);
    return { seat: first.id, model: opts[0]?.id || '' };
  }
  return { seat: 'auto', model: '' };
}

function loadSavedMessages(roomId: string): BrainstormMessage[] {
  if (typeof window === 'undefined' || !roomId) return [];
  try {
    const raw = window.localStorage.getItem(`${STORAGE_PREFIX}${roomId}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (m): m is BrainstormMessage =>
        Boolean(m) &&
        (m.role === 'user' || m.role === 'assistant') &&
        typeof m.content === 'string' &&
        m.content.trim().length > 0,
    );
  } catch {
    return [];
  }
}

function saveMessages(roomId: string, messages: BrainstormMessage[]): void {
  if (typeof window === 'undefined' || !roomId) return;
  try {
    if (messages.length === 0) {
      window.localStorage.removeItem(`${STORAGE_PREFIX}${roomId}`);
    } else {
      window.localStorage.setItem(`${STORAGE_PREFIX}${roomId}`, JSON.stringify(messages.slice(-40)));
    }
  } catch {
    // 忽略儲存配額錯誤
  }
}

export default function GoalBrainstormModal({
  open,
  room,
  goalDraft,
  modelsView,
  onClose,
  onApplied,
  onToast,
}: GoalBrainstormModalProps) {
  const callableChoices = useMemo(
    () => filterCallableSeatChoices(modelsView).filter((c) => c.id !== 'auto'),
    [modelsView],
  );
  const defaultSel = useMemo(
    () => pickDefaultBrainstormSeat(modelsView, Boolean(room.demo)),
    [modelsView, room.demo],
  );

  const [seat, setSeat] = useState<string>(defaultSel.seat);
  const [model, setModel] = useState<string>(defaultSel.model);
  const [messages, setMessages] = useState<BrainstormMessage[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [planDraft, setPlanDraft] = useState<{ filename: string; content: string } | null>(null);
  const listEndRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // 切換房間或開啟時載入該房間歷史紀錄與預設座位
  useEffect(() => {
    if (!open) return;
    setMessages(loadSavedMessages(room.id));
    setPlanDraft(null);
    setSeat((prev) => {
      const stillValid = callableChoices.some((c) => c.id === prev);
      return stillValid ? prev : defaultSel.seat;
    });
    setTimeout(() => inputRef.current?.focus(), 40);
  }, [open, room.id, callableChoices, defaultSel.seat]);

  useEffect(() => {
    if (!seat || seat === 'auto') {
      setModel('');
      return;
    }
    const opts = seatModelOptions(modelsView, seat);
    setModel((prev) => (opts.some((o) => o.id === prev) ? prev : opts[0]?.id || ''));
  }, [seat, modelsView]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) {
        onCloseRef.current();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, busy]);

  useEffect(() => {
    if (open && !planDraft) {
      listEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, open, planDraft]);

  if (!open) return null;

  const modelOptions = seatModelOptions(modelsView, seat, model);

  const updateMessages = (next: BrainstormMessage[]) => {
    setMessages(next);
    saveMessages(room.id, next);
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || busy) return;
    const nextMsgs: BrainstormMessage[] = [...messages, { role: 'user', content: text }];
    updateMessages(nextMsgs);
    setInput('');
    setBusy(true);
    try {
      const res = (await teamApi.brainstormRoom({
        id: room.id,
        messages: nextMsgs,
        mode: 'chat',
        seat,
        model,
        goal_draft: goalDraft,
      })) as BrainstormResponse;
      if (!res?.ok || !res.reply) {
        onToast(res?.text || 'Brainstorm 回覆失敗，請重試或切換 CLI／模型');
        return;
      }
      updateMessages([...nextMsgs, { role: 'assistant', content: res.reply }]);
    } catch (err) {
      onToast(err instanceof Error ? err.message : 'Brainstorm 連線失敗');
    } finally {
      setBusy(false);
    }
  };

  const handleGeneratePlan = async () => {
    if (busy) return;
    if (messages.length === 0 && !goalDraft.trim() && !room.goal.trim()) {
      onToast('請先輸入粗略目標或進行至少一輪討論再生成計畫');
      return;
    }
    setBusy(true);
    try {
      const res = (await teamApi.brainstormRoom({
        id: room.id,
        messages,
        mode: 'generate_plan',
        seat,
        model,
        goal_draft: goalDraft,
      })) as BrainstormResponse;
      if (!res?.ok || !res.content) {
        onToast(res?.text || '生成計畫失敗，請重試或切換其他 CLI／模型');
        return;
      }
      setPlanDraft({
        filename: res.filename || 'brainstorm-plan.md',
        content: res.content,
      });
    } catch (err) {
      onToast(err instanceof Error ? err.message : '生成計畫失敗');
    } finally {
      setBusy(false);
    }
  };

  const handleSavePlan = async () => {
    if (!planDraft || busy) return;
    if (!planDraft.filename.trim() || !planDraft.content.trim()) {
      onToast('計畫檔名與內容不可為空');
      return;
    }
    setBusy(true);
    try {
      const res = (await teamApi.saveBrainstormPlan({
        id: room.id,
        filename: planDraft.filename.trim(),
        content: planDraft.content,
        apply_goal: true,
      })) as SaveBrainstormPlanResponse;
      if (!res?.ok) {
        onToast(res?.text || '寫入計畫失敗');
        return;
      }
      await onApplied(res);
      onToast(`已寫入 ${res.plan_rel || 'docs/plans/'} 並套用總目標`);
      onClose();
    } catch (err) {
      onToast(err instanceof Error ? err.message : '寫入計畫失敗');
    } finally {
      setBusy(false);
    }
  };

  const handleClear = () => {
    if (busy) return;
    updateMessages([]);
    setPlanDraft(null);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="目標 Brainstorm 討論視窗"
    >
      <div className="flex h-[84vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-[#2a3147] bg-[#131722] text-slate-100 shadow-2xl">
        {/* Header + CLI/模型選擇列 */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#252c40] bg-[#171c2b] px-5 py-3.5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-amber-400/15 text-amber-300 border border-amber-400/30">
              <Sparkles className="h-4 w-4" />
            </span>
            <div>
              <h2 className="text-sm font-bold text-slate-100 flex items-center gap-2">
                <span>目標 Brainstorm 討論</span>
                <span className="rounded bg-[#232a3e] px-2 py-0.5 text-xs font-medium text-slate-300">
                  {room.name}
                </span>
              </h2>
              <p className="text-xs text-slate-400">
                先與 AI 來回釐清方向，隨時可按下「生成計畫」並預覽寫入 docs/plans/
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Brainstorm CLI 座位"
              value={seat}
              disabled={busy}
              onChange={(e) => setSeat(e.target.value)}
              className="rounded-lg border border-[#2b334a] bg-[#0e111a] px-2.5 py-1.5 text-xs text-slate-200 focus:border-amber-400 focus:outline-none"
            >
              {callableChoices.length === 0 && <option value="auto">自動選擇可用模型</option>}
              {callableChoices.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>

            {modelOptions.length > 0 && (
              <select
                aria-label="Brainstorm 模型"
                value={model}
                disabled={busy}
                onChange={(e) => setModel(e.target.value)}
                className="max-w-[200px] rounded-lg border border-[#2b334a] bg-[#0e111a] px-2.5 py-1.5 text-xs text-slate-200 focus:border-amber-400 focus:outline-none"
              >
                {modelOptions.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            )}

            {messages.length > 0 && !planDraft && (
              <button
                type="button"
                disabled={busy}
                onClick={handleClear}
                className="flex items-center gap-1 rounded-lg border border-[#2b334a] bg-[#1d2334] px-2.5 py-1.5 text-xs text-slate-300 hover:bg-[#262e44] cursor-pointer disabled:opacity-50"
                title="清空此房間的 Brainstorm 對話紀錄"
              >
                <Trash2 className="h-3.5 w-3.5" />
                <span>清空</span>
              </button>
            )}

            <button
              type="button"
              disabled={busy}
              onClick={onClose}
              aria-label="放棄並關閉 Brainstorm"
              title="放棄目前討論並關閉 Brainstorm 視窗（Esc）"
              className="ml-auto flex items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs font-semibold text-rose-200 hover:bg-rose-500/20 hover:border-rose-400 cursor-pointer disabled:opacity-50"
            >
              <X className="h-3.5 w-3.5" />
              <span>放棄並關閉</span>
            </button>
          </div>
        </div>

        {/* 粗略目標脈絡提示 */}
        {(goalDraft.trim() || room.repo) && (
          <div className="border-b border-[#202638] bg-[#10141f] px-5 py-2 text-xs text-slate-400 flex flex-wrap items-center gap-x-4 gap-y-1">
            {room.repo && (
              <span className="truncate max-w-[360px]" title={room.repo}>
                專案：<span className="text-slate-300 font-mono">{room.repo}</span>
              </span>
            )}
            {goalDraft.trim() && (
              <span className="truncate max-w-[460px]" title={goalDraft}>
                目前粗略目標：<span className="text-amber-300/90">{goalDraft.trim()}</span>
              </span>
            )}
          </div>
        )}

        {/* 主內容區：預覽計畫模式 vs 對話討論模式 */}
        {planDraft ? (
          <div className="flex flex-1 flex-col gap-3 overflow-hidden p-5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-sm font-semibold text-emerald-300">
                <FileText className="h-4 w-4" />
                <span>計畫草稿預覽（確認後將寫入專案 docs/plans/ 並套用總目標）</span>
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={() => setPlanDraft(null)}
                className="flex items-center gap-1.5 rounded-lg border border-[#2d354d] bg-[#1d2334] px-3 py-1.5 text-xs text-slate-200 hover:bg-[#272f45] cursor-pointer disabled:opacity-50"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
                <span>返回繼續討論</span>
              </button>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-xs font-mono text-slate-400 shrink-0">docs/plans/</span>
              <input
                type="text"
                value={planDraft.filename}
                disabled={busy}
                onChange={(e) => setPlanDraft({ ...planDraft, filename: e.target.value })}
                className="flex-1 rounded-lg border border-[#2a3147] bg-[#0c0f17] px-3 py-1.5 text-xs font-mono text-slate-100 focus:border-emerald-400 focus:outline-none"
              />
            </div>

            <textarea
              value={planDraft.content}
              disabled={busy}
              onChange={(e) => setPlanDraft({ ...planDraft, content: e.target.value })}
              className="flex-1 w-full resize-none rounded-xl border border-[#2a3147] bg-[#0c0f17] p-3.5 font-mono text-xs leading-relaxed text-slate-100 focus:border-emerald-400 focus:outline-none"
            />

            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => setPlanDraft(null)}
                className="rounded-lg bg-[#23293b] px-4 py-2 text-xs font-medium text-slate-300 hover:bg-[#2d354d] cursor-pointer disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={handleSavePlan}
                className="flex items-center gap-1.5 rounded-lg bg-emerald-500 px-4 py-2 text-xs font-bold text-slate-950 hover:bg-emerald-400 cursor-pointer disabled:opacity-50"
              >
                <Check className="h-4 w-4" />
                <span>{busy ? '寫入中…' : '確認寫入並套用計畫'}</span>
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
              {messages.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center text-center text-slate-400 space-y-2 px-6">
                  <Sparkles className="h-7 w-7 text-amber-300/80" />
                  <p className="text-sm font-medium text-slate-200">
                    從一個粗略想法開始，透過對話收斂成可執行的 CE 計畫
                  </p>
                  <p className="text-xs max-w-md leading-relaxed">
                    你可以描述想做的功能、痛點或限制條件；若上方已經填寫粗略目標，也可直接告訴 AI「幫我釐清這個目標」或直接點擊「生成計畫」。
                  </p>
                </div>
              ) : (
                messages.map((m, idx) => (
                  <div
                    key={idx}
                    className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}
                  >
                    <div
                      className={`max-w-[82%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words ${
                        m.role === 'user'
                          ? 'bg-amber-400/15 border border-amber-400/30 text-slate-100'
                          : 'bg-[#1a2030] border border-[#283047] text-slate-200'
                      }`}
                    >
                      {m.content}
                    </div>
                  </div>
                ))
              )}
              {busy && (
                <div className="flex justify-start">
                  <div className="rounded-2xl border border-[#283047] bg-[#1a2030] px-4 py-2.5 text-xs text-slate-400 animate-pulse">
                    正在思考並整理建議…
                  </div>
                </div>
              )}
              <div ref={listEndRef} />
            </div>

            <div className="border-t border-[#252c40] bg-[#171c2b] p-4 space-y-2.5">
              <div className="flex gap-2">
                <textarea
                  ref={inputRef}
                  rows={2}
                  value={input}
                  disabled={busy}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleSend();
                    }
                  }}
                  placeholder="輸入你的想法、補充條件或回答 AI 的問題…（Enter 送出，Shift+Enter 換行）"
                  className="flex-1 resize-none rounded-xl border border-[#2b334a] bg-[#0e111a] px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none"
                />
                <div className="flex flex-col gap-1.5 shrink-0">
                  <button
                    type="button"
                    disabled={busy || !input.trim()}
                    onClick={handleSend}
                    className="flex items-center justify-center gap-1.5 rounded-xl bg-amber-400 px-4 py-2 text-xs font-bold text-slate-950 hover:bg-amber-300 cursor-pointer disabled:opacity-50"
                  >
                    <Send className="h-3.5 w-3.5" />
                    <span>送出</span>
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={handleGeneratePlan}
                    className="flex items-center justify-center gap-1.5 rounded-xl border border-emerald-500/40 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25 cursor-pointer disabled:opacity-50"
                    title="隨時將目前討論收斂成 docs/plans/*.md 計畫草稿"
                  >
                    <Sparkles className="h-3.5 w-3.5" />
                    <span>生成計畫</span>
                  </button>
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
