import type { Point, ToolCategory } from './types';

export const TILE_SIZE = 16;
export const MAP_COLS = 28;
export const MAP_ROWS = 18;

export interface DeskSlot {
  id: string;
  zone: 'engineering' | 'browser_qa' | 'ops';
  preferredCategory: ToolCategory;
  /** Top-left tile of the 2x1 desk */
  deskTile: Point;
  /** Walkable floor tile where the worker sits/stands */
  seatTile: Point;
  label: string;
}

export interface LoungeSpot {
  id: string;
  kind: 'coffee' | 'lounge' | 'meeting';
  tile: Point;
  facing: 'up' | 'down' | 'left' | 'right';
}

export const DESK_SLOTS: DeskSlot[] = [
  {
    id: 'desk-1',
    zone: 'engineering',
    preferredCategory: 'ide',
    deskTile: { x: 3, y: 4 },
    seatTile: { x: 3, y: 5 },
    label: 'DEV-01',
  },
  {
    id: 'desk-2',
    zone: 'engineering',
    preferredCategory: 'ide',
    deskTile: { x: 8, y: 4 },
    seatTile: { x: 8, y: 5 },
    label: 'DEV-02',
  },
  {
    id: 'desk-3',
    zone: 'engineering',
    preferredCategory: 'terminal',
    deskTile: { x: 3, y: 9 },
    seatTile: { x: 3, y: 10 },
    label: 'CLI-03',
  },
  {
    id: 'desk-4',
    zone: 'engineering',
    preferredCategory: 'other',
    deskTile: { x: 8, y: 9 },
    seatTile: { x: 8, y: 10 },
    label: 'OPS-04',
  },
  {
    id: 'desk-5',
    zone: 'browser_qa',
    preferredCategory: 'browser',
    deskTile: { x: 14, y: 4 },
    seatTile: { x: 14, y: 5 },
    label: 'WEB-05',
  },
  {
    id: 'desk-6',
    zone: 'browser_qa',
    preferredCategory: 'design',
    deskTile: { x: 18, y: 4 },
    seatTile: { x: 18, y: 5 },
    label: 'UI-06',
  },
  {
    id: 'desk-7',
    zone: 'browser_qa',
    preferredCategory: 'browser',
    deskTile: { x: 14, y: 9 },
    seatTile: { x: 14, y: 10 },
    label: 'QA-07',
  },
  {
    id: 'desk-8',
    zone: 'ops',
    preferredCategory: 'terminal',
    deskTile: { x: 18, y: 9 },
    seatTile: { x: 18, y: 10 },
    label: 'SRV-08',
  },
];

export const LOUNGE_SPOTS: LoungeSpot[] = [
  { id: 'coffee-1', kind: 'coffee', tile: { x: 23, y: 4 }, facing: 'up' },
  { id: 'coffee-2', kind: 'coffee', tile: { x: 24, y: 4 }, facing: 'up' },
  { id: 'lounge-1', kind: 'lounge', tile: { x: 22, y: 12 }, facing: 'right' },
  { id: 'lounge-2', kind: 'lounge', tile: { x: 24, y: 13 }, facing: 'up' },
  { id: 'meeting-1', kind: 'meeting', tile: { x: 6, y: 13 }, facing: 'down' },
  { id: 'meeting-2', kind: 'meeting', tile: { x: 9, y: 14 }, facing: 'left' },
];

export interface FurnitureProp {
  kind:
    | 'plant'
    | 'server_rack'
    | 'coffee_counter'
    | 'water_cooler'
    | 'sofa'
    | 'meeting_table'
    | 'bookshelf'
    | 'printer';
  x: number;
  y: number;
  w: number;
  h: number;
}

