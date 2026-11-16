'use client';

import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Crosshair,
  Eye,
  EyeOff,
  Minus,
  Plus,
  Volume2,
  VolumeX,
} from 'lucide-react';
import { MAP_COLS, MAP_ROWS, TILE_SIZE } from '@/lib/office-map';
import { advanceSimulationTick, routeWorkerForStatus } from '@/lib/office-sim';
import {
  drawAnimatedWindowStars,
  drawDeskLabels,
  drawFurniture,
  drawOfficeBackground,
  drawWorkerCharacter,
  drawZoneSignage,
  getWorkerHitBounds,
} from '@/lib/pixel-sprites';
import type { AgentWorker } from '@/lib/types';

interface PixelOfficeCanvasProps {
  workers: AgentWorker[];
  selectedWorkerId: string | null;
  onSelectWorker: (workerId: string) => void;
  onJumpNextHelp: () => void;
  needsHelpCount: number;
  soundMuted: boolean;
  onToggleSound: () => void;
}

const WORLD_W = MAP_COLS * TILE_SIZE;
const WORLD_H = MAP_ROWS * TILE_SIZE;

const STATUS_LABEL: Record<AgentWorker['status'], string> = {
  working: '工作中',
  needs_help: '需要協助',
  idle: '待機休息',
  paused: '已暫停',
  error: '發生異常',
};

