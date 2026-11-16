import {
  DESK_SLOTS,
  FURNITURE_PROPS,
  MAP_COLS,
  MAP_ROWS,
  TILE_SIZE,
  getDeskSlotById,
} from './office-map';
import type { AgentWorker, ToolCategory } from './types';

function px(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  color: string
) {
  ctx.fillStyle = color;
  ctx.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h));
}

function drawRug(
  ctx: CanvasRenderingContext2D,
  tx: number,
  ty: number,
  tw: number,
  th: number,
  base: string,
  border: string,
  accent: string
) {
  const x = tx * TILE_SIZE;
  const y = ty * TILE_SIZE;
  const w = tw * TILE_SIZE;
  const h = th * TILE_SIZE;
  px(ctx, x, y, w, h, border);
  px(ctx, x + 2, y + 2, w - 4, h - 4, base);
  // Subtle corner motif
  px(ctx, x + 4, y + 4, 3, 1, accent);
  px(ctx, x + 4, y + 4, 1, 3, accent);
  px(ctx, x + w - 7, y + 4, 3, 1, accent);
  px(ctx, x + w - 5, y + 4, 1, 3, accent);
  px(ctx, x + 4, y + h - 5, 3, 1, accent);
  px(ctx, x + 4, y + h - 7, 1, 3, accent);
  px(ctx, x + w - 7, y + h - 5, 3, 1, accent);
  px(ctx, x + w - 5, y + h - 7, 1, 3, accent);
}

