'use client';

import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Check, Copy, GitMerge, KeyRound, MessageSquareWarning, RefreshCw, ShieldAlert, X } from 'lucide-react';
import type { TeamInboxItem, TeamInboxKind, TeamRoom } from '@/lib/types';
import { copyTextToClipboard } from '@/lib/ui';

interface ExceptionInboxProps {
  open: boolean;
  items: TeamInboxItem[];
  rooms: TeamRoom[];
  onClose: () => void;
  onAnswer: (itemId: string, answer: string) => Promise<void>;
  onSelectRoom: (roomId: string) => void;
}

const KIND_META: Record<TeamInboxKind, { label: string; badge: string; icon: React.ReactNode }> = {
  approve_spec: { label: '規格確認', badge: 'bg-sky-500/15 text-sky-300 border-sky-500/30', icon: <Check className="w-3.5 h-3.5" /> },
  approve_plan: { label: '計畫確認', badge: 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30', icon: <Check className="w-3.5 h-3.5" /> },
  permission: { label: '權限核准', badge: 'bg-amber-500/15 text-amber-300 border-amber-500/30', icon: <ShieldAlert className="w-3.5 h-3.5" /> },
  question: { label: '問題回覆', badge: 'bg-purple-500/15 text-purple-300 border-purple-500/30', icon: <MessageSquareWarning className="w-3.5 h-3.5" /> },
  merge: { label: '合併確認', badge: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30', icon: <GitMerge className="w-3.5 h-3.5" /> },
  retry: { label: '重試決策', badge: 'bg-orange-500/15 text-orange-300 border-orange-500/30', icon: <RefreshCw className="w-3.5 h-3.5" /> },
  error: { label: '錯誤處理', badge: 'bg-rose-500/15 text-rose-300 border-rose-500/30', icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  unverified: { label: '待驗證項', badge: 'bg-amber-500/15 text-amber-300 border-amber-500/30', icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  stuck: { label: '進度卡住', badge: 'bg-orange-500/15 text-orange-300 border-orange-500/30', icon: <RefreshCw className="w-3.5 h-3.5" /> },
  recommend: { label: '建議決策', badge: 'bg-sky-500/15 text-sky-300 border-sky-500/30', icon: <MessageSquareWarning className="w-3.5 h-3.5" /> },
  login: { label: '登入／網路', badge: 'bg-rose-500/15 text-rose-300 border-rose-500/30', icon: <KeyRound className="w-3.5 h-3.5" /> },
};

const DEFAULT_OPTS: Record<TeamInboxKind, Array<{ id: string; label: string }>> = {
  approve_spec: [{ id: 'start', label: '核准規格並開始' }, { id: 'revise', label: '退回修改' }],
  approve_plan: [{ id: 'start', label: '核准計畫並開始' }, { id: 'revise', label: '退回修改' }],
  permission: [{ id: 'allow', label: '允許一次' }, { id: 'reject', label: '拒絕' }],
  question: [{ id: 'continue', label: '繼續執行' }],
  merge: [{ id: 'merge', label: '確認合併' }, { id: 'skip', label: '暫不合併' }],
  retry: [{ id: 'retry', label: '再試一次' }, { id: 'stop', label: '暫停房間' }],
  error: [{ id: 'retry', label: '再試一次' }, { id: 'stop', label: '暫停房間' }],
  unverified: [{ id: 'accept', label: '接受並繼續' }, { id: 'strengthen', label: '要求補強測試' }],
  stuck: [{ id: 'retry', label: '再試一次' }, { id: 'stop', label: '暫停房間' }],
  recommend: [{ id: 'accept', label: '採納建議' }],
  login: [{ id: 'retry', label: '我已登入，重試' }, { id: 'pause', label: '先暫停' }],
};

export default function ExceptionInbox({ open, items, rooms, onClose, onAnswer, onSelectRoom }: ExceptionInboxProps) {
  const [customText, setCustomText] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string>('');
  const [copiedId, setCopiedId] = useState<string>('');
  const prevFocusRef = useRef<HTMLElement | null>(null);
  const closeBtnRef = useRef<HTMLButtonElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const handleCopy = async (id: string, content: string) => {
    const ok = await copyTextToClipboard(content);
    if (ok) {
      setCopiedId(id);
      setTimeout(() => setCopiedId((prev) => (prev === id ? '' : prev)), 2000);
    }
  };

  useEffect(() => {
    if (open) {
      prevFocusRef.current = document.activeElement as HTMLElement | null;
      const focusTimer = setTimeout(() => closeBtnRef.current?.focus(), 0);
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onCloseRef.current();
        }
      };
      window.addEventListener('keydown', onKey);
      return () => {
        clearTimeout(focusTimer);
        window.removeEventListener('keydown', onKey);
      };
    } else if (prevFocusRef.current) {
      prevFocusRef.current.focus();
      prevFocusRef.current = null;
    }
  }, [open]);

  if (!open) return null;

  const roomById = new Map(rooms.map((r) => [r.id, r]));

  const submit = async (itemId: string, ans: string) => {
    if (!ans.trim()) return;
    setBusyId(itemId);
    try {
      await onAnswer(itemId, ans.trim());
      setCustomText((prev) => ({ ...prev, [itemId]: '' }));
    } finally {
      setBusyId('');
    }
  };

  return (
    <div
      className="fixed inset-0 z-40 bg-black/60 backdrop-blur-xs flex justify-end"
      role="dialog"
      aria-modal="true"
      aria-label="例外收件匣"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg h-full bg-[#141720] border-l border-[#262b3d] flex flex-col shadow-2xl">
        <div className="px-5 py-4 border-b border-[#252a3a] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-400" aria-hidden="true" />
            <h2 className="text-sm font-bold text-slate-100">例外收件匣</h2>
            <span className="text-xs px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 font-mono">
              {items.length}
            </span>
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-100 hover:bg-[#23293b] cursor-pointer"
            aria-label="關閉例外收件匣"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {items.length === 0 ? (
            <div className="text-center py-12 text-xs text-slate-400">
              目前沒有需要人工介入的例外項目。
            </div>
          ) : (
            items.map((item) => {
              const meta = KIND_META[item.kind] ?? KIND_META.question;
              const room = roomById.get(item.room_id);
              const opts = item.options && item.options.length > 0 ? item.options : DEFAULT_OPTS[item.kind] ?? [];
              const disabled = busyId === item.id;
              const fullCopyText = [
                `[${room?.name || room?.slug || item.room_id}] ${item.title || meta.label}`,
                item.text || '',
                item.detail || '',
              ]
                .filter(Boolean)
                .join('\n\n');
              const isCopied = copiedId === item.id;
              return (
                <div key={item.id} className="rounded-xl bg-[#1a1e2b] border border-[#2a3044] p-4 space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[11px] font-medium ${meta.badge}`}>
                      {meta.icon}
                      {meta.label}
                    </span>
                    <div className="flex items-center gap-2 min-w-0">
                      <button
                        type="button"
                        onClick={() => void handleCopy(item.id, item.detail || item.text || fullCopyText)}
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[11px] font-semibold cursor-pointer shrink-0 transition-colors ${
                          isCopied
                            ? 'bg-emerald-500/20 border-emerald-400/50 text-emerald-300'
                            : 'bg-[#121622] hover:bg-[#23293b] border-[#2d354d] text-slate-200 hover:text-amber-300'
                        }`}
                        title="複製此錯誤或系統訊息到剪貼簿，方便貼到其他地方除錯"
                      >
                        <Copy className="w-3 h-3" />
                        <span>{isCopied ? '已複製' : '複製訊息'}</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          onSelectRoom(item.room_id);
                          onClose();
                        }}
                        className="text-[11px] text-slate-400 hover:text-sky-300 truncate max-w-[160px] cursor-pointer"
                      >
                        {room?.slug || item.room_id}
                      </button>
                    </div>
                  </div>
                  <div className="text-xs font-semibold text-slate-100">{item.title || meta.label}</div>
                  {item.text && <div className="text-xs text-slate-300 whitespace-pre-wrap break-words">{item.text}</div>}
                  {item.detail && (
                    <div className="relative">
                      <pre className="text-[11px] leading-relaxed font-mono bg-[#0e1017] border border-[#232838] rounded-lg p-2.5 text-slate-200 whitespace-pre-wrap break-words overflow-y-auto max-h-48 select-text">
                        {item.detail}
                      </pre>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-2 pt-1">
                    {opts.map((opt) => (
                      <button
                        key={opt.id}
                        type="button"
                        disabled={disabled}
                        onClick={() => submit(item.id, opt.id)}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-[#252c40] hover:bg-[#313a54] text-slate-100 disabled:opacity-50 cursor-pointer"
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                  <div className="flex gap-2 pt-1">
                    <input
                      type="text"
                      value={customText[item.id] ?? ''}
                      onChange={(e) => setCustomText((prev) => ({ ...prev, [item.id]: e.target.value }))}
                      placeholder="或輸入自訂指示…"
                      className="flex-1 px-2.5 py-1.5 rounded-lg bg-[#0e1017] border border-[#252a3a] text-xs text-slate-100 placeholder:text-slate-500"
                    />
                    <button
                      type="button"
                      disabled={disabled || !(customText[item.id] ?? '').trim()}
                      onClick={() => submit(item.id, customText[item.id] ?? '')}
                      className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-sky-500 hover:bg-sky-400 text-slate-950 disabled:opacity-40 cursor-pointer"
                    >
                      送出
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
