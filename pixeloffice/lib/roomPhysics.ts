import Matter from 'matter-js';
import PF from 'pathfinding';

export type PetNativePosture = 'stand' | 'sit' | 'loaf';
export type PetBehaviorState =
  | 'loaf'
  | 'sit'
  | 'waking'
  | 'stretching'
  | 'walking'
  | 'trotting'
  | 'grooming'
  | 'petted'
  | 'curious'
  | 'chase_ball';

export type HumanPosturePhase = 'seated' | 'standing_up' | 'walking' | 'seating' | 'standing';

export interface Vec2 {
  x: number;
  y: number;
}

export interface QuadrupedGaitState {
  /** Normalized gait cycle phase [0, 1) driven strictly by ground displacement, never time-only jitter */
  cycle: number;
  /** 'walk' (4-beat lateral) or 'trot' (2-beat diagonal) */
  gaitType: 'walk' | 'trot';
  /** Normalized leg stride offset [-1, 1] for front pair and hind pair (negative = swung forward, positive = pushing back) */
  frontSwing: number;
  hindSwing: number;
  /** Vertical paw lift [0, 1] for front and hind pairs */
  frontLift: number;
  hindLift: number;
  /** Smooth body vertical displacement derived from foot support polygon (tiny, realistic, never bouncy) */
  bodyLift: number;
  /** Smooth stretch factor [0, 1] during stretching state */
  stretchAmt: number;
  /** Smooth crouch/tuck factor [0, 1] when transitioning between stand and loaf */
  tuckAmt: number;
}

export interface PhysicsActorSnapshot {
  x: number;
  y: number;
  vx: number;
  vy: number;
  speed: number;
  pose: string;
  posturePhase: HumanPosturePhase;
  /** Smooth 0..1 seated blend (0 = fully standing, 1 = fully seated) to prevent harsh snap */
  seatBlend: number;
  facing: 'left' | 'right' | 'front';
  useBack: boolean;
  walking: boolean;
  idleActivity?: 'coffee' | 'window' | 'board_glance' | 'stretch' | null;
}

export type PhysicsSeason = 'spring' | 'summer' | 'autumn' | 'winter';

export interface PhysicsPetSnapshot {
  x: number;
  y: number;
  vx: number;
  vy: number;
  speed: number;
  facingLeft: boolean;
  facingBack: boolean;
  behavior: PetBehaviorState;
  nativePosture: PetNativePosture;
  gait: QuadrupedGaitState;
  interactionHeart: boolean;
  pettingTargetSlot: 'supervisor' | 'implementer' | 'reviewer' | null;
  actionLabel: string;
}

export interface RoomPhysicsIntent {
  roomId: string;
  season: PhysicsSeason;
  isNight: boolean;
  isRainOrSnow: boolean;
  isRoomIdle: boolean;
  humans: Record<
    'supervisor' | 'implementer' | 'reviewer',
    {
      standbySpot: string;
      targetSpot: string;
      targetPose: string;
      isWorking: boolean;
      hasError: boolean;
    }
  >;
  pets: {
    dog: { charId: string; nativePosture: PetNativePosture; trait: string };
    cat: { charId: string; nativePosture: PetNativePosture; trait: string };
  };
  spots: Record<string, { x: number; y: number; pose: string }>;
}

export interface RoomPhysicsOutput {
  humans: Record<'supervisor' | 'implementer' | 'reviewer', PhysicsActorSnapshot>;
  pets: {
    dog: PhysicsPetSnapshot;
    cat: PhysicsPetSnapshot;
  };
  toyBall: Vec2;
}

/**
 * Exact native sprite posture catalog for the 24-pet sheets (roster-cats-12.png & roster-dogs-12.png).
 * Verified directly against sprite sheet cells:
 * - Black cat (cell 3) is natively lying down (loaf).
 * - Orange cat (cell 1), Tuxedo (cell 4), British Blue (cell 7), Ragdoll (cell 10), Red Shiba (dog cell 1) are natively sitting.
 * - All other 18 pets are natively standing on 4 legs and can transition stand <-> stretch <-> walk/trot <-> tucked loaf.
 */
export const PET_NATIVE_POSTURE: Record<string, PetNativePosture> = {
  c04_black_cat: 'loaf',
  c02_orange: 'sit',
  c05_tuxedo: 'sit',
  c08_british_blue: 'sit',
  c11_ragdoll: 'sit',
  d02_shiba_red: 'sit',
};

export function getPetNativePosture(charId: string): PetNativePosture {
  return PET_NATIVE_POSTURE[charId] || 'stand';
}

// === Room NavMesh Grid & Obstacles (240x135 logical coordinates, cellSize = 4) ===
const NAV_CELL = 4;
const NAV_W = Math.ceil(240 / NAV_CELL); // 60
const NAV_H = Math.ceil(135 / NAV_CELL); // 34