export function drawOfficeBackground(ctx: CanvasRenderingContext2D) {
  const worldW = MAP_COLS * TILE_SIZE;
  const worldH = MAP_ROWS * TILE_SIZE;

  // Base floor planks
  for (let ty = 2; ty < MAP_ROWS - 1; ty++) {
    for (let tx = 1; tx < MAP_COLS - 1; tx++) {
      const x = tx * TILE_SIZE;
      const y = ty * TILE_SIZE;
      const alt = (tx + ty) % 2 === 0;
      px(ctx, x, y, TILE_SIZE, TILE_SIZE, alt ? '#6f4c30' : '#68462c');
      // Plank horizontal seams
      px(ctx, x, y + 7, TILE_SIZE, 1, '#583922');
      px(ctx, x, y + 15, TILE_SIZE, 1, '#583922');
      // Staggered vertical plank joint
      const seamX = (tx * 7 + ty * 5) % 12 + 2;
      px(ctx, x + seamX, y, 1, 7, '#5a3b23');
      px(ctx, x + ((seamX + 6) % 14) + 1, y + 8, 1, 7, '#5a3b23');
    }
  }

  // Zone Rugs
  // Zone A: Engineering & IDE Bay
  drawRug(ctx, 2, 3, 9, 9, '#233149', '#324566', '#4a6591');
  // Zone B: Browser & Design Studio
  drawRug(ctx, 13, 3, 8, 9, '#2c2742', '#3f385c', '#5e5385');
  // Zone C: Lounge & Sofa
  drawRug(ctx, 22, 10, 4, 5, '#243c35', '#34554b', '#4d7c6e');
  // Zone D: Meeting nook
  drawRug(ctx, 5, 13, 5, 3, '#3b2f2f', '#524141', '#735a5a');

  // Outer perimeter & top wall
  px(ctx, 0, 0, worldW, TILE_SIZE * 2, '#1e2230');
  // Upper wall plaster
  px(ctx, TILE_SIZE, 4, worldW - TILE_SIZE * 2, 18, '#2b3145');
  // Wood wainscoting
  px(ctx, TILE_SIZE, 22, worldW - TILE_SIZE * 2, 9, '#4c3525');
  px(ctx, TILE_SIZE, 22, worldW - TILE_SIZE * 2, 1, '#6d4c36');
  px(ctx, TILE_SIZE, 31, worldW - TILE_SIZE * 2, 1, '#1a1512');

  // Side and bottom border walls
  px(ctx, 0, 0, TILE_SIZE, worldH, '#181b26');
  px(ctx, worldW - TILE_SIZE, 0, TILE_SIZE, worldH, '#181b26');
  px(ctx, 0, worldH - TILE_SIZE, worldW, TILE_SIZE, '#181b26');
  // Inner rim highlight
  px(ctx, TILE_SIZE - 1, TILE_SIZE * 2, 1, worldH - TILE_SIZE * 3, '#2f354a');
  px(ctx, worldW - TILE_SIZE, TILE_SIZE * 2, 1, worldH - TILE_SIZE * 3, '#2f354a');
  px(ctx, TILE_SIZE, worldH - TILE_SIZE, worldW - TILE_SIZE * 2, 1, '#2f354a');

  // 3 Twilight Windows on the back wall
  const windowCols = [3, 12, 19];
  windowCols.forEach((col) => {
    const wx = col * TILE_SIZE;
    const wy = 5;
    const ww = 3 * TILE_SIZE;
    const wh = 16;
    px(ctx, wx - 1, wy - 1, ww + 2, wh + 2, '#151821');
    px(ctx, wx, wy, ww, wh, '#172542');
    px(ctx, wx, wy + 10, ww, 6, '#23365e');
    // Distant city silhouette
    px(ctx, wx + 4, wy + 8, 6, 8, '#10182b');
    px(ctx, wx + 13, wy + 6, 8, 10, '#0d1424');
    px(ctx, wx + 25, wy + 9, 7, 7, '#10182b');
    px(ctx, wx + 36, wy + 7, 8, 9, '#0d1424');
    // Tiny lit building windows
    px(ctx, wx + 15, wy + 8, 1, 1, '#fbbf24');
    px(ctx, wx + 18, wy + 10, 1, 1, '#fbbf24');
    px(ctx, wx + 38, wy + 9, 1, 1, '#38bdf8');
    // Window mullion bars
    px(ctx, wx + ww / 2, wy, 2, wh, '#333b52');
    px(ctx, wx, wy + wh - 1, ww, 2, '#505b7a');
  });

  // Whiteboard on wall (cols 7..9)
  const wbX = 7 * TILE_SIZE;
  px(ctx, wbX, 6, 42, 15, '#475569');
  px(ctx, wbX + 1, 7, 40, 13, '#e2e8f0');
  // Sticky notes & diagram lines
  px(ctx, wbX + 4, 9, 5, 5, '#fbbf24');
  px(ctx, wbX + 11, 9, 5, 5, '#34d399');
  px(ctx, wbX + 18, 9, 5, 5, '#fb7185');
  px(ctx, wbX + 4, 16, 14, 1, '#64748b');
  px(ctx, wbX + 26, 10, 10, 1, '#3b82f6');
  px(ctx, wbX + 26, 13, 8, 1, '#3b82f6');

  // Pixel wall clock (col 16)
  const ckX = 16 * TILE_SIZE + 3;
  px(ctx, ckX, 8, 10, 10, '#334155');
  px(ctx, ckX + 1, 9, 8, 8, '#f8fafc');
  px(ctx, ckX + 4, 11, 1, 3, '#0f172a');
  px(ctx, ckX + 5, 13, 2, 1, '#ef4444');

  // Wall sign above coffee corner
  px(ctx, 23 * TILE_SIZE + 2, 8, 28, 9, '#2b1e16');
  px(ctx, 23 * TILE_SIZE + 3, 9, 26, 7, '#4a3223');
  px(ctx, 23 * TILE_SIZE + 6, 12, 20, 1, '#fbbf24');
}

export function drawAnimatedWindowStars(
  ctx: CanvasRenderingContext2D,
  timeSec: number
) {
  const windowCols = [3, 12, 19];
  windowCols.forEach((col, wIdx) => {
    const wx = col * TILE_SIZE;
    const wy = 5;
    const twinkle = Math.sin(timeSec * 2.2 + wIdx * 1.7) > 0.2;
    px(ctx, wx + 7, wy + 3, 1, 1, twinkle ? '#fef08a' : '#64748b');
    px(ctx, wx + 29, wy + 2, 1, 1, twinkle ? '#64748b' : '#e2e8f0');
    px(ctx, wx + 42, wy + 4, 1, 1, '#cbd5e1');
  });
}

