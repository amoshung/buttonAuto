'use client';

import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, CircleDot, Copy, Pause, Play, Plus, Shuffle, Sparkles, X } from 'lucide-react';
import type { AgentWorker, InboxItem, TeamRoom } from '@/lib/types';
import { PHASES, copyTextToClipboard, formatTime, progressPct, statusTone } from '@/lib/ui';
import {
  CHARACTER_ROSTER,
  LH,
  LW,
  PIXEL_SCALE,
  ROOM_THEMES,
  type PersonaSlotKey,
  type RoomPersona,
  buildRoomScene,
  cycleRoomCast,
  drawEmptyRoomCanvas,
  drawRoomCanvas,
  getRoomThemeOverride,
  registerVisibleRooms,
  setRoomThemeOverride,
} from '@/lib/pixelRoomScene';
import {
  getRoomVistaOverride,
  getTimeOfDayOverride,
  setRoomVistaOverride,
  setTimeOfDayOverride,
  type TimeOfDayOverride,
  type WindowVistaSelection,
} from '@/lib/roomAtmosphere';

export type GridLayoutMode = 'auto' | '1x1' | '2x2' | '3x3';
export type GridMode = GridLayoutMode;

interface RoomGridProps {
  workers: AgentWorker[];
  rooms: TeamRoom[];
  inbox?: InboxItem[];
  selectedId: string | null;
  selectedPersonaSlot?: PersonaSlotKey | null;
  onSelect: (id: string) => void;
  onSelectPersona?: (roomId: string, slot: PersonaSlotKey | null) => void;
  onToast?: (msg: string) => void;
  gridMode: GridLayoutMode;
  onOpenWizard: () => void;
  onStartDemo?: () => void;
  onOpenSetup?: () => void;
  onTogglePauseRoom?: (roomId: string, paused: boolean) => void;
  onAnswerInbox?: (itemId: string, answer: string, text?: string) => Promise<void>;
}