interface RectObstacle {
  id: string;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

// Physical furniture footprints in 240x135 logical space (common + seasonal)
const BASE_OBSTACLES: readonly RectObstacle[] = [
  { id: 'left_desk', x0: 71, y0: 56, x1: 95, y1: 79 },
  { id: 'right_desk', x0: 98, y0: 44, x1: 121, y1: 66 },
  { id: 'coffee_counter', x0: 168, y0: 56, x1: 196, y1: 82 },
];

const SEASONAL_OBSTACLES: Record<PhysicsSeason, readonly RectObstacle[]> = {
  spring: [{ id: 'irori_hearth', x0: 128, y0: 81, x1: 147, y1: 97 }],
  autumn: [{ id: 'irori_hearth', x0: 128, y0: 81, x1: 147, y1: 97 }],
  winter: [{ id: 'winter_kotatsu', x0: 122, y0: 80, x1: 148, y1: 95 }],
  summer: [
    { id: 'summer_table', x0: 124, y0: 81, x1: 148, y1: 95 },
    { id: 'summer_fan', x0: 172, y0: 64, x1: 186, y1: 86 },
  ],
};

export function getObstaclesForSeason(season: PhysicsSeason): readonly RectObstacle[] {
  return [...BASE_OBSTACLES, ...(SEASONAL_OBSTACLES[season] || SEASONAL_OBSTACLES.spring)];
}

export const ROOM_OBSTACLES: readonly RectObstacle[] = getObstaclesForSeason('spring');

/** Hysteresis facing check for pets moving vertically (prevents front/back flicker). */
export function updatePetFacingBack(current: boolean, vy: number): boolean {
  if (vy < -0.16) return true;
  if (vy > -0.05) return false;
  return current;
}

/** Seasonal warmth/coolness rest spots for dog & cat (safely outside seasonal colliders). */
export function getSeasonalPetSpots(season: PhysicsSeason, role: 'dog' | 'cat'): {
  restPrimary: Vec2;
  restSecondary: Vec2;
  restLabel: string;
} {
  if (season === 'winter') {
    return role === 'cat'
      ? { restPrimary: { x: 162, y: 98 }, restSecondary: { x: 106, y: 102 }, restLabel: '窩在格紋暖桌旁取暖打盹' }
      : { restPrimary: { x: 104, y: 105 }, restSecondary: { x: 160, y: 102 }, restLabel: '趴在暖桌被爐邊安心取暖' };
  }
  if (season === 'summer') {
    return role === 'cat'
      ? { restPrimary: { x: 164, y: 88 }, restSecondary: { x: 86, y: 96 }, restLabel: '靠在立式電扇微風旁乘涼小憩' }
      : { restPrimary: { x: 84, y: 98 }, restSecondary: { x: 162, y: 90 }, restLabel: '趴在清涼杉木地板上吹電扇納涼' };
  }
  return role === 'cat'
    ? { restPrimary: { x: 152, y: 101 }, restSecondary: { x: 116, y: 88 }, restLabel: '蜷在地爐與窗光旁安心打盹' }
    : { restPrimary: { x: 112, y: 103 }, restSecondary: { x: 84, y: 98 }, restLabel: '沐浴在日光與炭火旁趴著小憩' };
}

// Walkable isometric diamond polygon of the wooden corridor + tatami floor
const FLOOR_POLY: [number, number][] = [
  [54, 88],
  [124, 52],
  [184, 84],
  [118, 118],
];

// Seat approach points so humans step around chairs instead of clipping through backrests
const SEAT_APPROACH: Record<string, Vec2> = {
  implSeat: { x: 106, y: 86 },
  revSeat: { x: 131, y: 74 },
};

function pointInPoly(x: number, y: number, poly: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0];
    const yi = poly[i][1];
    const xj = poly[j][0];
    const yj = poly[j][1];
    const intersect = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-6) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

function isPointWalkable(x: number, y: number, margin = 2.0, season: PhysicsSeason = 'spring'): boolean {
  if (!pointInPoly(x, y, FLOOR_POLY)) return false;
  for (const ob of getObstaclesForSeason(season)) {
    if (x >= ob.x0 - margin && x <= ob.x1 + margin && y >= ob.y0 - margin && y <= ob.y1 + margin) {
      return false;
    }
  }
  return true;
}

const cachedSeasonalGrids: Partial<Record<PhysicsSeason, PF.Grid>> = {};
const astarFinder = new PF.AStarFinder({
  allowDiagonal: true,
  dontCrossCorners: true,
});

function getBaseNavGrid(season: PhysicsSeason = 'spring'): PF.Grid {
  const cached = cachedSeasonalGrids[season];
  if (cached) return cached;
  const grid = new PF.Grid(NAV_W, NAV_H);
  for (let gy = 0; gy < NAV_H; gy++) {
    for (let gx = 0; gx < NAV_W; gx++) {
      const wx = gx * NAV_CELL + NAV_CELL * 0.5;
      const wy = gy * NAV_CELL + NAV_CELL * 0.5;
      if (!isPointWalkable(wx, wy, 1.5, season)) {
        grid.setWalkableAt(gx, gy, false);
      }
    }
  }
  cachedSeasonalGrids[season] = grid;
  return grid;
}

function nearestWalkableCell(grid: PF.Grid, gx: number, gy: number): [number, number] {
  const cx = Math.max(0, Math.min(NAV_W - 1, gx));
  const cy = Math.max(0, Math.min(NAV_H - 1, gy));
  if (grid.isWalkableAt(cx, cy)) return [cx, cy];
  for (let r = 1; r <= 8; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        const nx = cx + dx;
        const ny = cy + dy;
        if (nx >= 0 && nx < NAV_W && ny >= 0 && ny < NAV_H && grid.isWalkableAt(nx, ny)) {
          return [nx, ny];
        }
      }
    }
  }
  return [cx, cy];
}

export function findNavPath(from: Vec2, to: Vec2, season: PhysicsSeason = 'spring'): Vec2[] {
  const dist = Math.hypot(to.x - from.x, to.y - from.y);
  if (dist < 3.0) return [{ x: to.x, y: to.y }];

  const grid = getBaseNavGrid(season).clone();
  const [sx, sy] = nearestWalkableCell(grid, Math.floor(from.x / NAV_CELL), Math.floor(from.y / NAV_CELL));
  const [ex, ey] = nearestWalkableCell(grid, Math.floor(to.x / NAV_CELL), Math.floor(to.y / NAV_CELL));

  const raw = astarFinder.findPath(sx, sy, ex, ey, grid);
  if (!raw || raw.length === 0) {
    return [{ x: to.x, y: to.y }];
  }
  const smoothed = PF.Util.smoothenPath(grid, raw);
  const pts: Vec2[] = [];
  for (let i = 1; i < smoothed.length; i++) {
    pts.push({
      x: smoothed[i][0] * NAV_CELL + NAV_CELL * 0.5,
      y: smoothed[i][1] * NAV_CELL + NAV_CELL * 0.5,
    });
  }
  if (pts.length === 0 || Math.hypot(pts[pts.length - 1].x - to.x, pts[pts.length - 1].y - to.y) > 1.2) {
    pts.push({ x: to.x, y: to.y });
  }
  return pts;
}

// === Internal Room Physics World (Matter.js + Utility AI) ===