export function drawFurniture(
  ctx: CanvasRenderingContext2D,
  timeSec: number,
  workers: AgentWorker[]
) {
  // Draw non-desk props first
  for (const prop of FURNITURE_PROPS) {
    const x = prop.x * TILE_SIZE;
    const y = prop.y * TILE_SIZE;
    if (prop.kind === 'plant') {
      // Terracotta pot & lush pixel leaves
      px(ctx, x + 5, y + 10, 6, 5, '#b45309');
      px(ctx, x + 4, y + 9, 8, 2, '#d97706');
      px(ctx, x + 3, y + 4, 4, 5, '#15803d');
      px(ctx, x + 8, y + 3, 5, 5, '#16a34a');
      px(ctx, x + 5, y + 1, 5, 6, '#22c55e');
      px(ctx, x + 6, y + 2, 2, 2, '#4ade80');
    } else if (prop.kind === 'server_rack') {
      px(ctx, x + 2, y - 4, 12, 19, '#0f172a');
      px(ctx, x + 3, y - 3, 10, 17, '#1e293b');
      for (let i = 0; i < 4; i++) {
        const sy = y - 1 + i * 4;
        px(ctx, x + 4, sy, 8, 2, '#0f172a');
        const blink = Math.sin(timeSec * 5 + i * 1.9) > 0;
        px(ctx, x + 5, sy, 1, 1, blink ? '#22c55e' : '#14532d');
        px(ctx, x + 7, sy, 1, 1, i === 2 && !blink ? '#f59e0b' : '#38bdf8');
      }
    } else if (prop.kind === 'coffee_counter') {
      px(ctx, x, y + 2, 32, 13, '#473022');
      px(ctx, x, y + 2, 32, 3, '#7c553c');
      // Espresso machine
      px(ctx, x + 4, y - 4, 10, 9, '#475569');
      px(ctx, x + 5, y - 3, 8, 5, '#94a3b8');
      px(ctx, x + 7, y + 1, 4, 2, '#0f172a');
      // Power LED
      px(ctx, x + 11, y - 2, 1, 1, '#ef4444');
      // Mug on tray
      px(ctx, x + 8, y + 2, 3, 3, '#f8fafc');
      // Rising steam
      const steamOffset = Math.floor(timeSec * 3) % 4;
      px(ctx, x + 9, y - 6 - steamOffset, 1, 2, 'rgba(248, 250, 252, 0.55)');
      px(ctx, x + 19, y + 1, 4, 3, '#f59e0b'); // cookie jar
    } else if (prop.kind === 'water_cooler') {
      px(ctx, x + 4, y + 4, 8, 11, '#cbd5e1');
      px(ctx, x + 5, y - 2, 6, 6, '#38bdf8');
      px(ctx, x + 6, y - 1, 2, 4, '#bae6fd');
      px(ctx, x + 6, y + 8, 1, 2, '#2563eb');
      px(ctx, x + 9, y + 8, 1, 2, '#ef4444');
    } else if (prop.kind === 'bookshelf') {
      px(ctx, x + 2, y, 12, 30, '#5c3c24');
      px(ctx, x + 3, y + 2, 10, 26, '#3b2516');
      const bookColors = ['#ef4444', '#3b82f6', '#10b981', '#f59e0b', '#a855f7'];
      for (let row = 0; row < 3; row++) {
        for (let b = 0; b < 4; b++) {
          px(
            ctx,
            x + 4 + b * 2,
            y + 4 + row * 8,
            2,
            5,
            bookColors[(row + b) % bookColors.length]
          );
        }
      }
    } else if (prop.kind === 'printer') {
      px(ctx, x + 2, y + 4, 12, 10, '#334155');
      px(ctx, x + 3, y + 2, 10, 6, '#94a3b8');
      px(ctx, x + 5, y + 1, 6, 2, '#f8fafc');
      px(ctx, x + 11, y + 5, 1, 1, '#22c55e');
    } else if (prop.kind === 'sofa') {
      // Cozy lounge sofa with cushions + side coffee table
      px(ctx, x + 1, y + 1, 30, 14, '#7f1d1d');
      px(ctx, x + 3, y + 2, 26, 5, '#991b1b');
      px(ctx, x + 3, y + 7, 12, 6, '#dc2626');
      px(ctx, x + 16, y + 7, 13, 6, '#dc2626');
      px(ctx, x + 15, y + 7, 1, 6, '#7f1d1d'); // cushion seam
      px(ctx, x + 4, y + 4, 4, 3, '#fbbf24'); // throw pillow
      px(ctx, x + 1, y + 4, 3, 9, '#b91c1c');
      px(ctx, x + 28, y + 4, 3, 9, '#b91c1c');
    } else if (prop.kind === 'meeting_table') {
      // Surrounding stools
      px(ctx, x - 6, y + 5, 5, 5, '#334155');
      px(ctx, x + 33, y + 5, 5, 5, '#334155');
      px(ctx, x + 7, y - 4, 5, 4, '#334155');
      px(ctx, x + 20, y - 4, 5, 4, '#334155');
      // Table body
      px(ctx, x + 1, y + 2, 30, 12, '#473022');
      px(ctx, x + 2, y + 3, 28, 10, '#85583a');
      // Notepad, coffee mug & laptop
      px(ctx, x + 6, y + 5, 5, 4, '#f8fafc');
      px(ctx, x + 13, y + 7, 2, 2, '#f59e0b');
      px(ctx, x + 18, y + 5, 7, 5, '#334155');
      px(ctx, x + 19, y + 6, 5, 3, '#38bdf8');
    }
  }

  // Draw Workstations (Desks + Chairs + Live Screen Visuals)
  for (const desk of DESK_SLOTS) {
    const x = desk.deskTile.x * TILE_SIZE;
    const y = desk.deskTile.y * TILE_SIZE;
    const occupant = workers.find((w) => w.deskId === desk.id);

    // Desk shadow & legs
    px(ctx, x + 1, y + 14, 30, 2, 'rgba(0,0,0,0.32)');
    px(ctx, x + 2, y + 11, 3, 4, '#292d3d');
    px(ctx, x + 27, y + 11, 3, 4, '#292d3d');

    // Desk wood top
    px(ctx, x, y + 2, 32, 11, '#5d4029');
    px(ctx, x + 1, y + 3, 30, 8, '#8a603e');

    // Monitor setup
    const monX = x + 5;
    const monY = y - 4;
    px(ctx, monX + 6, monY + 8, 4, 2, '#334155'); // stand
    px(ctx, monX, monY, 16, 9, '#1e293b'); // bezel
    drawMonitorScreen(
      ctx,
      monX + 1,
      monY + 1,
      14,
      7,
      occupant?.category ?? desk.preferredCategory,
      occupant?.status ?? 'idle',
      timeSec
    );

    // Secondary mini screen or desk prop
    if (desk.preferredCategory === 'ide' || desk.preferredCategory === 'browser') {
      px(ctx, x + 22, monY + 1, 8, 7, '#1e293b');
      drawMonitorScreen(
        ctx,
        x + 23,
        monY + 2,
        6,
        5,
        'terminal',
        occupant?.status ?? 'idle',
        timeSec + 1.3
      );
    } else {
      // Desk lamp or mug
      px(ctx, x + 24, y + 4, 3, 3, '#e2e8f0');
      px(ctx, x + 25, y + 5, 1, 1, '#78350f');
    }

    // Keyboard & Mouse
    px(ctx, x + 7, y + 7, 10, 2, '#cbd5e1');
    px(ctx, x + 19, y + 7, 2, 2, '#94a3b8');

    // Office Chair at seatTile
    const cx = desk.seatTile.x * TILE_SIZE + 4;
    const cy = desk.seatTile.y * TILE_SIZE + 4;
    px(ctx, cx, cy + 2, 8, 7, '#1e293b');
    px(ctx, cx + 1, cy + 3, 6, 5, '#334155');
  }
}