export const FURNITURE_PROPS: FurnitureProp[] = [
  { kind: 'plant', x: 1, y: 2, w: 1, h: 1 },
  { kind: 'plant', x: 12, y: 15, w: 1, h: 1 },
  { kind: 'plant', x: 1, y: 15, w: 1, h: 1 },
  { kind: 'plant', x: 26, y: 15, w: 1, h: 1 },
  { kind: 'server_rack', x: 21, y: 2, w: 1, h: 1 },
  { kind: 'coffee_counter', x: 23, y: 2, w: 2, h: 1 },
  { kind: 'water_cooler', x: 26, y: 2, w: 1, h: 1 },
  { kind: 'bookshelf', x: 26, y: 6, w: 1, h: 2 },
  { kind: 'printer', x: 16, y: 14, w: 1, h: 1 },
  { kind: 'sofa', x: 23, y: 11, w: 2, h: 1 },
  { kind: 'meeting_table', x: 6, y: 14, w: 2, h: 1 },
];

// Precompute collision grid (true = walkable, false = blocked)
const WALKABLE_GRID: boolean[][] = (() => {
  const grid: boolean[][] = Array.from({ length: MAP_ROWS }, () =>
    Array.from({ length: MAP_COLS }, () => true)
  );

  // Perimeter walls & top interior wall (rows 0, 1, 17 and cols 0, 27)
  for (let y = 0; y < MAP_ROWS; y++) {
    for (let x = 0; x < MAP_COLS; x++) {
      if (y <= 1 || y === MAP_ROWS - 1 || x === 0 || x === MAP_COLS - 1) {
        grid[y][x] = false;
      }
    }
  }

  // Block desk tiles (2x1 each)
  for (const desk of DESK_SLOTS) {
    grid[desk.deskTile.y][desk.deskTile.x] = false;
    grid[desk.deskTile.y][desk.deskTile.x + 1] = false;
  }

  // Block furniture props
  for (const prop of FURNITURE_PROPS) {
    for (let dy = 0; dy < prop.h; dy++) {
      for (let dx = 0; dx < prop.w; dx++) {
        const py = prop.y + dy;
        const px = prop.x + dx;
        if (py >= 0 && py < MAP_ROWS && px >= 0 && px < MAP_COLS) {
          grid[py][px] = false;
        }
      }
    }
  }

  return grid;
})();

export function isWalkable(tx: number, ty: number): boolean {
  const rx = Math.round(tx);
  const ry = Math.round(ty);
  if (rx < 0 || rx >= MAP_COLS || ry < 0 || ry >= MAP_ROWS) return false;
  return WALKABLE_GRID[ry][rx];
}

export function getDeskSlotById(deskId: string): DeskSlot {
  return DESK_SLOTS.find((d) => d.id === deskId) ?? DESK_SLOTS[0];
}

/**
 * Shortest-path BFS on the 28x18 office grid.
 * Returns waypoints excluding the starting tile, or [] if already at goal or unreachable.
 */
export function findGridPath(from: Point, to: Point): Point[] {
  const start = { x: Math.round(from.x), y: Math.round(from.y) };
  const goal = { x: Math.round(to.x), y: Math.round(to.y) };

  if (start.x === goal.x && start.y === goal.y) return [];
  if (!isWalkable(goal.x, goal.y)) return [];

  const key = (x: number, y: number) => `${x},${y}`;
  const queue: Point[] = [start];
  const visited = new Set<string>([key(start.x, start.y)]);
  const parent = new Map<string, Point>();

  const dirs = [
    { x: 0, y: -1 },
    { x: 1, y: 0 },
    { x: 0, y: 1 },
    { x: -1, y: 0 },
  ];

  while (queue.length > 0) {
    const curr = queue.shift()!;
    if (curr.x === goal.x && curr.y === goal.y) {
      const path: Point[] = [];
      let trace: Point | undefined = goal;
      while (trace && !(trace.x === start.x && trace.y === start.y)) {
        path.unshift(trace);
        trace = parent.get(key(trace.x, trace.y));
      }
      return path;
    }

    for (const d of dirs) {
      const nx = curr.x + d.x;
      const ny = curr.y + d.y;
      const nKey = key(nx, ny);
      if (!visited.has(nKey) && isWalkable(nx, ny)) {
        visited.add(nKey);
        parent.set(nKey, curr);
        queue.push({ x: nx, y: ny });
      }
    }
  }

  return [];
}