interface HumanActorSim {
  slotKey: 'supervisor' | 'implementer' | 'reviewer';
  body: Matter.Body;
  currentSpot: string;
  targetSpot: string;
  targetPose: string;
  waypoints: Vec2[];
  posturePhase: HumanPosturePhase;
  phaseTimerMs: number;
  seatBlend: number;
  facing: 'left' | 'right' | 'front';
  useBack: boolean;
  // Smart idle autonomy
  idleActivity: 'coffee' | 'window' | 'board_glance' | 'stretch' | null;
  idleOverrideDest: Vec2 | null;
  nextIdleDecisionMs: number;
}

interface PetActorSim {
  role: 'dog' | 'cat';
  body: Matter.Body;
  nativePosture: PetNativePosture;
  behavior: PetBehaviorState;
  stateTimerMs: number;
  stateDurationMs: number;
  waypoints: Vec2[];
  facingLeft: boolean;
  facingBack: boolean;
  gaitCycle: number;
  stretchAmt: number;
  tuckAmt: number;
  // Utility AI internal needs [0..1]
  energy: number;
  curiosity: number;
  affection: number;
  interactionHeart: boolean;
  pettingTargetSlot: 'supervisor' | 'implementer' | 'reviewer' | null;
  actionLabel: string;
  cozyAnchor: Vec2;
}

interface RoomPhysicsWorld {
  roomId: string;
  engine: Matter.Engine;
  activeSeason: PhysicsSeason;
  seasonalBodies: Matter.Body[];
  humans: Record<'supervisor' | 'implementer' | 'reviewer', HumanActorSim>;
  pets: {
    dog: PetActorSim;
    cat: PetActorSim;
  };
  toyBall: Matter.Body;
  lastUpdateMs: number;
  seed: number;
}

const roomWorlds = new Map<string, RoomPhysicsWorld>();