function drawMonitorScreen(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  category: ToolCategory,
  status: AgentWorker['status'],
  timeSec: number
) {
  if (status === 'idle' || status === 'paused') {
    px(ctx, x, y, w, h, '#0f172a');
    // Dim standby dot
    px(ctx, x + w - 2, y + h - 2, 1, 1, '#475569');
    return;
  }

  if (status === 'needs_help' || status === 'error') {
    const flash = Math.sin(timeSec * 6) > 0;
    px(ctx, x, y, w, h, status === 'error' ? '#450a0a' : '#451a03');
    px(
      ctx,
      x + Math.floor(w / 2) - 1,
      y + 1,
      2,
      Math.max(2, h - 3),
      flash ? '#fbbf24' : '#ef4444'
    );
    px(ctx, x + Math.floor(w / 2) - 1, y + h - 2, 2, 1, '#ffffff');
    return;
  }

  // Active working screen visuals tailored to tool category
  if (category === 'ide') {
    px(ctx, x, y, w, h, '#0f172a');
    px(ctx, x, y, 2, h, '#1e293b'); // sidebar
    const scroll = Math.floor(timeSec * 3) % 3;
    const lineColors = ['#38bdf8', '#34d399', '#fbbf24', '#c084fc'];
    for (let row = 0; row < h - 1; row += 2) {
      const len = 3 + ((row * 3 + scroll) % Math.max(3, w - 5));
      px(
        ctx,
        x + 3,
        y + 1 + row,
        Math.min(len, w - 4),
        1,
        lineColors[(row + scroll) % lineColors.length]
      );
    }
  } else if (category === 'browser') {
    px(ctx, x, y, w, h, '#f8fafc');
    px(ctx, x, y, w, 2, '#cbd5e1'); // browser tab bar
    px(ctx, x + 1, y + 3, w - 2, 2, '#3b82f6'); // hero block
    const pulse = Math.floor(timeSec * 2) % 2;
    px(ctx, x + 1, y + 6, 5, 1, pulse ? '#64748b' : '#94a3b8');
  } else if (category === 'terminal') {
    px(ctx, x, y, w, h, '#052e16');
    const blink = Math.sin(timeSec * 5) > 0;
    px(ctx, x + 1, y + 1, Math.max(2, w - 4), 1, '#4ade80');
    px(ctx, x + 1, y + 3, Math.max(2, w - 6), 1, '#22c55e');
    if (blink) {
      px(ctx, x + 1, y + h - 2, 2, 1, '#86efac');
    }
  } else {
    // Design / Other
    px(ctx, x, y, w, h, '#1e1b4b');
    px(ctx, x + 2, y + 1, w - 4, h - 2, '#6366f1');
    px(ctx, x + 3, y + 2, 4, 3, '#f472b6');
  }
}