function PixelRoomCanvas({
  room,
  inboxItems,
  selected,
  selectedPersonaSlot,
  onClickPersona,
  onToast,
  compact,
}: {
  room: TeamRoom;
  inboxItems: InboxItem[];
  selected: boolean;
  selectedPersonaSlot?: PersonaSlotKey | null;
  onClickPersona?: (slot: PersonaSlotKey | null) => void;
  onToast?: (msg: string) => void;
  compact: boolean;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [livePersonas, setLivePersonas] = useState<RoomPersona[]>(() => buildRoomScene(room, inboxItems, Date.now()).personas);

  useEffect(() => {
    let raf = 0;
    let lastDraw = 0;
    let lastOverlay = 0;
    let disposed = false;
    let skippedByViewport = false;
    // 使用 6 倍高解析度渲染（1440×810），完整呈現 1376×768 原生背景與 240×240 角色的精細筆觸
    const scale = Math.max(PIXEL_SCALE, 6);

    const render = (now: number) => {
      if (disposed || skippedByViewport) return;
      if (now - lastDraw >= 38) {
        lastDraw = now;
        const wallNow = Date.now();
        const curScene = buildRoomScene(room, inboxItems, wallNow, true);
        const cv = canvasRef.current;
        if (cv) {
          if (cv.width !== LW * scale) cv.width = LW * scale;
          if (cv.height !== LH * scale) cv.height = LH * scale;
          const ctx = cv.getContext('2d');
          if (ctx) {
            ctx.setTransform(scale, 0, 0, scale, 0, 0);
            ctx.imageSmoothingEnabled = true;
            ctx.imageSmoothingQuality = 'high';
            drawRoomCanvas(ctx, room, curScene, wallNow, selected, selected ? selectedPersonaSlot : null);
          }
        }
        if (now - lastOverlay >= 110) {
          lastOverlay = now;
          setLivePersonas(curScene.personas);
        }
      }
      raf = requestAnimationFrame(render);
    };

    const wrapEl = wrapRef.current;
    const handleVisibilityChange = (ev: Event) => {
      const skipped = Boolean((ev as Event & { skipped?: boolean }).skipped);
      skippedByViewport = skipped;
      if (skipped) {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
      } else if (!disposed && !raf) {
        raf = requestAnimationFrame(render);
      }
    };
    wrapEl?.addEventListener('contentvisibilityautostatechange', handleVisibilityChange);

    raf = requestAnimationFrame(render);
    return () => {
      disposed = true;
      wrapEl?.removeEventListener('contentvisibilityautostatechange', handleVisibilityChange);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [room, inboxItems, selected, selectedPersonaSlot]);

  const handleCanvasClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const cv = canvasRef.current;
    if (!cv || !onClickPersona) return;
    const rect = cv.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const lx = ((e.clientX - rect.left) / rect.width) * LW;
    const ly = ((e.clientY - rect.top) / rect.height) * LH;

    // 檢查是否點中任一 45° 等距像素角色（人物身體中心約為 x, y - 11）
    let best: { slot: PersonaSlotKey; dist: number } | null = null;
    for (const p of livePersonas) {
      const dx = Math.abs(lx - p.x);
      const dy = Math.abs(ly - (p.y - 11));
      if (dx <= 14 && dy <= 16) {
        const dist = Math.hypot(dx, dy);
        if (!best || dist < best.dist) {
          best = { slot: p.slotKey, dist };
        }
      }
    }
    if (best) {
      e.stopPropagation();
      onClickPersona(selected && selectedPersonaSlot === best.slot ? null : best.slot);
    }
  };

  const inspectedPersona =
    selected && selectedPersonaSlot ? livePersonas.find((p) => p.slotKey === selectedPersonaSlot) || null : null;

  return (
    <div
      ref={wrapRef}
      style={{ contentVisibility: 'auto', containIntrinsicSize: 'auto 300px', containerType: 'size' }}
      className="relative flex-1 min-h-0 w-full bg-[#c89d66] overflow-hidden select-none border-b border-[#3a2a1e] flex items-center justify-center"
    >
      {/* 透過 CSS Container Query 精準鎖定 16:9 原始等距比例，確保 1x1 / 2x2 / 3x3 房間圖片絕不被壓扁或拉扯，且對話框 100% 對齊人物頭頂 */}
      <div
        style={{
          width: 'min(100cqw, calc(100cqh * 16 / 9))',
          height: 'min(100cqh, calc(100cqw * 9 / 16))',
        }}
        className="relative m-auto shrink-0"
      >
        <canvas
          ref={canvasRef}
          onClick={handleCanvasClick}
          className="w-full h-full block cursor-pointer"
          aria-label={`${room.name} 像素辦公室畫布（點擊像素人物可查看最新五項工作紀錄）`}
        />

        {/* 房間內三位人類工作角色的精準貼身 Hover 互動區：平常 100% 隱藏姓名與對話框；滑鼠移到人物身上時，在該人物頭頂正上方緊貼浮現姓名與可點擊對話框 */}
        <div className="absolute inset-0 pointer-events-none">
          {livePersonas.map((persona) => {
            // 精準對齊該人物的實際腳底中心點 (persona.x, persona.y)，零偏移錯位，絕不會搞混哪個對話框屬於誰！
            const leftPct = Math.max(6, Math.min(94, (persona.x / LW) * 100));
            const footTopPct = Math.max(14, Math.min(96, (persona.y / LH) * 100));
            const isPinned = selected && selectedPersonaSlot === persona.slotKey;

            const statusIcon =
              persona.bubbleTone === 'err'
                ? '⚠️'
                : persona.bubbleTone === 'wait'
                  ? '❓'
                  : persona.pose === 'type'
                    ? '⚡'
                    : persona.pose === 'write' || persona.isWorking
                      ? '📋'
                      : persona.bubbleTone === 'ok'
                        ? '✨'
                        : '☕';

            const statusLabelShort =
              persona.bubbleTone === 'err'
                ? '發生錯誤'
                : persona.bubbleTone === 'wait'
                  ? '等待決定'
                  : persona.isWorking
                    ? '工作中'
                    : '待命中';

            const bubbleAccent =
              persona.bubbleTone === 'err'
                ? 'border-rose-400/90 bg-[#1f1218]/92 text-rose-100'
                : persona.bubbleTone === 'wait'
                  ? 'border-amber-400/95 bg-[#1e1910]/92 text-amber-100'
                  : isPinned
                    ? 'border-amber-400 bg-[#121726]/95 text-white ring-2 ring-amber-400/35'
                    : 'border-amber-300/75 bg-[#121726]/92 text-slate-100 hover:border-amber-300';

            return (
              <div
                key={persona.slotKey}
                style={{ left: `${leftPct}%`, top: `${footTopPct}%` }}
                className={`group/persona absolute -translate-x-1/2 -translate-y-full flex flex-col items-center transition-[left,top] duration-100 ease-linear ${
                  isPinned ? 'z-30' : 'z-10 hover:z-30'
                }`}
              >
                {/* 浮動姓名＋可點擊對話框：平常完全隱藏（opacity-0），滑鼠移到人物身上或已點選時緊貼人物頭頂正上方浮現 */}
                <div
                  className={`mb-1 flex flex-col items-center transition-all duration-150 origin-bottom ${
                    isPinned
                      ? 'opacity-100 scale-100 pointer-events-auto'
                      : 'opacity-0 scale-95 pointer-events-none group-hover/persona:opacity-100 group-hover/persona:scale-100 group-hover/persona:pointer-events-auto'
                  }`}
                >
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onClickPersona?.(isPinned ? null : persona.slotKey);
                    }}
                    className={`cursor-pointer relative rounded-xl border px-2.5 py-1.5 shadow-2xl backdrop-blur-md text-left transition-transform hover:scale-[1.03] ${
                      compact ? 'max-w-[190px]' : 'min-w-[140px] max-w-[230px]'
                    } ${bubbleAccent}`}
                    title={isPinned ? '點擊收合工作紀錄' : `點擊查看 ${persona.roleTitle} 目前在做什麼`}
                  >
                    {/* 第一行：角色姓名 ＋ 狀態膠囊 */}
                    <div className="flex items-center justify-between gap-2 text-[11px] font-bold leading-tight">
                      <span className="flex items-center gap-1 truncate text-white">
                        <span
                          className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
                          style={{ backgroundColor: persona.badgeColor }}
                        />
                        <span className="truncate">{persona.roleTitle}</span>
                      </span>
                      <span className="text-[10px] font-semibold opacity-80 shrink-0">
                        {statusIcon} {statusLabelShort}
                      </span>
                    </div>

                    {/* 第二行：簡短對話框內容 ＋ 點擊引導 */}
                    <div className="mt-1 text-[11px] leading-snug text-slate-200/95 line-clamp-2">
                      {persona.bubble || '待命休息中'}
                    </div>
                    <div className="mt-1 text-[9.5px] font-semibold text-amber-300/90 flex items-center justify-end gap-0.5">
                      <span>{isPinned ? '點擊收合' : '點擊查看在做什麼 ▸'}</span>
                    </div>

                    {/* 精準指向該人物頭頂正中央的對話框小尖角 */}
                    <span
                      aria-hidden="true"
                      className="absolute left-1/2 -bottom-[5px] -translate-x-1/2 w-2.5 h-2.5 rotate-45 bg-[#121726] border-r border-b border-amber-300/75"
                    />
                  </button>
                </div>

                {/* 精準覆蓋像素人物身體的透明 Hover／點擊感應區（底部對齊腳底，滑鼠移上人物立即高亮腳底微光並浮現對話框） */}
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onClickPersona?.(isPinned ? null : persona.slotKey);
                  }}
                  className={`pointer-events-auto cursor-pointer relative flex items-end justify-center bg-transparent border-0 p-0 focus:outline-none ${
                    compact ? 'w-9 h-12' : 'w-14 h-[74px] sm:w-16 sm:h-[86px]'
                  }`}
                  aria-label={`檢視 ${persona.roleTitle} 的工作狀態`}
                >
                  <span
                    aria-hidden="true"
                    className={`w-10 h-3 rounded-full border transition-opacity duration-150 ${
                      isPinned
                        ? 'opacity-90 border-amber-300 bg-amber-400/25'
                        : 'opacity-0 group-hover/persona:opacity-90 border-amber-300/80 bg-amber-300/20'
                    }`}
                  />
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* 點擊像素人彈出的「最新五項工作紀錄（DESC）」浮窗 */}
      {inspectedPersona && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="absolute bottom-2 left-2 right-2 sm:left-auto sm:right-2 sm:w-[360px] z-20 rounded-xl bg-[#0e121d]/95 backdrop-blur-md border border-amber-400/70 shadow-2xl p-3 text-xs space-y-2"
        >
          <div className="flex items-start justify-between gap-2 border-b border-[#252d44] pb-1.5">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span
                  className="px-1.5 py-0.5 rounded text-[10px] font-bold text-slate-950 shrink-0"
                  style={{ backgroundColor: inspectedPersona.badgeColor }}
                >
                  {inspectedPersona.tierLabel}
                </span>
                <span className="font-bold text-slate-100 truncate">{inspectedPersona.roleTitle}</span>
                <span
                  className={`px-1.5 py-0.2 rounded text-[10px] font-semibold ${
                    inspectedPersona.isWorking
                      ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                      : 'bg-slate-700/60 text-slate-300'
                  }`}
                >
                  {inspectedPersona.isWorking ? '⚡ 工作中' : '🛋️ 待命休息'}
                </span>
              </div>
              <div className="text-[11px] font-mono text-sky-300 truncate mt-0.5">{inspectedPersona.engineLabel}</div>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              {inspectedPersona.recentRecords.length > 0 && (
                <button
                  type="button"
                  onClick={async () => {
                    const txt = inspectedPersona.recentRecords
                      .map((ev) => `${formatTime(ev.ts)} ${ev.text}`)
                      .join('\n');
                    const ok = await copyTextToClipboard(txt);
                    if (ok) onToast?.(`已複製 ${inspectedPersona.roleTitle.split(' ')[0]} 最新 5 項工作紀錄`);
                  }}
                  className="px-1.5 py-0.5 rounded bg-[#1c2336] hover:bg-[#28324d] border border-[#323e5e] text-[10px] text-amber-300 flex items-center gap-1 cursor-pointer"
                  title="複製此角色最新 5 項工作紀錄"
                >
                  <Copy className="w-2.5 h-2.5" />
                  <span>複製</span>
                </button>
              )}
              <button
                type="button"
                onClick={() => onClickPersona?.(null)}
                className="p-1 rounded hover:bg-[#232b40] text-slate-400 hover:text-slate-100 cursor-pointer"
                title="關閉角色工作紀錄"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          <div className="space-y-1">
            <div className="text-[11px] font-semibold text-amber-300 flex items-center justify-between">
              <span>📋 最新 5 項工作紀錄（DESC 最新在上）</span>
              <span className="text-slate-400 font-mono">{inspectedPersona.recentRecords.length}/5</span>
            </div>
            {inspectedPersona.recentRecords.length === 0 ? (
              <div className="text-xs text-slate-400 py-1.5 px-2 rounded bg-[#141927] border border-[#22293d]">
                目前尚無此角色的工作紀錄（狀態：{inspectedPersona.bubble}）
              </div>
            ) : (
              <ul className="space-y-1 max-h-36 overflow-y-auto pr-1">
                {inspectedPersona.recentRecords.map((ev, idx) => (
                  <li
                    key={ev.id || `${ev.ts}-${idx}`}
                    className="flex items-start gap-1.5 text-[11px] leading-snug p-1.5 rounded bg-[#141927] border border-[#22293d]"
                  >
                    <span className="font-mono text-[10px] text-amber-300/90 shrink-0 mt-0.5">#{idx + 1}</span>
                    <span className="font-mono text-[10px] text-slate-400 shrink-0 mt-0.5">{formatTime(ev.ts)}</span>
                    <span className="text-slate-200 break-words flex-1 select-text">{ev.text}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function EmptySlotCard({
  slotIdx,
  onOpenWizard,
  compact,
}: {
  slotIdx: number;
  onOpenWizard: () => void;
  compact: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const renderEmpty = () => {
      const cv = canvasRef.current;
      if (!cv) return;
      const scale = Math.max(PIXEL_SCALE, 6);
      if (cv.width !== LW * scale) cv.width = LW * scale;
      if (cv.height !== LH * scale) cv.height = LH * scale;
      const ctx = cv.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      drawEmptyRoomCanvas(ctx, `slot-${slotIdx}`, Date.now());
    };
    renderEmpty();
    const timer = setInterval(renderEmpty, 250);
    return () => clearInterval(timer);
  }, [slotIdx]);

  return (
    <div className="h-full min-h-0 rounded-xl border border-dashed border-[#2a3147] bg-[#131722]/70 flex flex-col overflow-hidden hover:border-amber-400/50 transition-colors">
      <div className={`${compact ? 'px-2.5 py-1' : 'px-3 py-1.5'} border-b border-[#202637] flex items-center justify-between text-xs text-slate-400 shrink-0`}>
        <span className="font-semibold">空房間 #{slotIdx + 1}</span>
        <span className="text-[11px] text-slate-500">待派工</span>
      </div>
      <div
        style={{ containerType: 'size' }}
        className="relative flex-1 min-h-0 w-full bg-[#5e4a32] overflow-hidden flex items-center justify-center"
      >
        <div
          style={{
            width: 'min(100cqw, calc(100cqh * 16 / 9))',
            height: 'min(100cqh, calc(100cqw * 9 / 16))',
          }}
          className="relative m-auto shrink-0"
        >
          <canvas
            ref={canvasRef}
            className="w-full h-full block opacity-80"
            aria-hidden="true"
          />
        </div>
        <div className="absolute inset-0 flex items-center justify-center p-2">
          <button
            type="button"
            onClick={onOpenWizard}
            className={`${compact ? 'px-3 py-1.5 text-xs' : 'px-4 py-2 text-sm'} rounded-xl bg-[#161b28]/90 hover:bg-amber-400 hover:text-slate-950 text-slate-100 border border-amber-400/40 font-bold flex items-center gap-1.5 shadow-xl cursor-pointer transition-colors`}
          >
            <Plus className="w-3.5 h-3.5" />
            <span>派工新專案到此房間</span>
          </button>
        </div>
      </div>
      {!compact && (
        <div className="px-3 py-1.5 text-xs text-slate-400 truncate shrink-0">
          選一個專案資料夾並設定目標，由高階主管與開發／審查工程師協作完成。
        </div>
      )}
    </div>
  );
}

export default function RoomGrid({
  workers,
  rooms,
  inbox = [],
  selectedId,
  selectedPersonaSlot,
  onSelect,
  onSelectPersona,
  onToast,
  gridMode,
  onOpenWizard,
  onStartDemo,
  onOpenSetup,
  onTogglePauseRoom,
  onAnswerInbox,
}: RoomGridProps) {
  const [, setCastTick] = useState(0);
  const [expandedApprovalRoomId, setExpandedApprovalRoomId] = useState<string | null>(null);
  const [answeringId, setAnsweringId] = useState<string | null>(null);
  const roomById = new Map(rooms.map((r) => [r.id, r]));
  registerVisibleRooms(workers.map((w) => w.id));

  if (workers.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-6 text-center">
        <div className="max-w-md rounded-2xl bg-[#161923] border border-[#262b3d] p-6 space-y-3 shadow-xl">
          <div className="text-base font-bold text-slate-100">辦公室目前沒有房間</div>
          <p className="text-xs text-slate-400 leading-relaxed">
            點選「派工」建立一個新房間。高階主管（PO / Tech Lead / QA）會負責規劃與把關，並指派開發與審查工程師完成實作、測試與審查！
          </p>
          <div className="flex flex-wrap items-center justify-center gap-2 pt-1">
            <button
              type="button"
              onClick={onOpenWizard}
              className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-amber-400 hover:bg-amber-300 text-slate-950 font-bold text-xs cursor-pointer"
            >
              <Plus className="w-4 h-4" />
              <span>新增房間（派工）</span>
            </button>
            {onStartDemo && (
              <button
                type="button"
                onClick={onStartDemo}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 border border-emerald-500/40 font-semibold text-xs cursor-pointer"
              >
                <Sparkles className="w-3.5 h-3.5 text-emerald-300" />
                <span>一鍵開啟 Demo 展示房間</span>
              </button>
            )}
            {onOpenSetup && (
              <button
                type="button"
                onClick={onOpenSetup}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-[#23293b] hover:bg-[#2e354d] text-slate-200 border border-[#343c54] font-semibold text-xs cursor-pointer"
              >
                <span>🔧 環境健檢精靈</span>
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  // 依據格線模式決定欄列配置，強制 Fit 畫面高度與寬度（2×2 顯示 4 格、3×3 顯示 3欄×3列 共 9 格）
  const effectiveMode: '1x1' | '2x2' | '3x3' =
    gridMode === '1x1'
      ? '1x1'
      : gridMode === '2x2'
        ? '2x2'
        : gridMode === '3x3'
          ? '3x3'
          : workers.length <= 1
            ? '1x1'
            : workers.length <= 4
              ? '2x2'
              : '3x3';

  const isMultiGrid = effectiveMode === '2x2' || effectiveMode === '3x3';
  const is3x3 = effectiveMode === '3x3';

  // 1x1 專注模式只顯示目前選中的房間（未選中時顯示第一個房間）
  const visibleWorkers =
    effectiveMode === '1x1'
      ? [workers.find((w) => w.id === selectedId) ?? workers[0]].filter(Boolean)
      : workers;

  const targetSlots =
    effectiveMode === '2x2'
      ? Math.max(visibleWorkers.length, 4)
      : effectiveMode === '3x3'
        ? Math.max(visibleWorkers.length, 9)
        : 1;
  const emptyCount = effectiveMode === '1x1' ? 0 : Math.max(0, targetSlots - visibleWorkers.length);

  const gridClass =
    effectiveMode === '1x1'
      ? 'grid-cols-1 grid-rows-1 flex-1 min-h-0 max-w-6xl mx-auto w-full'
      : effectiveMode === '2x2'
        ? 'grid-cols-2 grid-rows-2 h-full w-full'
        : 'grid-cols-3 grid-rows-3 h-full w-full';

  return (
    <div className="flex-1 min-h-0 h-full w-full overflow-hidden p-2 sm:p-3 flex flex-col gap-2">
      {effectiveMode === '1x1' && workers.length > 1 && (
        <div className="max-w-6xl mx-auto w-full flex items-center gap-1.5 overflow-x-auto shrink-0 pb-0.5">
          <span className="text-[11px] text-slate-400 shrink-0">切換房間：</span>
          {workers.map((item) => {
            const active = visibleWorkers[0]?.id === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => onSelect(item.id)}
                className={`px-2.5 py-1 rounded-md text-xs font-semibold border transition-colors shrink-0 cursor-pointer flex items-center gap-1.5 ${
                  active
                    ? 'bg-amber-400/20 text-amber-200 border-amber-400/60'
                    : 'bg-[#141824] text-slate-300 border-[#262c40] hover:border-slate-500'
                }`}
              >
                <span
                  className={`w-1.5 h-1.5 rounded-full ${
                    item.needsYou
                      ? 'bg-amber-400'
                      : roomById.get(item.id)?.status === 'running'
                        ? 'bg-emerald-400'
                        : roomById.get(item.id)?.status === 'error'
                          ? 'bg-rose-500'
                          : 'bg-slate-500'
                  }`}
                />
                <span>{item.name}</span>
              </button>
            );
          })}
        </div>
      )}
      <div className={`grid ${gridClass} ${is3x3 ? 'gap-2' : 'gap-2.5'}`}>
        {visibleWorkers.map((w) => {
          const room = roomById.get(w.id);
          const selected = w.id === selectedId;
          const phaseIdx = Math.max(0, PHASES.findIndex((p) => p.id === (w.phase ?? 'goal')));
          const roomInbox = inbox.filter((i) => i.room_id === w.id || (Array.isArray(i.rooms) && i.rooms.includes(w.id)));
          const scene = room ? buildRoomScene(room, roomInbox) : null;
          const activeCrit =
            scene && scene.activeCriterionIndex >= 0 && room?.criteria
              ? room.criteria[scene.activeCriterionIndex]
              : null;

          return (
            <div
              key={w.id}
              onClick={() => onSelect(w.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onSelect(w.id);
                }
              }}
              className={`group relative h-full min-h-0 rounded-xl border transition-all flex flex-col overflow-hidden cursor-pointer text-left ${
                selected
                  ? 'border-amber-400 bg-[#181d2b] shadow-[0_0_0_2px_rgba(251,191,36,0.25)]'
                  : w.needsYou
                    ? 'border-amber-500/60 bg-[#161a26] hover:border-amber-400'
                    : 'border-[#262c40] bg-[#141824] hover:border-[#38415e]'
              }`}
            >
              {/* 房間頂部標題列：多格（2x2 / 3x3）拆成兩列，確保左上角任務名稱永遠完整清楚、不被裝飾選單擠壓 */}
              {(() => {
                const sceneSelectors = room && !is3x3 ? (
                  <>
                    <select
                      aria-label="選擇窗外風景"
                      value={getRoomVistaOverride(room.id)}
                      onChange={(e) => {
                        const v = e.target.value as WindowVistaSelection;
                        setRoomVistaOverride(room.id, v);
                        setCastTick((n) => n + 1);
                      }}
                      className={`${isMultiGrid ? 'px-1' : 'px-1.5'} py-0.5 rounded bg-[#181e2e] hover:bg-[#232b40] text-[10px] font-semibold text-sky-200 border border-sky-500/35 cursor-pointer focus:outline-none`}
                      title="選擇本房間窗外風景（都會高樓／古民家庭園／海港遠眺／高山雲海）"
                    >
                      <option value="auto">{isMultiGrid ? '🪟 窗景' : '🪟 自動窗景'}</option>
                      <option value="skyline">🏙️ 都會高樓</option>
                      <option value="garden">🌳 古民家庭園</option>
                      <option value="harbor">🌊 海港遠眺</option>
                      <option value="alpine">⛰️ 高山雲海</option>
                    </select>

                    <select
                      aria-label="選擇季節與室內配置"
                      value={getRoomThemeOverride(room.id)}
                      onChange={(e) => {
                        setRoomThemeOverride(room.id, e.target.value);
                        setCastTick((n) => n + 1);
                      }}
                      className={`${isMultiGrid ? 'px-1 max-w-[92px]' : 'px-1.5 max-w-[110px]'} py-0.5 rounded bg-[#241e18] hover:bg-[#352a20] text-[10px] font-semibold text-amber-200 border border-amber-500/35 cursor-pointer focus:outline-none`}
                      title="選擇季節與室內配置（夏天會收起火爐換成西瓜冰麥茶几，冬天火爐最旺）"
                    >
                      <option value="auto">{isMultiGrid ? '🎨 季節' : '🎨 自動季節'}</option>
                      {ROOM_THEMES.map((th) => (
                        <option key={th.id} value={th.id}>
                          {th.badge}
                        </option>
                      ))}
                    </select>

                    <select
                      aria-label="選擇時段光影"
                      value={getTimeOfDayOverride()}
                      onChange={(e) => {
                        setTimeOfDayOverride(e.target.value as TimeOfDayOverride);
                        setCastTick((n) => n + 1);
                      }}
                      className={`${isMultiGrid ? 'px-1' : 'px-1.5'} py-0.5 rounded bg-[#1a1829] hover:bg-[#282440] text-[10px] font-semibold text-violet-200 border border-violet-500/35 cursor-pointer focus:outline-none`}
                      title="SunCalc 真實日照光影與陰影方向（可即時或手動預覽晨／午／夕／夜）"
                    >
                      <option value="auto">{isMultiGrid ? '🕐 日照' : '🕐 即時日照'}</option>
                      <option value="dawn">🌅 晨曦</option>
                      <option value="noon">☀️ 正午</option>
                      <option value="sunset">🌇 夕照</option>
                      <option value="night">🌙 星夜</option>
                    </select>
                  </>
                ) : null;

                const castButton = room ? (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      cycleRoomCast(room.id);
                      setCastTick((v) => v + 1);
                      const nextScene = buildRoomScene(room, roomInbox);
                      const names = nextScene.personas.map((p) => p.character.name).join('、');
                      const pets = nextScene.companions.map((c) => c.character?.name).join('、');
                      onToast?.(
                        `🎲 已切換「${w.name}」風景（${nextScene.theme.badge}）、人類成員（${names}）與吉祥物（${pets}）`
                      );
                    }}
                    className="px-1.5 py-0.5 rounded bg-[#261e18] hover:bg-[#382b22] text-[10px] font-semibold text-amber-200 flex items-center gap-1 cursor-pointer border border-amber-500/35 shrink-0"
                    title={`隨機切換本房間的四季風景擺設（${ROOM_THEMES.length} 種）與人類／動物吉祥物陣容（${CHARACTER_ROSTER.length} 位）`}
                  >
                    <Shuffle className="w-2.5 h-2.5 text-amber-300" />
                    <span>換角</span>
                  </button>
                ) : null;

                const phaseBadge = (
                  <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-[#1e2436] text-sky-300 border border-sky-500/30 shrink-0">
                    {PHASES[phaseIdx]?.label ?? '進行中'}
                  </span>
                );

                const themeBadge = scene?.theme ? (
                  <span
                    className={`px-1.5 py-0.5 rounded text-[9px] font-semibold bg-[#241e18] text-amber-200 border border-amber-500/30 ${isMultiGrid ? 'truncate' : 'shrink-0'}`}
                    title={`本房間專屬風景與室內擺設主題（共 ${ROOM_THEMES.length} 種四季／天氣設定）：${scene.theme.name}`}
                  >
                    {scene.theme.badge}
                  </span>
                ) : null;

                return (
                  <div className={`${isMultiGrid ? 'px-2.5 py-1.5 space-y-1' : 'px-3.5 py-2'} bg-[#10131d] border-b border-[#22283a] shrink-0`}>
                    <div className="flex items-center justify-between gap-2 min-w-0">
                      <div className="min-w-0 flex-1 flex items-center gap-1.5">
                        <span
                          className={`w-2 h-2 rounded-full shrink-0 ${
                            w.needsYou
                              ? 'bg-amber-400 animate-pulse'
                              : room?.status === 'running'
                                ? 'bg-emerald-400'
                                : room?.status === 'error'
                                  ? 'bg-rose-500'
                                  : 'bg-slate-500'
                          }`}
                        />
                        <h3
                          className={`${is3x3 ? 'text-xs' : 'text-sm'} font-bold text-slate-100 truncate min-w-0`}
                          title={`${w.name}${room?.goal ? ` — ${room.goal}` : ''}`}
                        >
                          {w.name}
                        </h3>
                        {room?.delegate && (
                          <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 shrink-0">
                            AI 代管
                          </span>
                        )}
                        {!isMultiGrid && phaseBadge}
                        {!isMultiGrid && themeBadge}
                      </div>

                      <div className="flex items-center gap-1.5 shrink-0">
                        <div
                          className={`text-[10px] font-semibold flex items-center gap-1 ${is3x3 ? 'max-w-[96px]' : 'max-w-[150px]'} truncate ${statusTone(w)}`}
                          title={w.statusLabel}
                        >
                          {w.needsYou ? (
                            <AlertTriangle className="w-3 h-3 text-amber-400 shrink-0" />
                          ) : room?.status === 'done' || progressPct(w) === 100 ? (
                            <CheckCircle2 className="w-3 h-3 text-emerald-400 shrink-0" />
                          ) : (
                            <CircleDot className="w-3 h-3 shrink-0" />
                          )}
                          <span className="truncate">{w.statusLabel}</span>
                        </div>

                        {!isMultiGrid && room && (
                          <div className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
                            {sceneSelectors}
                            {castButton}
                          </div>
                        )}

                        {room && onTogglePauseRoom && !['done', 'archived'].includes(room.status) && (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              onTogglePauseRoom(room.id, room.status === 'paused');
                            }}
                            className="px-1.5 py-0.5 rounded bg-[#202638] hover:bg-[#2d354d] text-[10px] font-semibold text-slate-200 flex items-center gap-1 cursor-pointer border border-[#313a54] shrink-0"
                            title={room.status === 'paused' ? '繼續此房間' : '暫停此房間'}
                          >
                            {room.status === 'paused' ? <Play className="w-2.5 h-2.5 text-emerald-400" /> : <Pause className="w-2.5 h-2.5 text-amber-300" />}
                            <span>{room.status === 'paused' ? '繼續' : '暫停'}</span>
                          </button>
                        )}
                      </div>
                    </div>

                    {isMultiGrid && (
                      <div className="flex items-center justify-between gap-1.5 min-w-0">
                        <div className="flex items-center gap-1 min-w-0 overflow-hidden">
                          {phaseBadge}
                          {themeBadge}
                        </div>
                        {room && (
                          <div className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
                            {sceneSelectors}
                            {castButton}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* 階段進度條 */}
              <div className={`grid grid-cols-3 gap-1 ${isMultiGrid ? 'px-2.5 py-1' : 'px-3.5 py-1.5'} bg-[#121622] border-b border-[#1f2536] shrink-0`}>
                {PHASES.map((p, idx) => (
                  <div key={p.id} className="flex items-center gap-1 min-w-0">
                    <div
                      className={`h-1 flex-1 rounded-full ${
                        idx < phaseIdx
                          ? 'bg-emerald-400'
                          : idx === phaseIdx
                            ? w.needsYou
                              ? 'bg-amber-400'
                              : 'bg-sky-400'
                            : 'bg-[#252c3f]'
                      }`}
                    />
                    {!is3x3 && (
                      <span className={`text-[9px] truncate hidden sm:inline ${idx === phaseIdx ? 'text-slate-200 font-semibold' : 'text-slate-500'}`}>
                        {p.label}
                      </span>
                    )}
                  </div>
                ))}
              </div>

              {/* 房間內即時權限審核／決策警示氣泡（不需切回右側文字清單，點一下即可核准或退回） */}
              {roomInbox.length > 0 && (
                <div
                  className="shrink-0 bg-amber-500/15 border-b border-amber-500/40 px-2.5 py-1.5"
                  onClick={(e) => e.stopPropagation()}
                >
                  <div className="flex items-center justify-between gap-2">
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedApprovalRoomId((prev) => (prev === w.id ? null : w.id))
                      }
                      className="flex items-center gap-1.5 text-left min-w-0 cursor-pointer"
                    >
                      <span className="w-2 h-2 rounded-full bg-amber-400 animate-ping shrink-0" />
                      <AlertTriangle className="w-3.5 h-3.5 text-amber-300 shrink-0" />
                      <span className="text-[11px] font-semibold text-amber-100 truncate">
                        ⚡ 需要你確認：{roomInbox[0].title || roomInbox[0].text || '權限或決策待審核'}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedApprovalRoomId((prev) => (prev === w.id ? null : w.id))
                      }
                      className="px-2 py-0.5 rounded bg-amber-400 hover:bg-amber-300 text-slate-950 text-[10px] font-bold shrink-0 cursor-pointer"
                    >
                      {expandedApprovalRoomId === w.id ? '收合' : `快速審核 (${roomInbox.length})`}
                    </button>
                  </div>

                  {expandedApprovalRoomId === w.id && onAnswerInbox && (
                    <div className="mt-2 pt-2 border-t border-amber-500/30 space-y-2 text-[11px]">
                      {roomInbox[0].detail && (
                        <p className="text-amber-100/90 leading-snug break-words line-clamp-3">
                          {roomInbox[0].detail}
                        </p>
                      )}
                      <div className="flex flex-wrap gap-1.5">
                        {(Array.isArray(roomInbox[0].options) && roomInbox[0].options.length > 0
                          ? roomInbox[0].options.map((o) => ({
                              id: String(o.id ?? o.label ?? 'approve'),
                              label: String(o.label ?? o.id ?? '同意'),
                            }))
                          : [
                              { id: 'allow', label: '✅ 核准執行' },
                              { id: 'deny', label: '❌ 拒絕／退回' },
                            ]
                        ).map((opt) => (
                          <button
                            key={opt.id}
                            type="button"
                            disabled={answeringId === roomInbox[0].id}
                            onClick={async () => {
                              setAnsweringId(roomInbox[0].id);
                              try {
                                await onAnswerInbox(roomInbox[0].id, opt.id, opt.label);
                                setExpandedApprovalRoomId(null);
                              } finally {
                                setAnsweringId(null);
                              }
                            }}
                            className={`px-2.5 py-1 rounded-md font-semibold text-[11px] cursor-pointer transition-colors ${
                              opt.id === 'deny' || opt.id === 'reject'
                                ? 'bg-rose-500/20 hover:bg-rose-500/35 text-rose-200 border border-rose-500/40'
                                : 'bg-emerald-500/20 hover:bg-emerald-500/35 text-emerald-200 border border-emerald-500/40'
                            }`}
                          >
                            {opt.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* 像素辦公室畫布（自動伸縮填滿卡片剩餘高度，點擊像素人可看最新 5 項工作紀錄） */}
              {room && (
                <PixelRoomCanvas
                  room={room}
                  inboxItems={roomInbox}
                  selected={selected}
                  selectedPersonaSlot={selected ? selectedPersonaSlot : null}
                  onClickPersona={(slot) => {
                    onSelect(w.id);
                    onSelectPersona?.(w.id, slot);
                  }}
                  onToast={onToast}
                  compact={isMultiGrid}
                />
              )}

              {/* 房間內三位 Personas 可點擊名牌列（點擊任一角色查看最新 5 項工作紀錄） */}
              {scene && !is3x3 && (
                <div className={`grid grid-cols-3 gap-1.5 ${isMultiGrid ? 'px-2 py-1' : 'p-2.5'} bg-[#111520] border-b border-[#1f2536] shrink-0`}>
                  {scene.personas.map((persona) => {
                    const isPinned = selected && selectedPersonaSlot === persona.slotKey;
                    return (
                      <button
                        key={persona.slotKey}
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelect(w.id);
                          onSelectPersona?.(w.id, isPinned ? null : persona.slotKey);
                        }}
                        className={`rounded-lg text-left transition-all cursor-pointer ${
                          isPinned
                            ? 'bg-amber-500/15 border border-amber-400 ring-1 ring-amber-400/50'
                            : persona.isWorking
                              ? 'bg-[#172133] border border-sky-500/40 hover:border-amber-400/70'
                              : 'bg-[#171c2b] border border-[#262e45] hover:border-amber-400/60'
                        } ${isMultiGrid ? 'px-1.5 py-1' : 'px-3 py-2'} flex flex-col justify-between min-w-0`}
                        title={`點擊查看 ${persona.roleTitle} 的最新 5 項工作紀錄（目前：${persona.bubble}）`}
                      >
                        <div className="flex items-center justify-between gap-1 w-full">
                          <span className={`${isMultiGrid ? 'text-[10px]' : 'text-xs sm:text-sm'} font-bold text-slate-100 truncate flex items-center gap-1.5`}>
                            <span>{persona.roleTitle.split(' ')[0]}</span>
                            <span
                              className={`${isMultiGrid ? 'text-[8px] px-1' : 'text-[10px] px-1.5 py-0.2'} rounded ${
                                persona.isWorking ? 'bg-emerald-500/25 text-emerald-300' : 'bg-slate-800 text-slate-400'
                              }`}
                            >
                              {persona.isWorking ? '工作中' : '待命'}
                            </span>
                          </span>
                          <span
                            className={`px-1.5 py-0.2 rounded ${isMultiGrid ? 'text-[8.5px]' : 'text-[10px]'} font-bold text-slate-950 shrink-0`}
                            style={{ backgroundColor: persona.badgeColor }}
                          >
                            {persona.tierLabel}
                          </span>
                        </div>
                        <div className={`${isMultiGrid ? 'text-[9.5px]' : 'text-[11px] mt-0.5'} text-sky-300 font-mono truncate w-full`} title={persona.engineLabel}>
                          {persona.engineLabel}
                        </div>
                        {!isMultiGrid && (
                          <div className="text-xs text-slate-100 truncate mt-1 bg-[#0d1018] px-2.5 py-1.5 rounded-md border border-[#283148] w-full flex items-center justify-between gap-2" title={persona.bubble}>
                            <span className="truncate font-medium">💬 {persona.bubble}</span>
                            <span className="text-[10px] text-amber-300 shrink-0 font-mono">
                              紀錄({persona.recentRecords.length})
                            </span>
                          </div>
                        )}
                      </button>
                    );
                  })}
                </div>
              )}

              {/* 房間底部：目前執行的 CE 計畫單元與目標進度摘要 */}
              <div className={`${isMultiGrid ? 'px-2.5 py-1 text-[10px]' : 'px-3.5 py-1.5 text-[11px]'} bg-[#0e111a] flex items-center justify-between gap-2 shrink-0`}>
                {activeCrit ? (
                  <div className="text-slate-200 truncate flex-1 flex items-center gap-1.5" title={`目前執行 CE 計畫單元：${activeCrit.id || ''} ${activeCrit.text}`}>
                    <span className="px-1.5 py-0.2 rounded bg-amber-400/20 border border-amber-400/40 text-amber-300 font-mono text-[9.5px] font-bold shrink-0">
                      ⚡ 計畫進度 {(scene?.activeCriterionIndex ?? 0) + 1}/{room?.criteria?.length ?? 1}
                    </span>
                    <span className="font-mono text-amber-200 shrink-0">{activeCrit.id}</span>
                    <span className="truncate text-slate-200">{activeCrit.text}</span>
                  </div>
                ) : (
                  <div className="text-slate-300 truncate flex-1" title={w.goal || w.activity}>
                    <span className="text-slate-500 mr-1">目標：</span>
                    {w.goal || w.activity || '跟隨 CE 計畫執行'}
                  </div>
                )}
                {roomInbox.length > 0 && (
                  <span className="px-2 py-0.5 rounded-full bg-amber-400 text-slate-950 font-bold text-[9.5px] shrink-0">
                    {roomInbox.length} 件等你決定
                  </span>
                )}
              </div>
            </div>
          );
        })}

        {/* 2x2 / 3x3 模式下的空房間派工格 */}
        {Array.from({ length: emptyCount }).map((_, i) => (
          <EmptySlotCard key={`empty-${i}`} slotIdx={workers.length + i} onOpenWizard={onOpenWizard} compact={isMultiGrid} />
        ))}
      </div>
    </div>
  );
}