function hashStr(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function seededRand(seed: number, step: number): number {
  let x = (seed ^ Math.imul(step + 1, 0x9e3779b1)) >>> 0;
  x ^= x >>> 16;
  x = Math.imul(x, 0x85ebca6b) >>> 0;
  x ^= x >>> 13;
  x = Math.imul(x, 0xc2b2ae35) >>> 0;
  x ^= x >>> 16;
  return (x >>> 0) / 4294967296;
}

function isSeatedPose(pose: string): boolean {
  return pose === 'type' || pose === 'sit' || pose === 'head' || pose === 'sofa' || pose === 'mug' || pose === 'cheer' || pose === 'petting';
}

function buildSeasonalBodies(season: PhysicsSeason): Matter.Body[] {
  return (SEASONAL_OBSTACLES[season] || SEASONAL_OBSTACLES.spring).map((ob) =>
    Matter.Bodies.rectangle((ob.x0 + ob.x1) / 2, (ob.y0 + ob.y1) / 2, ob.x1 - ob.x0, ob.y1 - ob.y0, {
      isStatic: true,
      label: ob.id,
      collisionFilter: { category: 0x0001 },
    })
  );
}

function syncWorldSeasonObstacles(world: RoomPhysicsWorld, season: PhysicsSeason): void {
  if (world.activeSeason === season) return;
  for (const b of world.seasonalBodies) {
    Matter.Composite.remove(world.engine.world, b);
  }
  const nextBodies = buildSeasonalBodies(season);
  Matter.Composite.add(world.engine.world, nextBodies);
  world.seasonalBodies = nextBodies;
  world.activeSeason = season;
}

function createRoomWorld(intent: RoomPhysicsIntent, now: number): RoomPhysicsWorld {
  const engine = Matter.Engine.create({
    gravity: { x: 0, y: 0, scale: 0 },
  });

  // Add common static furniture collision bodies + current season's dynamic colliders
  const seasonalBodies = buildSeasonalBodies(intent.season);
  const staticBodies: Matter.Body[] = [
    ...BASE_OBSTACLES.map((ob) =>
      Matter.Bodies.rectangle((ob.x0 + ob.x1) / 2, (ob.y0 + ob.y1) / 2, ob.x1 - ob.x0, ob.y1 - ob.y0, {
        isStatic: true,
        label: ob.id,
        collisionFilter: { category: 0x0001 },
      })
    ),
    ...seasonalBodies,
  ];

  // Isometric room boundary walls (4 angled static edges around FLOOR_POLY)
  for (let i = 0; i < FLOOR_POLY.length; i++) {
    const a = FLOOR_POLY[i];
    const b = FLOOR_POLY[(i + 1) % FLOOR_POLY.length];
    const cx = (a[0] + b[0]) / 2;
    const cy = (a[1] + b[1]) / 2;
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
    staticBodies.push(
      Matter.Bodies.rectangle(cx, cy, len + 4, 3, {
        isStatic: true,
        angle: ang,
        label: `wall_${i}`,
        collisionFilter: { category: 0x0001 },
      })
    );
  }
  Matter.Composite.add(engine.world, staticBodies);

  const seed = hashStr(intent.roomId);

  const makeHuman = (slotKey: 'supervisor' | 'implementer' | 'reviewer'): HumanActorSim => {
    const hIntent = intent.humans[slotKey];
    const startSpot = intent.spots[hIntent.targetSpot] || intent.spots[hIntent.standbySpot] || { x: 120, y: 90, pose: 'stand' };
    const seated = isSeatedPose(hIntent.targetPose);
    const body = Matter.Bodies.circle(startSpot.x, startSpot.y, 3.2, {
      frictionAir: 0.28,
      restitution: 0.1,
      mass: 2.0,
      label: `human_${slotKey}`,
      // Humans collide with furniture (0x0001) and pets/ball, but softly pass each other during narrow corridor crossings
      collisionFilter: { category: 0x0002, mask: 0x0001 | 0x0008 },
    });
    Matter.Composite.add(engine.world, body);
    return {
      slotKey,
      body,
      currentSpot: hIntent.targetSpot,
      targetSpot: hIntent.targetSpot,
      targetPose: hIntent.targetPose,
      waypoints: [],
      posturePhase: seated ? 'seated' : 'standing',
      phaseTimerMs: 0,
      seatBlend: seated ? 1 : 0,
      facing: 'front',
      useBack: hIntent.targetSpot === 'implSeat' || hIntent.targetSpot === 'revSeat',
      idleActivity: null,
      idleOverrideDest: null,
      nextIdleDecisionMs: now + 9000 + Math.round(seededRand(seed, slotKey.length * 17) * 14000),
    };
  };

  const dogPosture = intent.pets.dog.nativePosture;
  const catPosture = intent.pets.cat.nativePosture;
  const dogStart: Vec2 = dogPosture === 'stand' ? { x: 78, y: 97 } : { x: 82, y: 100 };
  const catStart: Vec2 = catPosture === 'loaf' ? { x: 156, y: 100 } : catPosture === 'sit' ? { x: 158, y: 96 } : { x: 156, y: 100 };

  const makePet = (role: 'dog' | 'cat', start: Vec2, nativePosture: PetNativePosture): PetActorSim => {
    const body = Matter.Bodies.circle(start.x, start.y, 2.8, {
      isStatic: false,
      frictionAir: 0.25,
      restitution: 0.15,
      mass: 1.0,
      label: `pet_${role}`,
      collisionFilter: { category: 0x0004, mask: 0x0001 | 0x0004 | 0x0008 },
    });
    Matter.Composite.add(engine.world, body);
    const initBehavior: PetBehaviorState =
      nativePosture === 'loaf' ? 'loaf' : nativePosture === 'sit' ? 'sit' : role === 'cat' ? 'loaf' : 'curious';
    return {
      role,
      body,
      nativePosture,
      behavior: initBehavior,
      stateTimerMs: 0,
      stateDurationMs: 4200 + Math.round(seededRand(seed, role === 'dog' ? 31 : 73) * 4500),
      waypoints: [],
      facingLeft: role === 'cat',
      facingBack: false,
      gaitCycle: 0,
      stretchAmt: 0,
      tuckAmt: initBehavior === 'loaf' ? 1 : 0,
      energy: 0.55 + seededRand(seed, role === 'dog' ? 5 : 9) * 0.35,
      curiosity: 0.5 + seededRand(seed, role === 'dog' ? 11 : 13) * 0.4,
      affection: 0.45 + seededRand(seed, role === 'dog' ? 19 : 23) * 0.4,
      interactionHeart: false,
      pettingTargetSlot: null,
      actionLabel:
        initBehavior === 'loaf'
          ? '在榻榻米暖爐旁安心趴睡'
          : initBehavior === 'sit'
            ? '端坐守望辦公室成員'
            : '四處張望探索房間',
      cozyAnchor: { ...start },
    };
  };

  const toyBall = Matter.Bodies.circle(92, 104, 1.8, {
    frictionAir: 0.045,
    restitution: 0.78,
    mass: 0.25,
    label: 'toy_ball',
    collisionFilter: { category: 0x0008, mask: 0x0001 | 0x0002 | 0x0004 },
  });
  Matter.Composite.add(engine.world, toyBall);

  const world: RoomPhysicsWorld = {
    roomId: intent.roomId,
    engine,
    activeSeason: intent.season,
    seasonalBodies,
    humans: {
      supervisor: makeHuman('supervisor'),
      implementer: makeHuman('implementer'),
      reviewer: makeHuman('reviewer'),
    },
    pets: {
      dog: makePet('dog', dogStart, dogPosture),
      cat: makePet('cat', catStart, catPosture),
    },
    toyBall,
    lastUpdateMs: now,
    seed,
  };

  roomWorlds.set(intent.roomId, world);
  return world;
}

function buildHumanWaypoints(from: Vec2, targetSpot: string, dest: Vec2): Vec2[] {
  const approach = SEAT_APPROACH[targetSpot];
  if (approach) {
    const leg1 = findNavPath(from, approach);
    leg1.push({ x: dest.x, y: dest.y });
    return leg1;
  }
  return findNavPath(from, dest);
}

function stepHuman(
  human: HumanActorSim,
  hIntent: RoomPhysicsIntent['humans']['supervisor'],
  intent: RoomPhysicsIntent,
  dtMs: number,
  now: number,
  seed: number
) {
  const destSpot = intent.spots[hIntent.targetSpot] || intent.spots[hIntent.standbySpot] || { x: 120, y: 90, pose: 'stand' };

  // 1. Backend command priority: if targetSpot changed (e.g. standby -> implSeat), immediately abort idle excursion & replan cleanly!
  if (human.targetSpot !== hIntent.targetSpot) {
    human.targetSpot = hIntent.targetSpot;
    human.targetPose = hIntent.targetPose;
    human.idleActivity = null;
    human.idleOverrideDest = null;

    // If currently seated, stand up smoothly first (220ms) before walking
    if (human.posturePhase === 'seated' || human.posturePhase === 'seating') {
      human.posturePhase = 'standing_up';
      human.phaseTimerMs = 0;
    } else {
      human.posturePhase = 'walking';
    }
    // Step out through approach anchor if leaving a desk seat
    const leaveAnchor = SEAT_APPROACH[human.currentSpot];
    const startPt = { x: human.body.position.x, y: human.body.position.y };
    const path = buildHumanWaypoints(leaveAnchor && Math.hypot(startPt.x - leaveAnchor.x, startPt.y - leaveAnchor.y) < 16 ? leaveAnchor : startPt, hIntent.targetSpot, destSpot);
    if (leaveAnchor && Math.hypot(startPt.x - leaveAnchor.x, startPt.y - leaveAnchor.y) < 16) {
      path.unshift({ x: leaveAnchor.x, y: leaveAnchor.y });
    }
    human.waypoints = path;
  } else {
    human.targetPose = hIntent.targetPose;
  }

  // 2. Smart Idle Autonomy when NOT working (stand up to get coffee, glance at window/board, stretch, then return)
  if (!hIntent.isWorking && !hIntent.hasError && intent.isRoomIdle && human.waypoints.length === 0 && now >= human.nextIdleDecisionMs) {
    const r = seededRand(seed, Math.floor(now / 1000) + human.slotKey.length * 13);
    human.nextIdleDecisionMs = now + 13000 + Math.round(r * 14000);

    if (human.idleActivity) {
      // Return to standby cushion
      human.idleActivity = null;
      human.idleOverrideDest = null;
      human.posturePhase = 'walking';
      human.waypoints = buildHumanWaypoints({ x: human.body.position.x, y: human.body.position.y }, hIntent.targetSpot, destSpot);
    } else if (r < 0.24 && human.slotKey === 'supervisor') {
      // Supervisor walks to pour-over coffee bar
      human.idleActivity = 'coffee';
      human.idleOverrideDest = { x: 162, y: 80 };
      human.posturePhase = human.seatBlend > 0.3 ? 'standing_up' : 'walking';
      human.phaseTimerMs = 0;
      human.waypoints = findNavPath({ x: human.body.position.x, y: human.body.position.y }, human.idleOverrideDest);
    } else if (r >= 0.24 && r < 0.44 && human.slotKey === 'implementer') {
      // Implementer walks over near window/corridor to stretch & look outside
      human.idleActivity = 'window';
      human.idleOverrideDest = { x: 96, y: 92 };
      human.posturePhase = human.seatBlend > 0.3 ? 'standing_up' : 'walking';
      human.phaseTimerMs = 0;
      human.waypoints = findNavPath({ x: human.body.position.x, y: human.body.position.y }, human.idleOverrideDest);
    } else if (r >= 0.44 && r < 0.62 && human.slotKey === 'reviewer') {
      // Reviewer glances at the Kanban board
      human.idleActivity = 'board_glance';
      human.idleOverrideDest = { x: 146, y: 72 };
      human.posturePhase = human.seatBlend > 0.3 ? 'standing_up' : 'walking';
      human.phaseTimerMs = 0;
      human.waypoints = findNavPath({ x: human.body.position.x, y: human.body.position.y }, human.idleOverrideDest);
    }
  }

  // 3. Advance posture phase & steering
  if (human.posturePhase === 'standing_up') {
    human.phaseTimerMs += dtMs;
    const u = Math.min(1, human.phaseTimerMs / 220);
    human.seatBlend = 1 - u;
    Matter.Body.setVelocity(human.body, { x: 0, y: 0 });
    if (u >= 1) {
      human.posturePhase = human.waypoints.length > 0 ? 'walking' : 'standing';
      human.phaseTimerMs = 0;
    }
    return;
  }

  if (human.posturePhase === 'walking') {
    human.seatBlend = Math.max(0, human.seatBlend - dtMs / 180);
    if (human.waypoints.length === 0) {
      const wantSeated = !human.idleActivity && isSeatedPose(human.targetPose);
      human.currentSpot = human.idleActivity ? 'idle' : human.targetSpot;
      human.posturePhase = wantSeated ? 'seating' : 'standing';
      human.phaseTimerMs = 0;
      Matter.Body.setVelocity(human.body, { x: 0, y: 0 });
      return;
    }

    const wp = human.waypoints[0];
    const dx = wp.x - human.body.position.x;
    const dy = wp.y - human.body.position.y;
    const dist = Math.hypot(dx, dy);
    const reachRadius = human.waypoints.length === 1 ? 1.2 : 2.8;

    if (dist <= reachRadius) {
      human.waypoints.shift();
      if (human.waypoints.length === 0) {
        Matter.Body.setPosition(human.body, { x: wp.x, y: wp.y });
        Matter.Body.setVelocity(human.body, { x: 0, y: 0 });
        const wantSeated = !human.idleActivity && isSeatedPose(human.targetPose);
        human.currentSpot = human.idleActivity ? 'idle' : human.targetSpot;
        human.posturePhase = wantSeated ? 'seating' : 'standing';
        human.phaseTimerMs = 0;
        return;
      }
    } else {
      // Smooth arrival damping on final waypoint, constant comfortable walking speed otherwise
      const maxSpeed = 0.72; // logical units per Matter step (~26 units/sec)
      const speed = human.waypoints.length === 1 && dist < 8 ? Math.max(0.22, (dist / 8) * maxSpeed) : maxSpeed;
      Matter.Body.setVelocity(human.body, {
        x: (dx / dist) * speed,
        y: (dy / dist) * speed,
      });
      // Facing with hysteresis to prevent flicker
      if (Math.abs(dx) > 0.4) {
        human.facing = dx < 0 ? 'left' : 'right';
      }
      human.useBack = dy < -0.25;
    }
    return;
  }

  if (human.posturePhase === 'seating') {
    human.phaseTimerMs += dtMs;
    const u = Math.min(1, human.phaseTimerMs / 240);
    human.seatBlend = u;
    Matter.Body.setVelocity(human.body, { x: 0, y: 0 });
    if (u >= 1) {
      human.posturePhase = 'seated';
      human.seatBlend = 1;
      human.phaseTimerMs = 0;
    }
    return;
  }

  // Stationary ('seated' or 'standing')
  const wantSeated = !human.idleActivity && isSeatedPose(human.targetPose);
  if (wantSeated && human.posturePhase !== 'seated') {
    human.posturePhase = 'seating';
    human.phaseTimerMs = 0;
  } else if (!wantSeated && human.posturePhase === 'seated') {
    human.posturePhase = 'standing_up';
    human.phaseTimerMs = 0;
  }

  // Keep locked gently on anchor when stationary so physics collisions never drift seated humans off cushions/chairs
  const anchor = human.idleOverrideDest || destSpot;
  const adx = anchor.x - human.body.position.x;
  const ady = anchor.y - human.body.position.y;
  if (Math.hypot(adx, ady) > 0.35) {
    Matter.Body.setPosition(human.body, {
      x: human.body.position.x + adx * 0.25,
      y: human.body.position.y + ady * 0.25,
    });
  }
  Matter.Body.setVelocity(human.body, { x: 0, y: 0 });
}

function chooseNextPetGoal(
  pet: PetActorSim,
  world: RoomPhysicsWorld,
  intent: RoomPhysicsIntent,
  now: number
) {
  const stepSeed = Math.floor(now / 1300) + (pet.role === 'dog' ? 101 : 211);
  const r = seededRand(world.seed, stepSeed);

  // === Smart Utility AI for ALL room pets (every opened room's dog & cat actively roam, stretch, visit humans & chase ball!) ===

  // Rule: Cannot jump directly from loaf/sit to walking! Must wake -> stretch first!
  if (pet.behavior === 'loaf' || pet.behavior === 'sit') {
    pet.behavior = 'waking';
    pet.stateTimerMs = 0;
    pet.stateDurationMs = 900;
    pet.waypoints = [];
    pet.actionLabel = '剛睡醒，抬起頭伸展起身';
    return;
  }
  if (pet.behavior === 'waking') {
    pet.behavior = 'stretching';
    pet.stateTimerMs = 0;
    pet.stateDurationMs = 2200 + Math.round(r * 1000);
    pet.waypoints = [];
    pet.actionLabel = pet.role === 'cat' ? '拱背伸個大懶腰～' : '前趴壓低伸懶腰～';
    return;
  }

  // Update needs: rain/snow/night & winter make pets prefer indoor cozy napping
  const cozyFactor = intent.season === 'winter' || intent.isNight || intent.isRainOrSnow ? 0.28 : 0;
  const wantRest = pet.energy < 0.36 + cozyFactor * 0.45;
  const seasonSpots = getSeasonalPetSpots(intent.season, pet.role);

  // Candidate targets in walkable room space
  const patrolSpotA: Vec2 = pet.role === 'dog' ? { x: 74, y: 95 } : { x: 162, y: 95 };
  const patrolSpotB: Vec2 = pet.role === 'dog' ? { x: 102, y: 106 } : { x: 146, y: 104 };

  // Check if any human is working (dog loves keeping working engineer company) or idle (asking for pets)
  const impl = world.humans.implementer;
  const sup = world.humans.supervisor;

  if (wantRest && pet.behavior !== 'stretching') {
    const restSpot = cozyFactor > 0 || intent.season === 'summer' ? seasonSpots.restPrimary : seasonSpots.restSecondary;
    if (Math.hypot(pet.body.position.x - restSpot.x, pet.body.position.y - restSpot.y) > 5.5) {
      pet.behavior = 'walking';
      pet.waypoints = findNavPath({ x: pet.body.position.x, y: pet.body.position.y }, restSpot, intent.season);
      pet.stateTimerMs = 0;
      pet.stateDurationMs = 7000;
      pet.actionLabel = seasonSpots.restLabel;
      return;
    }
    pet.behavior = 'loaf';
    pet.waypoints = [];
    pet.stateTimerMs = 0;
    pet.stateDurationMs = 6800 + Math.round(r * 5200);
    pet.energy = Math.min(1, pet.energy + 0.55);
    pet.actionLabel = seasonSpots.restLabel;
    return;
  }

  // Dog vs Cat personality differentiation (P=0.971):
  // - Dogs enthusiastically chase the toy ball and greet humans up close
  // - Cats stay aloof with wider personal space, occasionally brushing past humans
  if (pet.role === 'dog' && r < 0.34) {
    const ballPos = { x: world.toyBall.position.x, y: world.toyBall.position.y };
    pet.behavior = 'chase_ball';
    pet.waypoints = findNavPath({ x: pet.body.position.x, y: pet.body.position.y }, ballPos, intent.season);
    pet.stateTimerMs = 0;
    pet.stateDurationMs = 5500;
    pet.energy = Math.max(0.1, pet.energy - 0.25);
    pet.actionLabel = '興奮搖尾小跑追逐玩具球';
    return;
  }

  const visitCutoff = pet.role === 'dog' ? 0.62 : 0.32;
  if (r < visitCutoff) {
    const targetHuman = pet.role === 'dog' ? impl : sup;
    const xOffset = pet.role === 'dog' ? -9 : 16;
    const yOffset = pet.role === 'dog' ? 6 : 9;
    const visitPt: Vec2 = {
      x: Math.max(68, Math.min(172, targetHuman.body.position.x + xOffset)),
      y: Math.max(72, Math.min(109, targetHuman.body.position.y + yOffset)),
    };
    if (Math.hypot(pet.body.position.x - visitPt.x, pet.body.position.y - visitPt.y) > 6.0) {
      pet.behavior = pet.role === 'dog' ? 'trotting' : 'walking';
      pet.waypoints = findNavPath({ x: pet.body.position.x, y: pet.body.position.y }, visitPt, intent.season);
      pet.stateTimerMs = 0;
      pet.stateDurationMs = 6500;
      pet.energy = Math.max(0.1, pet.energy - 0.16);
      pet.actionLabel =
        pet.role === 'dog'
          ? '熱情搖尾跑去迎接夥伴'
          : '保持優雅距離偶爾過去蹭蹭';
      return;
    }
    pet.behavior = 'petted';
    pet.waypoints = [];
    pet.stateTimerMs = 0;
    pet.stateDurationMs = pet.role === 'dog' ? 4600 : 3200;
    pet.affection = Math.min(1, pet.affection + 0.35);
    pet.actionLabel = pet.role === 'dog' ? '在腳邊搖尾巴享受摸摸 ♥' : '輕輕蹭了一下又恢復高冷 ♥';
    return;
  }

  if (r < 0.78) {
    const dest =
      Math.hypot(pet.body.position.x - patrolSpotA.x, pet.body.position.y - patrolSpotA.y) > 10
        ? patrolSpotA
        : patrolSpotB;
    pet.behavior = 'walking';
    pet.waypoints = findNavPath({ x: pet.body.position.x, y: pet.body.position.y }, dest, intent.season);
    pet.stateTimerMs = 0;
    pet.stateDurationMs = 7000;
    pet.energy = Math.max(0.1, pet.energy - 0.14);
    pet.actionLabel = pet.role === 'cat' ? '沿著榻榻米優雅四足巡邏' : '在杉木地板悠閒四足散步';
    return;
  }

  // Groom or sit & watch
  pet.behavior = pet.role === 'cat' ? 'grooming' : 'curious';
  pet.waypoints = [];
  pet.stateTimerMs = 0;
  pet.stateDurationMs = 4000 + Math.round(r * 2500);
  pet.energy = Math.min(1, pet.energy + 0.15);
  pet.actionLabel = pet.role === 'cat' ? '停下來優雅舔爪洗臉' : '豎起耳朵好奇觀察周圍';
}

function stepPet(
  pet: PetActorSim,
  world: RoomPhysicsWorld,
  intent: RoomPhysicsIntent,
  dtMs: number,
  now: number
) {
  pet.stateTimerMs += dtMs;
  pet.interactionHeart = false;
  pet.pettingTargetSlot = null;

  const isLocomoting =
    pet.behavior === 'walking' || pet.behavior === 'trotting' || pet.behavior === 'chase_ball';

  // Smooth posture tuck / stretch interpolation
  const targetTuck = pet.behavior === 'loaf' ? 1 : 0;
  const targetStretch = pet.behavior === 'stretching' ? Math.sin(Math.min(1, pet.stateTimerMs / Math.max(1, pet.stateDurationMs)) * Math.PI) : 0;
  pet.tuckAmt += (targetTuck - pet.tuckAmt) * Math.min(1, dtMs / 260);
  pet.stretchAmt += (targetStretch - pet.stretchAmt) * Math.min(1, dtMs / 160);

  // HARD RULE: Loaf / Sit / Waking / Stretching / Grooming / Petted MUST have zero translational velocity!
  if (!isLocomoting) {
    Matter.Body.setVelocity(pet.body, { x: 0, y: 0 });
    // Gently decay gait cycle back to neutral stance
    pet.gaitCycle = pet.gaitCycle % 1;

    // Check proximity to an idle human for heart interaction
    if (pet.behavior === 'petted' || pet.behavior === 'sit' || pet.behavior === 'loaf') {
      for (const slot of ['implementer', 'supervisor', 'reviewer'] as const) {
        const h = world.humans[slot];
        const hInt = intent.humans[slot];
        if (!hInt.isWorking && h.posturePhase !== 'walking') {
          const d = Math.hypot(h.body.position.x - pet.body.position.x, h.body.position.y - pet.body.position.y);
          if (d < 22 && pet.behavior === 'petted') {
            pet.interactionHeart = true;
            pet.pettingTargetSlot = slot;
            pet.facingLeft = h.body.position.x < pet.body.position.x;
            break;
          }
        }
      }
    }

    if (pet.stateTimerMs >= pet.stateDurationMs) {
      chooseNextPetGoal(pet, world, intent, now);
    }
    return;
  }

  // Locomoting (standing quadruped only!)
  if (pet.waypoints.length === 0 || pet.stateTimerMs >= pet.stateDurationMs) {
    Matter.Body.setVelocity(pet.body, { x: 0, y: 0 });
    // On arrival: transition cleanly to petted / loaf / curious rather than looping walk forever
    if (pet.behavior === 'chase_ball') {
      // Give the Matter.js toy ball a playful nudge!
      const bdx = world.toyBall.position.x - pet.body.position.x;
      const bdy = world.toyBall.position.y - pet.body.position.y;
      const bd = Math.hypot(bdx, bdy);
      if (bd < 8) {
        Matter.Body.applyForce(world.toyBall, world.toyBall.position, {
          x: (bdx / (bd + 0.1)) * 0.0022,
          y: (bdy / (bd + 0.1)) * 0.0022,
        });
      }
    }
    chooseNextPetGoal(pet, world, intent, now);
    return;
  }

  const wp = pet.waypoints[0];
  const dx = wp.x - pet.body.position.x;
  const dy = wp.y - pet.body.position.y;
  const dist = Math.hypot(dx, dy);
  const reachRadius = pet.waypoints.length === 1 ? 2.0 : 3.2;

  if (dist <= reachRadius) {
    pet.waypoints.shift();
    if (pet.waypoints.length === 0) {
      Matter.Body.setVelocity(pet.body, { x: 0, y: 0 });
      return;
    }
  } else {
    const weatherPace = intent.isRainOrSnow || intent.isNight ? 0.78 : 1.0;
    const baseSpeed = (pet.behavior === 'trotting' || pet.behavior === 'chase_ball' ? 0.56 : 0.36) * weatherPace;
    const speed = pet.waypoints.length === 1 && dist < 7 ? Math.max(0.16, (dist / 7) * baseSpeed) : baseSpeed;
    let vx = (dx / dist) * speed;
    let vy = (dy / dist) * speed;

    // Soft avoidance around seated humans (P=0.967) so pets never clip through cushions/knees
    const avoidRadius = pet.role === 'cat' ? 14.5 : 11.5;
    for (const slot of ['supervisor', 'implementer', 'reviewer'] as const) {
      const h = world.humans[slot];
      if (h.posturePhase !== 'seated' && h.posturePhase !== 'seating') continue;
      const rx = pet.body.position.x - h.body.position.x;
      const ry = pet.body.position.y - h.body.position.y;
      const rd = Math.hypot(rx, ry);
      if (rd > 0.1 && rd < avoidRadius) {
        const push = ((avoidRadius - rd) / avoidRadius) * 0.28;
        vx += (rx / rd) * push;
        vy += (ry / rd) * push;
      }
    }

    Matter.Body.setVelocity(pet.body, { x: vx, y: vy });

    if (Math.abs(vx) > 0.06) {
      pet.facingLeft = vx < 0;
    }
    pet.facingBack = updatePetFacingBack(pet.facingBack, vy);

    // Foot-driven gait: advance gait cycle strictly in proportion to real distance traveled!
    // Stride wavelength = ~7.5 logical units per full 4-beat/2-beat cycle -> feet NEVER slip!
    const strideLength = pet.behavior === 'trotting' || pet.behavior === 'chase_ball' ? 8.2 : 6.8;
    const ds = Math.hypot(vx, vy) * (dtMs / 16.67);
    pet.gaitCycle = (pet.gaitCycle + ds / strideLength) % 1;
  }
}

function computeQuadrupedGait(pet: PetActorSim): QuadrupedGaitState {
  const speed = Math.hypot(pet.body.velocity.x, pet.body.velocity.y);
  // Strict threshold: if pet is not genuinely moving, zero gait motion!
  const moving =
    (pet.behavior === 'walking' || pet.behavior === 'trotting' || pet.behavior === 'chase_ball') &&
    speed >= 0.12;
  const gaitType: 'walk' | 'trot' =
    pet.behavior === 'trotting' || pet.behavior === 'chase_ball' ? 'trot' : 'walk';

  if (!moving) {
    return {
      cycle: pet.gaitCycle,
      gaitType,
      frontSwing: 0,
      hindSwing: 0,
      frontLift: 0,
      hindLift: 0,
      bodyLift: 0,
      stretchAmt: 0,
      tuckAmt: 0,
    };
  }

  const phi = pet.gaitCycle * Math.PI * 2;
  const phaseLag = gaitType === 'trot' ? Math.PI : Math.PI * 0.65;
  const frontSwing = Math.sin(phi);
  const hindSwing = Math.sin(phi + phaseLag);
  const frontLift = Math.max(0, Math.cos(phi));
  const hindLift = Math.max(0, Math.cos(phi + phaseLag));
  // Cute, crisp pixel-art trotting hop driven strictly by displacement (never wobbles when stopped)
  const bodyLift = Math.abs(Math.sin(phi)) * (gaitType === 'trot' ? 0.85 : 0.55);

  return {
    cycle: pet.gaitCycle,
    gaitType,
    frontSwing,
    hindSwing,
    frontLift,
    hindLift,
    bodyLift,
    stretchAmt: 0,
    tuckAmt: 0,
  };
}

/**
 * Single authoritative physics step per room (called ONLY from the animation loop, never from React render).
 * Uses Matter.js fixed-step clamping (max 45ms) so tab-switches never explode velocities or warp characters.
 */
export function stepRoomPhysics(intent: RoomPhysicsIntent, now: number): RoomPhysicsOutput {
  let world = roomWorlds.get(intent.roomId);
  if (!world) {
    world = createRoomWorld(intent, now);
  } else if (world.activeSeason !== intent.season) {
    syncWorldSeasonObstacles(world, intent.season);
  }

  // Keep pet native posture synced if user cycled room cast (always dynamic so every room's pets stay lively)
  if (world.pets.dog.nativePosture !== intent.pets.dog.nativePosture) {
    world.pets.dog.nativePosture = intent.pets.dog.nativePosture;
    world.pets.dog.behavior = 'curious';
    world.pets.dog.waypoints = [];
    Matter.Body.setStatic(world.pets.dog.body, false);
  }
  if (world.pets.cat.nativePosture !== intent.pets.cat.nativePosture) {
    world.pets.cat.nativePosture = intent.pets.cat.nativePosture;
    world.pets.cat.behavior = 'curious';
    world.pets.cat.waypoints = [];
    Matter.Body.setStatic(world.pets.cat.body, false);
  }

  const rawDt = now - world.lastUpdateMs;
  if (rawDt > 2) {
    const dtMs = Math.min(48, Math.max(8, rawDt));
    world.lastUpdateMs = now;

    stepHuman(world.humans.supervisor, intent.humans.supervisor, intent, dtMs, now, world.seed);
    stepHuman(world.humans.implementer, intent.humans.implementer, intent, dtMs, now, world.seed);
    stepHuman(world.humans.reviewer, intent.humans.reviewer, intent, dtMs, now, world.seed);

    stepPet(world.pets.dog, world, intent, dtMs, now);
    stepPet(world.pets.cat, world, intent, dtMs, now);

    // Sub-step Matter.js at <= 16.667ms for textbook numerical stability and zero warnings
    const subSteps = Math.max(1, Math.min(3, Math.ceil(dtMs / 16.667)));
    const subDt = dtMs / subSteps;
    for (let s = 0; s < subSteps; s++) {
      Matter.Engine.update(world.engine, subDt);
    }
  }

  return snapshotRoomPhysics(world, intent);
}

/**
 * Pure read-only snapshot of a room's physics state (safe to call from React render without mutating anything!).
 */
export function peekRoomPhysics(intent: RoomPhysicsIntent, now: number): RoomPhysicsOutput {
  const world = roomWorlds.get(intent.roomId) || createRoomWorld(intent, now);
  return snapshotRoomPhysics(world, intent);
}

function snapshotRoomPhysics(world: RoomPhysicsWorld, intent: RoomPhysicsIntent): RoomPhysicsOutput {
  const toHumanSnap = (h: HumanActorSim): PhysicsActorSnapshot => {
    const vx = h.body.velocity.x;
    const vy = h.body.velocity.y;
    const speed = Math.hypot(vx, vy);
    const walking = h.posturePhase === 'walking';
    let pose = walking ? 'walk' : h.targetPose;
    if (!walking && h.idleActivity) {
      pose = h.idleActivity === 'coffee' ? 'mug' : h.idleActivity === 'board_glance' ? 'write' : 'stand';
    }
    return {
      x: h.body.position.x,
      y: h.body.position.y,
      vx,
      vy,
      speed,
      pose,
      posturePhase: h.posturePhase,
      seatBlend: h.seatBlend,
      facing: h.facing,
      useBack: h.useBack,
      walking,
      idleActivity: h.idleActivity,
    };
  };

  const toPetSnap = (p: PetActorSim): PhysicsPetSnapshot => ({
    x: p.body.position.x,
    y: p.body.position.y,
    vx: p.body.velocity.x,
    vy: p.body.velocity.y,
    speed: Math.hypot(p.body.velocity.x, p.body.velocity.y),
    facingLeft: p.facingLeft,
    facingBack: p.facingBack,
    behavior: p.behavior,
    nativePosture: p.nativePosture,
    gait: computeQuadrupedGait(p),
    interactionHeart: p.interactionHeart,
    pettingTargetSlot: p.pettingTargetSlot,
    actionLabel: p.actionLabel,
  });

  const dogSnap = toPetSnap(world.pets.dog);
  const catSnap = toPetSnap(world.pets.cat);

  const humans = {
    supervisor: toHumanSnap(world.humans.supervisor),
    implementer: toHumanSnap(world.humans.implementer),
    reviewer: toHumanSnap(world.humans.reviewer),
  };

  // Apply gentle petting pose if a pet is actively requesting pets from a stationary idle human
  for (const petSnap of [dogSnap, catSnap]) {
    if (petSnap.pettingTargetSlot && !humans[petSnap.pettingTargetSlot].walking && !intent.humans[petSnap.pettingTargetSlot].isWorking) {
      humans[petSnap.pettingTargetSlot].pose = 'petting';
    }
  }

  return {
    humans,
    pets: {
      dog: dogSnap,
      cat: catSnap,
    },
    toyBall: {
      x: world.toyBall.position.x,
      y: world.toyBall.position.y,
    },
  };
}

export function resetRoomPhysics(roomId?: string) {
  if (roomId) {
    roomWorlds.delete(roomId);
  } else {
    roomWorlds.clear();
  }
}