export function drawWorkerCharacter(
  ctx: CanvasRenderingContext2D,
  worker: AgentWorker,
  timeSec: number,
  nowMs: number,
  isSelected: boolean,
  isHovered: boolean
) {
  const baseX = worker.pos.x * TILE_SIZE;
  const baseY = worker.pos.y * TILE_SIZE;
  const isWalking = worker.path.length > 0;
  const isTyping =
    worker.behaviorState === 'at_desk' && worker.status === 'working';
  const needsHelp =
    worker.status === 'needs_help' || worker.status === 'error';
  const celebrating = worker.celebrateUntil > nowMs;

  // Vertical bounce
  let bobY = 0;
  if (isWalking) {
    bobY = Math.abs(Math.sin(timeSec * 12)) * -1.5;
  } else if (needsHelp) {
    bobY = Math.abs(Math.sin(timeSec * 6)) * -2;
  } else if (celebrating) {
    bobY = Math.abs(Math.sin(timeSec * 10)) * -2.5;
  } else if (isTyping) {
    bobY = Math.sin(timeSec * 8) > 0.6 ? -0.5 : 0;
  }

  const x = Math.round(baseX + 2);
  const y = Math.round(baseY - 4 + bobY);

  // Selection or hover reticle under character
  if (isSelected || isHovered) {
    const color = isSelected ? '#fbbf24' : 'rgba(248, 250, 252, 0.75)';
    const rx = Math.round(baseX);
    const ry = Math.round(baseY + 2);
    px(ctx, rx, ry, 4, 1, color);
    px(ctx, rx, ry, 1, 4, color);
    px(ctx, rx + 12, ry, 4, 1, color);
    px(ctx, rx + 15, ry, 1, 4, color);
    px(ctx, rx, ry + 12, 4, 1, color);
    px(ctx, rx, ry + 9, 1, 4, color);
    px(ctx, rx + 12, ry + 12, 4, 1, color);
    px(ctx, rx + 15, ry + 9, 1, 4, color);
  }

  // Soft floor shadow
  px(ctx, Math.round(baseX + 3), Math.round(baseY + 12), 10, 3, 'rgba(0,0,0,0.35)');

  // Legs
  const stride = isWalking ? Math.round(Math.sin(timeSec * 14) * 1.5) : 0;
  px(ctx, x + 3, y + 13 + (stride > 0 ? -1 : 0), 2, 4, '#1e293b');
  px(ctx, x + 7, y + 13 + (stride < 0 ? -1 : 0), 2, 4, '#1e293b');

  // Torso (shirt)
  px(ctx, x + 2, y + 8, 8, 6, worker.avatar.shirtColor);
  // Subtle shirt shading
  px(ctx, x + 2, y + 12, 8, 2, 'rgba(0,0,0,0.2)');

  // Arms (animated based on state)
  if (needsHelp || celebrating) {
    const wave = Math.sin(timeSec * 10) > 0 ? -3 : -1;
    px(ctx, x, y + 6 + wave, 2, 4, worker.avatar.shirtColor);
    px(ctx, x + 10, y + 6 - wave, 2, 4, worker.avatar.shirtColor);
    px(ctx, x, y + 5 + wave, 2, 2, worker.avatar.skinTone);
    px(ctx, x + 10, y + 5 - wave, 2, 2, worker.avatar.skinTone);
  } else if (isTyping) {
    const leftTap = Math.sin(timeSec * 18) > 0 ? -1 : 0;
    const rightTap = Math.cos(timeSec * 18) > 0 ? -1 : 0;
    px(ctx, x + 1, y + 8 + leftTap, 2, 3, worker.avatar.skinTone);
    px(ctx, x + 9, y + 8 + rightTap, 2, 3, worker.avatar.skinTone);
  } else if (worker.behaviorState === 'at_coffee') {
    px(ctx, x + 1, y + 9, 2, 4, worker.avatar.shirtColor);
    px(ctx, x + 9, y + 9, 3, 3, worker.avatar.shirtColor);
    // Tiny coffee mug in hand
    px(ctx, x + 11, y + 10, 3, 3, '#f8fafc');
    px(ctx, x + 12, y + 10, 1, 1, '#78350f');
  } else {
    px(ctx, x + 1, y + 9, 2, 4, worker.avatar.shirtColor);
    px(ctx, x + 9, y + 9, 2, 4, worker.avatar.shirtColor);
  }

  // Head
  px(ctx, x + 2, y + 1, 8, 7, worker.avatar.skinTone);

  // Hair styles
  const hc = worker.avatar.hairColor;
  if (worker.avatar.hairStyle === 'cap') {
    px(ctx, x + 2, y, 8, 3, '#dc2626');
    px(ctx, x + 1, y + 2, 10, 1, '#991b1b');
  } else if (worker.avatar.hairStyle === 'spiky') {
    px(ctx, x + 2, y, 8, 3, hc);
    px(ctx, x + 3, y - 1, 2, 1, hc);
    px(ctx, x + 7, y - 1, 2, 1, hc);
  } else if (worker.avatar.hairStyle === 'bob') {
    px(ctx, x + 1, y, 10, 3, hc);
    px(ctx, x + 1, y + 3, 1, 4, hc);
    px(ctx, x + 10, y + 3, 1, 4, hc);
  } else if (worker.avatar.hairStyle === 'bun') {
    px(ctx, x + 2, y, 8, 3, hc);
    px(ctx, x + 4, y - 2, 4, 2, hc);
  } else {
    px(ctx, x + 2, y, 8, 3, hc);
  }

  // Eyes (unless facing away)
  if (worker.facing !== 'up') {
    const blink = Math.sin(timeSec * 0.9 + x) > 0.96;
    if (!blink) {
      px(ctx, x + 4, y + 4, 1, 2, '#0f172a');
      px(ctx, x + 7, y + 4, 1, 2, '#0f172a');
    }
    if (worker.avatar.accessory === 'glasses') {
      px(ctx, x + 3, y + 4, 3, 2, 'rgba(56, 189, 248, 0.45)');
      px(ctx, x + 6, y + 4, 3, 2, 'rgba(56, 189, 248, 0.45)');
    }
  } else {
    // Back of head hair fill
    px(ctx, x + 2, y + 2, 8, 4, hc);
  }

  // Headphones accessory
  if (worker.avatar.accessory === 'headphones') {
    px(ctx, x + 1, y + 2, 1, 4, '#38bdf8');
    px(ctx, x + 10, y + 2, 1, 4, '#38bdf8');
    px(ctx, x + 2, y, 8, 1, '#0284c7');
  }

  // Overhead Pixel Speech / Status Bubbles
  drawWorkerBubble(ctx, worker, x + 6, y - 4, timeSec, nowMs);
}