export default function PixelOfficeCanvas({
  workers,
  selectedWorkerId,
  onSelectWorker,
  onJumpNextHelp,
  needsHelpCount,
  soundMuted,
  onToggleSound,
}: PixelOfficeCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // Decoupled mutable simulation refs for 60fps loop without React re-render overhead
  const simWorkersRef = useRef<AgentWorker[]>(workers);
  const selectedIdRef = useRef<string | null>(selectedWorkerId);
  const hoveredIdRef = useRef<string | null>(null);
  const showLabelsRef = useRef<boolean>(true);
  const cameraRef = useRef({ zoom: 2.2, panX: 0, panY: 0, initialized: false });
  const dragRef = useRef<{
    active: boolean;
    moved: boolean;
    startX: number;
    startY: number;
    origPanX: number;
    origPanY: number;
  }>({
    active: false,
    moved: false,
    startX: 0,
    startY: 0,
    origPanX: 0,
    origPanY: 0,
  });

  const [showLabels, setShowLabels] = useState(true);
  const [zoomPct, setZoomPct] = useState(220);
  const [hoveredWorker, setHoveredWorker] = useState<{
    worker: AgentWorker;
    left: number;
    top: number;
  } | null>(null);

  // Sync incoming server snapshot into simulation state without snapping walking characters back
  useEffect(() => {
    const prevMap = new Map<string, AgentWorker>();
    for (const prev of simWorkersRef.current) {
      prevMap.set(prev.id, prev);
    }

    // 助理只送「狀態」；走去哪裡、怎麼走由瀏覽器依目前的即時位置規劃（休息→茶水間，其他→回座位）
    const next: AgentWorker[] = [];
    for (const incoming of workers) {
      const existing = prevMap.get(incoming.id);
      if (!existing) {
        next.push(routeWorkerForStatus(incoming, [...next, ...workers]));
        continue;
      }
      const sameDestination = existing.status === incoming.status && existing.deskId === incoming.deskId;
      if (sameDestination) {
        next.push({
          ...incoming,
          pos: existing.pos,
          path: existing.path,
          facing: existing.facing,
          behaviorState: existing.behaviorState,
          targetSpotId: existing.targetSpotId,
        });
        continue;
      }
      next.push(
        routeWorkerForStatus({ ...incoming, pos: existing.pos, targetSpotId: undefined }, [...next, ...workers])
      );
    }
    simWorkersRef.current = next;
  }, [workers]);

  useEffect(() => {
    selectedIdRef.current = selectedWorkerId;
  }, [selectedWorkerId]);

  useEffect(() => {
    showLabelsRef.current = showLabels;
  }, [showLabels]);

  const fitCameraToViewport = (w: number, h: number) => {
    if (w <= 0 || h <= 0) return;
    const fitZoom = Math.min((w - 24) / WORLD_W, (h - 24) / WORLD_H);
    const clampedZoom = Math.max(1.2, Math.min(3.0, Math.floor(fitZoom * 10) / 10));
    cameraRef.current.zoom = clampedZoom;
    cameraRef.current.panX = Math.round((w - WORLD_W * clampedZoom) / 2);
    cameraRef.current.panY = Math.round((h - WORLD_H * clampedZoom) / 2);
    cameraRef.current.initialized = true;
    setZoomPct(Math.round(clampedZoom * 100));
  };

  const adjustZoom = (delta: number) => {
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const prevZoom = cameraRef.current.zoom;
    const nextZoom = Math.max(1.0, Math.min(3.5, Math.round((prevZoom + delta) * 4) / 4));
    if (nextZoom === prevZoom) return;

    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const worldCx = (cx - cameraRef.current.panX) / prevZoom;
    const worldCy = (cy - cameraRef.current.panY) / prevZoom;

    cameraRef.current.zoom = nextZoom;
    cameraRef.current.panX = Math.round(cx - worldCx * nextZoom);
    cameraRef.current.panY = Math.round(cy - worldCy * nextZoom);
    setZoomPct(Math.round(nextZoom * 100));
  };

  const resetCamera = () => {
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    fitCameraToViewport(rect.width, rect.height);
  };

  // Focus camera gently on selected worker when selection changes from outside
  useEffect(() => {
    if (!selectedWorkerId || !containerRef.current || !cameraRef.current.initialized) return;
    const target = simWorkersRef.current.find((w) => w.id === selectedWorkerId);
    if (!target) return;
    const rect = containerRef.current.getBoundingClientRect();
    const zoom = cameraRef.current.zoom;
    const wx = (target.pos.x + 0.5) * TILE_SIZE * zoom;
    const wy = (target.pos.y + 0.5) * TILE_SIZE * zoom;
    const screenX = wx + cameraRef.current.panX;
    const screenY = wy + cameraRef.current.panY;

    // Only pan if character is near/outside viewport margin
    if (
      screenX < 80 ||
      screenX > rect.width - 80 ||
      screenY < 80 ||
      screenY > rect.height - 80
    ) {
      cameraRef.current.panX = Math.round(rect.width / 2 - wx);
      cameraRef.current.panY = Math.round(rect.height / 2 - wy);
    }
  }, [selectedWorkerId]);

  useEffect(() => {
    const container = containerRef.current;
    const canvas = canvasRef.current;
    if (!container || !canvas) return;

    // Pre-render static office floor, walls, rugs, and window frames once onto offscreen canvas
    const bgCanvas = document.createElement('canvas');
    bgCanvas.width = WORLD_W;
    bgCanvas.height = WORLD_H;
    const bgCtx = bgCanvas.getContext('2d');
    if (bgCtx) {
      bgCtx.imageSmoothingEnabled = false;
      drawOfficeBackground(bgCtx);
    }

    let animId = 0;
    let lastTime = performance.now();

    const resizeCanvas = () => {
      const rect = container.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      canvas.style.width = `${rect.width}px`;
      canvas.style.height = `${rect.height}px`;
      if (!cameraRef.current.initialized) {
        fitCameraToViewport(rect.width, rect.height);
      }
    };

    const observer = new ResizeObserver(resizeCanvas);
    observer.observe(container);
    resizeCanvas();

    const renderFrame = (now: number) => {
      const dt = Math.min((now - lastTime) / 1000, 0.05);
      lastTime = now;

      // Advance walking interpolation
      simWorkersRef.current = advanceSimulationTick(simWorkersRef.current, dt);

      const ctx = canvas.getContext('2d');
      if (ctx) {
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const cssW = canvas.width / dpr;
        const cssH = canvas.height / dpr;

        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.imageSmoothingEnabled = false;

        // Clear studio floor void
        ctx.fillStyle = '#0e1017';
        ctx.fillRect(0, 0, cssW, cssH);

        const { zoom, panX, panY } = cameraRef.current;
        const timeSec = now / 1000;

        ctx.save();
        ctx.translate(panX, panY);
        ctx.scale(zoom, zoom);
        ctx.imageSmoothingEnabled = false;

        ctx.drawImage(bgCanvas, 0, 0);
        drawAnimatedWindowStars(ctx, timeSec);
        drawFurniture(ctx, timeSec, simWorkersRef.current);

        // Sort characters by Y depth for natural overlap
        const sortedWorkers = [...simWorkersRef.current].sort(
          (a, b) => a.pos.y - b.pos.y
        );
        for (const worker of sortedWorkers) {
          drawWorkerCharacter(
            ctx,
            worker,
            timeSec,
            now,
            worker.id === selectedIdRef.current,
            worker.id === hoveredIdRef.current
          );
        }

        ctx.restore();

        // Crisp screen-space labels & zone signage
        ctx.save();
        ctx.translate(panX, panY);
        drawZoneSignage(ctx, zoom);
        drawDeskLabels(ctx, sortedWorkers, showLabelsRef.current, zoom);
        ctx.restore();
      }

      animId = requestAnimationFrame(renderFrame);
    };

    animId = requestAnimationFrame(renderFrame);

    return () => {
      cancelAnimationFrame(animId);
      observer.disconnect();
    };
  }, []);

  const hitTestWorker = (clientX: number, clientY: number): AgentWorker | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const { zoom, panX, panY } = cameraRef.current;
    const worldX = (clientX - rect.left - panX) / zoom;
    const worldY = (clientY - rect.top - panY) / zoom;

    let best: AgentWorker | null = null;
    let bestDist = 22; // generous click radius in world pixels

    for (const w of simWorkersRef.current) {
      const { charCenter, deskCenter } = getWorkerHitBounds(w);
      const dChar = Math.hypot(worldX - charCenter.x, worldY - charCenter.y);
      const dDesk = Math.hypot(worldX - deskCenter.x, worldY - deskCenter.y);
      const d = Math.min(dChar, dDesk);
      if (d < bestDist) {
        bestDist = d;
        best = w;
      }
    }
    return best;
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    dragRef.current = {
      active: true,
      moved: false,
      startX: e.clientX,
      startY: e.clientY,
      origPanX: cameraRef.current.panX,
      origPanY: cameraRef.current.panY,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();

    if (dragRef.current.active) {
      const dx = e.clientX - dragRef.current.startX;
      const dy = e.clientY - dragRef.current.startY;
      if (Math.hypot(dx, dy) > 5) {
        dragRef.current.moved = true;
        cameraRef.current.panX = dragRef.current.origPanX + dx;
        cameraRef.current.panY = dragRef.current.origPanY + dy;
        setHoveredWorker(null);
      }
      return;
    }

    const hit = hitTestWorker(e.clientX, e.clientY);
    hoveredIdRef.current = hit?.id ?? null;
    if (hit) {
      const sx = e.clientX - rect.left;
      const sy = e.clientY - rect.top;
      setHoveredWorker({
        worker: hit,
        left: Math.max(12, Math.min(sx + 14, rect.width - 270)),
        top: Math.max(12, sy - 70),
      });
    } else {
      setHoveredWorker(null);
    }
  };

  const handlePointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const wasMoved = dragRef.current.moved;
    dragRef.current.active = false;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    if (!wasMoved) {
      const hit = hitTestWorker(e.clientX, e.clientY);
      if (hit) {
        onSelectWorker(hit.id);
      }
    }
  };

  const handleWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    const delta = e.deltaY < 0 ? 0.25 : -0.25;
    adjustZoom(delta);
  };

  return (
    <div
      ref={containerRef}
      className="relative flex-1 h-full min-h-[420px] bg-[#0e1017] overflow-hidden select-none"
      data-testid="pixel-office-viewport"
    >
      <canvas
        ref={canvasRef}
        data-testid="pixel-office-canvas"
        aria-label="像素辦公室即時工作監看畫布"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerLeave={() => {
          hoveredIdRef.current = null;
          setHoveredWorker(null);
        }}
        onWheel={handleWheel}
        className={`block w-full h-full pixel-canvas ${
          hoveredWorker ? 'cursor-pointer' : 'cursor-grab active:cursor-grabbing'
        }`}
      />

      {/* Top-Left Compact Game HUD Strip (solid opaque dark surfaces, zero backdrop-blur) */}
      <div className="pointer-events-none absolute top-4 left-4 right-4 flex flex-wrap items-center justify-between gap-3">
        <div className="pointer-events-auto flex items-center gap-2">
          {needsHelpCount > 0 ? (
            <button
              type="button"
              onClick={onJumpNextHelp}
              className="px-3.5 py-2 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 text-xs font-bold flex items-center gap-2 shadow-md transition-colors cursor-pointer focus-visible:outline-2 focus-visible:outline-white"
            >
              <AlertTriangle className="w-4 h-4" aria-hidden="true" />
              <span className="tabular-nums">
                {needsHelpCount} 位夥伴需要協助 · 前往處理 [N]
              </span>
            </button>
          ) : (
            <div className="px-3 py-1.5 rounded-lg bg-[#161a26] border border-[#2a3044] text-xs text-emerald-400 font-medium">
              全體運作順暢 · 無待處理協助
            </div>
          )}
        </div>

        {/* Top-Right Compact Viewport Utility Controls */}
        <div className="pointer-events-auto flex items-center gap-1.5 bg-[#161a26] border border-[#2a3044] rounded-lg p-1 shadow-md">
          <button
            type="button"
            onClick={() => setShowLabels((v) => !v)}
            aria-label={showLabels ? '隱藏角色名牌' : '顯示角色名牌'}
            title={showLabels ? '隱藏角色名牌' : '顯示角色名牌'}
            className="p-1.5 rounded text-slate-300 hover:bg-[#22293d] hover:text-white transition-colors cursor-pointer"
          >
            {showLabels ? (
              <Eye className="w-4 h-4" aria-hidden="true" />
            ) : (
              <EyeOff className="w-4 h-4" aria-hidden="true" />
            )}
          </button>

          <button
            type="button"
            onClick={onToggleSound}
            aria-label={soundMuted ? '開啟提示音效' : '靜音提示音效'}
            title={soundMuted ? '開啟提示音效' : '靜音提示音效'}
            className="p-1.5 rounded text-slate-300 hover:bg-[#22293d] hover:text-white transition-colors cursor-pointer"
          >
            {soundMuted ? (
              <VolumeX className="w-4 h-4 text-slate-500" aria-hidden="true" />
            ) : (
              <Volume2 className="w-4 h-4 text-amber-300" aria-hidden="true" />
            )}
          </button>

          <div className="w-px h-4 bg-[#2a3044] mx-0.5" aria-hidden="true" />

          <button
            type="button"
            onClick={() => adjustZoom(-0.25)}
            aria-label="縮小畫布"
            className="p-1.5 rounded text-slate-300 hover:bg-[#22293d] hover:text-white transition-colors cursor-pointer"
          >
            <Minus className="w-3.5 h-3.5" aria-hidden="true" />
          </button>
          <span className="text-[11px] font-mono tabular-nums text-slate-300 px-1 min-w-[42px] text-center">
            {zoomPct}%
          </span>
          <button
            type="button"
            onClick={() => adjustZoom(0.25)}
            aria-label="放大畫布"
            className="p-1.5 rounded text-slate-300 hover:bg-[#22293d] hover:text-white transition-colors cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={resetCamera}
            aria-label="重設視角置中"
            title="重設視角置中 [0]"
            className="p-1.5 rounded text-slate-300 hover:bg-[#22293d] hover:text-white transition-colors cursor-pointer"
          >
            <Crosshair className="w-3.5 h-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* Bottom-Right Subtle Control Hint */}
      <div className="pointer-events-none absolute bottom-3 right-4 text-[11px] text-slate-400 bg-[#141720]/90 border border-[#252a3a] px-2.5 py-1 rounded">
        點擊角色查看與協助 · 拖曳平移 · 滾輪縮放 · [N] 跳至求助夥伴
      </div>

      {/* Crisp Hover Tooltip */}
      {hoveredWorker && (
        <div
          className="pointer-events-none absolute z-20 w-64 p-3 rounded-lg bg-[#161a26] border border-[#343c54] shadow-xl text-xs"
          style={{
            left: hoveredWorker.left,
            top: hoveredWorker.top,
          }}
        >
          <div className="flex items-center justify-between gap-2 mb-1">
            <span className="font-semibold text-slate-100 truncate">
              {hoveredWorker.worker.name}
            </span>
            <span
              className={`font-medium shrink-0 ${
                hoveredWorker.worker.status === 'needs_help'
                  ? 'text-amber-400'
                  : hoveredWorker.worker.status === 'error'
                    ? 'text-rose-400'
                    : hoveredWorker.worker.status === 'working'
                      ? 'text-emerald-400'
                      : 'text-slate-400'
              }`}
            >
              {STATUS_LABEL[hoveredWorker.worker.status]}
            </span>
          </div>
          <div className="font-mono text-[11px] text-slate-400 truncate mb-1">
            {hoveredWorker.worker.appName} · {hoveredWorker.worker.windowTitle}
          </div>
          <p className="text-slate-300 leading-relaxed line-clamp-2">
            {hoveredWorker.worker.activity}
          </p>
        </div>
      )}
    </div>
  );
}