function drawWorkerBubble(
  ctx: CanvasRenderingContext2D,
  worker: AgentWorker,
  cx: number,
  topY: number,
  timeSec: number,
  nowMs: number
) {
  if (worker.celebrateUntil > nowMs) {
    // Emerald checkmark celebration bubble
    const by = topY - 10;
    px(ctx, cx - 5, by, 10, 8, '#065f46');
    px(ctx, cx - 4, by + 1, 8, 6, '#10b981');
    px(ctx, cx - 1, by + 7, 2, 2, '#065f46');
    // Checkmark pixels
    px(ctx, cx - 2, by + 4, 1, 1, '#ffffff');
    px(ctx, cx - 1, by + 5, 1, 1, '#ffffff');
    px(ctx, cx, by + 4, 1, 1, '#ffffff');
    px(ctx, cx + 1, by + 3, 1, 1, '#ffffff');
    px(ctx, cx + 2, by + 2, 1, 1, '#ffffff');
    return;
  }

  if (worker.status === 'needs_help' || worker.status === 'error') {
    const pulse = Math.sin(timeSec * 6) > 0;
    const by = topY - 11 + (pulse ? -1 : 0);
    const border = worker.status === 'error' ? '#991b1b' : '#b45309';
    const fill = worker.status === 'error' ? '#ef4444' : '#f59e0b';
    px(ctx, cx - 6, by, 12, 9, '#0f172a');
    px(ctx, cx - 5, by + 1, 10, 7, border);
    px(ctx, cx - 4, by + 1, 8, 6, fill);
    px(ctx, cx - 1, by + 8, 2, 2, '#0f172a');
    // Exclamation mark or X
    if (worker.status === 'error') {
      px(ctx, cx - 2, by + 2, 1, 1, '#ffffff');
      px(ctx, cx + 1, by + 2, 1, 1, '#ffffff');
      px(ctx, cx - 1, by + 3, 2, 2, '#ffffff');
      px(ctx, cx - 2, by + 5, 1, 1, '#ffffff');
      px(ctx, cx + 1, by + 5, 1, 1, '#ffffff');
    } else {
      px(ctx, cx - 1, by + 2, 2, 3, '#0f172a');
      px(ctx, cx - 1, by + 6, 2, 1, '#0f172a');
    }
    return;
  }

  if (worker.status === 'paused') {
    const by = topY - 9;
    px(ctx, cx - 5, by, 10, 7, '#1e1b4b');
    px(ctx, cx - 4, by + 1, 8, 5, '#4338ca');
    px(ctx, cx - 2, by + 3, 1, 2, '#e0e7ff');
    px(ctx, cx + 1, by + 3, 1, 2, '#e0e7ff');
    return;
  }

  if (worker.behaviorState === 'at_coffee') {
    const by = topY - 9;
    px(ctx, cx - 5, by, 10, 7, '#3b2516');
    px(ctx, cx - 4, by + 1, 8, 5, '#78350f');
    px(ctx, cx - 2, by + 3, 4, 2, '#fef3c7');
  }
}

export function drawDeskLabels(
  ctx: CanvasRenderingContext2D,
  workers: AgentWorker[],
  showLabels: boolean,
  scale: number
) {
  if (!showLabels) return;
  ctx.save();
  ctx.font = `600 ${Math.max(9, Math.round(4.5 * scale))}px "Plus Jakarta Sans", "Noto Sans TC", sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';

  for (const w of workers) {
    const wx = (w.pos.x * TILE_SIZE + 8) * scale;
    const wy = (w.pos.y * TILE_SIZE + 14) * scale;
    const text = w.name;
    const metrics = ctx.measureText(text);
    const padX = 5;
    const boxW = metrics.width + padX * 2;
    const boxH = Math.max(14, Math.round(6.5 * scale));

    ctx.fillStyle =
      w.status === 'needs_help' || w.status === 'error'
        ? 'rgba(180, 83, 9, 0.92)'
        : 'rgba(15, 23, 42, 0.84)';
    ctx.fillRect(Math.round(wx - boxW / 2), Math.round(wy), Math.round(boxW), boxH);

    ctx.strokeStyle =
      w.status === 'needs_help' || w.status === 'error'
        ? 'rgba(251, 191, 36, 0.85)'
        : 'rgba(71, 85, 105, 0.6)';
    ctx.lineWidth = 1;
    ctx.strokeRect(
      Math.round(wx - boxW / 2) + 0.5,
      Math.round(wy) + 0.5,
      Math.round(boxW) - 1,
      boxH - 1
    );

    ctx.fillStyle = '#f8fafc';
    ctx.fillText(text, Math.round(wx), Math.round(wy + 2));
  }
  ctx.restore();
}

export function drawZoneSignage(ctx: CanvasRenderingContext2D, scale: number) {
  ctx.save();
  ctx.font = `700 ${Math.max(9, Math.round(4.2 * scale))}px "Silkscreen", "JetBrains Mono", monospace`;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  ctx.fillStyle = 'rgba(148, 163, 184, 0.72)';

  ctx.fillText('DEV & IDE BAY', 2.2 * TILE_SIZE * scale, 2.2 * TILE_SIZE * scale);
  ctx.fillText('BROWSER & QA', 13.2 * TILE_SIZE * scale, 2.2 * TILE_SIZE * scale);
  ctx.fillText('COFFEE LOUNGE', 22 * TILE_SIZE * scale, 9.2 * TILE_SIZE * scale);
  ctx.fillText('MEETING NOOK', 5 * TILE_SIZE * scale, 12.3 * TILE_SIZE * scale);
  ctx.restore();
}

export function getWorkerHitBounds(worker: AgentWorker) {
  const desk = getDeskSlotById(worker.deskId);
  const charCenter = {
    x: worker.pos.x * TILE_SIZE + 8,
    y: worker.pos.y * TILE_SIZE + 6,
  };
  const deskCenter = {
    x: (desk.deskTile.x + 1) * TILE_SIZE,
    y: desk.deskTile.y * TILE_SIZE + 8,
  };
  return { charCenter, deskCenter };
}
