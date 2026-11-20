import test from 'node:test';
import assert from 'node:assert/strict';
import { DESK_SLOTS, LOUNGE_SPOTS, findGridPath, isWalkable } from './office-map';
import {
  advanceSimulationTick,
  applyWatcherEvent,
  createSeedWorkers,
  resolveWorkerAssistance,
} from './office-sim';
import {
  buildIsoWalkPath,
  buildRoomScene,
  CHARACTER_ROSTER,
  COMPANIONS,
  cycleRoomCast,
  findActiveCriterionIndex,
  getPersonaRecentRecords,
  LH,
  LW,
  computeSeatedMicroOffset,
  getForegroundFurnitureLayers,
  pickRoomCast,
  pickRoomTheme,
  PIXEL_SCALE,
  registerVisibleRooms,
  resolveSeasonForDate,
  ROOM_THEMES,
  sortEventsDesc,
  SPOT,
} from './pixelRoomScene';
import {
  findNavPath,
  getObstaclesForSeason,
  getSeasonalPetSpots,
  updatePetFacingBack,
} from './roomPhysics';

test('all desk seats and lounge spots sit on walkable floor tiles', () => {
  for (const desk of DESK_SLOTS) {
    assert.equal(
      isWalkable(desk.seatTile.x, desk.seatTile.y),
      true,
      `Desk ${desk.id} seat must be walkable`
    );
    assert.equal(
      isWalkable(desk.deskTile.x, desk.deskTile.y),
      false,
      `Desk ${desk.id} table must block walking`
    );
  }
  for (const spot of LOUNGE_SPOTS) {
    assert.equal(
      isWalkable(spot.tile.x, spot.tile.y),
      true,
      `Lounge spot ${spot.id} must be walkable`
    );
  }
});

test('findGridPath routes around desks without crossing obstacles', () => {
  const desk1Seat = DESK_SLOTS[0].seatTile;
  const coffeeSpot = LOUNGE_SPOTS[0].tile;
  const path = findGridPath(desk1Seat, coffeeSpot);

  assert.ok(path.length > 5, 'Should produce multi-step path across office');
  for (const step of path) {
    assert.equal(isWalkable(step.x, step.y), true, `Step (${step.x},${step.y}) must be walkable`);
  }
  const last = path[path.length - 1];
  assert.deepEqual(last, coffeeSpot);
});

test('applyWatcherEvent updates existing worker and routes idle worker to lounge', () => {
  const seed = createSeedWorkers('2026-11-15T10:00:00.000Z');
  const { workers, updatedWorker, isNew } = applyWatcherEvent(
    seed,
    {
      id: 'cursor-ide',
      appName: 'Cursor IDE',
      windowTitle: 'Idle — no edits',
      status: 'idle',
      activity: '暫時無操作，前往茶水間休息',
    },
    '2026-11-15T10:05:00.000Z'
  );

  assert.equal(isNew, false);
  assert.equal(updatedWorker.status, 'idle');
  assert.equal(updatedWorker.source, 'watcher');
  assert.equal(updatedWorker.behaviorState, 'walking');
  assert.ok(updatedWorker.path.length > 0, 'Idle worker should receive a path to lounge/coffee');
  assert.equal(workers.length, seed.length);
});

test('applyWatcherEvent allocates a free desk for newly discovered external window', () => {
  const seed = createSeedWorkers('2026-11-15T10:00:00.000Z');
  const { workers, updatedWorker, isNew } = applyWatcherEvent(seed, {
    appName: 'Postman',
    windowTitle: 'POST /api/orders — Collection Runner',
    category: 'other',
    status: 'needs_help',
    activity: 'API 回傳 401 Unauthorized',
    helpPrompt: '請提供新的 Bearer Token 或重新登入測試帳號。',
  });

  assert.equal(isNew, true);
  assert.equal(workers.length, seed.length + 1);
  assert.equal(updatedWorker.status, 'needs_help');
  assert.ok(updatedWorker.deskId, 'New worker should be assigned a desk');
});

test('resolveWorkerAssistance clears help state, enqueues command, and starts celebration', () => {
  const seed = createSeedWorkers('2026-11-15T10:00:00.000Z');
  const target = seed.find((w) => w.status === 'needs_help')!;
  assert.ok(target);

  const result = resolveWorkerAssistance(
    seed,
    [],
    target.id,
    '套用預設 TWD 補丁',
    '已確認補上 currency: TWD',
    1_700_000_000_000
  );

  assert.ok(result.resolvedWorker);
  assert.equal(result.resolvedWorker.status, 'working');
  assert.equal(result.resolvedWorker.helpPrompt, undefined);
  assert.equal(result.resolvedWorker.celebrateUntil, 1_700_000_003_500);
  assert.equal(result.commands.length, 1);
  assert.equal(result.commands[0].workerId, target.id);
  assert.equal(result.commands[0].status, 'pending');
});

test('advanceSimulationTick moves walking worker smoothly to destination', () => {
  const seed = createSeedWorkers();
  const { workers } = applyWatcherEvent(seed, {
    id: 'cursor-ide',
    appName: 'Cursor IDE',
    windowTitle: 'Paused',
    status: 'idle',
    activity: '休息中',
  });

  let current = workers;
  for (let i = 0; i < 120; i++) {
    current = advanceSimulationTick(current, 0.1);
  }
  const cursor = current.find((w) => w.id === 'cursor-ide')!;
  assert.equal(cursor.path.length, 0);
  assert.ok(
    cursor.behaviorState === 'at_coffee' || cursor.behaviorState === 'at_lounge',
    'Worker should arrive at coffee or lounge spot'
  );
});

test('sortEventsDesc sorts events newest first and deduplicates by id', () => {
  const sorted = sortEventsDesc([
    { id: 10, ts: '2026-11-18T10:37:00', kind: 'info', text: 'oldest' },
    { id: 25, ts: '2026-11-18T12:50:00', kind: 'info', text: 'newest' },
    { id: 15, ts: '2026-11-18T11:05:00', kind: 'info', text: 'middle' },
    { id: 25, ts: '2026-11-18T12:50:00', kind: 'info', text: 'duplicate newest' },
  ]);
  assert.deepEqual(
    sorted.map((e) => e.id),
    [25, 15, 10]
  );
});

test('getPersonaRecentRecords returns up to 5 latest records in DESC order and buildRoomScene keeps idle actors in standby lounge', () => {
  assert.equal(PIXEL_SCALE, 4);
  const fakeRoom = {
    id: 'r1',
    name: 'demo',
    repo: 'd:/repo',
    goal: 'build wiki',
    phase: 'build' as const,
    status: 'paused' as const,
    mode: 'auto' as const,
    activity: '實作 U2：精編 telegram_dataflow 百科',
    progress: { done: 1, total: 3 },
    criteria: [
      { id: 'U1', text: '建立基礎架構', done: true, status: 'verified' },
      { id: 'U2', text: '精編 telegram_dataflow 百科', done: false, status: 'pending' },
      { id: 'U3', text: '向量索引與匯出', done: false, status: 'pending' },
    ],
    events: [
      { id: 1, ts: '2026-11-18T10:00:00', kind: 'info', text: '實作 round 1 開始' },
      { id: 2, ts: '2026-11-18T10:05:00', kind: 'info', text: '修改檔案 a.py' },
      { id: 3, ts: '2026-11-18T10:10:00', kind: 'info', text: '自動允許：pytest -q' },
      { id: 4, ts: '2026-11-18T10:15:00', kind: 'info', text: '修改檔案 b.py' },
      { id: 5, ts: '2026-11-18T10:20:00', kind: 'info', text: '實作 round 2 開始' },
      { id: 6, ts: '2026-11-18T10:25:00', kind: 'info', text: '修改檔案 c.py' },
    ],
  };

  const records = getPersonaRecentRecords(fakeRoom as any, fakeRoom.events as any, 'implementer', 5);
  assert.equal(records.length, 5);
  assert.ok(records[0].text.includes('修改檔案 c.py'));

  const activeIdx = findActiveCriterionIndex(fakeRoom.criteria, fakeRoom as any, fakeRoom.events as any);
  assert.equal(activeIdx, 1, 'Should match U2 as the active criterion');

  const scene = buildRoomScene(fakeRoom as any, []);
  assert.equal(scene.activeCriterionIndex, 1);
  // Paused room -> all 3 personas should not be working and stay in standby lounge coordinates
  for (const p of scene.personas) {
    assert.equal(p.isWorking, false);
    assert.ok(['sofa', 'mug', 'petting'].includes(p.pose));
    assert.ok(p.character && p.character.id, 'Each persona must be assigned a character from CHARACTER_ROSTER');
  }
});

test('CHARACTER_ROSTER has 48 characters (24 Taiwanese & Kominka humans + 24 natural dog/cat breeds) and physics-compliant pathfinding', () => {
  assert.equal(LW, 240);
  assert.equal(LH, 135);
  assert.equal(CHARACTER_ROSTER.length, 48);
  const humans = CHARACTER_ROSTER.filter((c) => c.species === 'human');
  const twHumans = humans.filter((c) => c.sheet === 'humansTw');
  const animals = CHARACTER_ROSTER.filter((c) => c.species === 'animal');
  const dogs = animals.filter((c) => c.petSubspecies === 'dog' && c.sheet === 'dogs');
  const cats = animals.filter((c) => c.petSubspecies === 'cat' && c.sheet === 'cats');

  assert.equal(humans.length, 24, 'Should have 24 human working characters');
  assert.equal(twHumans.length, 12, 'Should have 12 Taiwanese cultural human characters');
  assert.ok(animals.length >= 20, `Expected at least 20 dog/cat mascots, got ${animals.length}`);
  assert.equal(dogs.length, 12, 'Should have 12 natural dog breeds');
  assert.equal(cats.length, 12, 'Should have 12 natural cat breeds');
  for (const pet of animals) {
    assert.equal(pet.accessory, 'none', `Pet ${pet.id} must be non-anthropomorphic with no clothes/accessories`);
    assert.ok(pet.breedTrait, `Pet ${pet.id} must have a breed-specific behavior trait`);
  }
  assert.equal(COMPANIONS.length, 2);

  // Verify multi-waypoint physics pathfinding between sofaA (123, 85) and coffeeA (152, 85) routes around the burning hearth (129..147, 82..97)
  const hearthAvoidPath = buildIsoWalkPath(SPOT.sofaA.x, SPOT.sofaA.y, SPOT.coffeeA.x, SPOT.coffeeA.y);
  assert.ok(hearthAvoidPath.length >= 3, 'Should add corridor waypoint to avoid walking through the burning hearth');
  for (const wp of hearthAvoidPath) {
    const insideHearth = wp.x > 129 && wp.x < 147 && wp.y > 82 && wp.y < 97;
    assert.equal(insideHearth, false, `Waypoint (${wp.x}, ${wp.y}) must not step into the sunken hearth`);
  }

  for (const [key, pt] of Object.entries(SPOT)) {
    assert.ok(pt.x >= 40 && pt.x <= LW - 20, `Spot ${key}.x (${pt.x}) out of isometric bounds`);
    assert.ok(pt.y >= 40 && pt.y <= LH - 10, `Spot ${key}.y (${pt.y}) out of isometric bounds`);
  }

  // Verify 16 room themes (>= 15 required) covering seasons, rain, thunder, snow, and indoor decor
  assert.ok(ROOM_THEMES.length >= 15, `Expected at least 15 room themes, got ${ROOM_THEMES.length}`);
  const seasons = new Set(ROOM_THEMES.map((t) => t.season));
  assert.deepEqual(seasons, new Set(['spring', 'summer', 'autumn', 'winter']));
  const weathers = new Set(ROOM_THEMES.map((t) => t.weather));
  assert.ok(weathers.has('rain') && weathers.has('thunder') && weathers.has('snow'));

  // Register 8 visible rooms and verify:
  // 1) All working roles (supervisor, implementer, reviewer) are HUMANS (24 unique humans across 8 rooms!)
  // 2) All mascots (companions) are natural DOGS & CATS and have smaller sizeScale than humans
  // 3) Zero duplicate characters appear within a room OR across all 8 visible rooms on screen (40 unique characters!)
  const visibleIds = ['r-1', 'r-2', 'r-3', 'r-4', 'r-5', 'r-6', 'r-7', 'r-8'];
  registerVisibleRooms(visibleIds);
  const screenCharIds = new Set<string>();
  const screenThemeIds = new Set<string>();

  for (const rid of visibleIds) {
    const cast = pickRoomCast(rid);
    const theme = pickRoomTheme(rid);
    screenThemeIds.add(theme.id);

    assert.equal(cast.supervisor.species, 'human', 'Supervisor must be human');
    assert.equal(cast.implementer.species, 'human', 'Implementer must be human');
    assert.equal(cast.reviewer.species, 'human', 'Reviewer must be human');
    assert.equal(cast.companions[0].species, 'animal', 'Companion 0 must be dog mascot');
    assert.equal(cast.companions[0].petSubspecies, 'dog', 'Companion 0 must be a dog breed');
    assert.equal(cast.companions[1].species, 'animal', 'Companion 1 must be cat mascot');
    assert.equal(cast.companions[1].petSubspecies, 'cat', 'Companion 1 must be a cat breed');

    assert.ok(cast.companions[0].sizeScale < cast.supervisor.sizeScale, 'Dog must be smaller than human');
    assert.ok(cast.companions[1].sizeScale < cast.implementer.sizeScale, 'Cat must be smaller than human');

    const roomIds = [
      cast.supervisor.id,
      cast.implementer.id,
      cast.reviewer.id,
      cast.companions[0].id,
      cast.companions[1].id,
    ];
    assert.equal(new Set(roomIds).size, 5, `Room ${rid} must have 5 unique characters`);
    for (const cid of roomIds) {
      assert.equal(screenCharIds.has(cid), false, `Character ${cid} duplicated across visible rooms`);
      screenCharIds.add(cid);
    }
  }
  assert.equal(screenCharIds.size, 40, 'All 40 characters across 8 rooms must be 100% unique');
  assert.equal(screenThemeIds.size, 8, 'All 8 visible rooms must have distinct scenery themes');

  // Cycling room cast should rotate to a new character team and theme
  const castA = pickRoomCast('r-1');
  const sigA = `${castA.supervisor.id}:${castA.implementer.id}:${castA.reviewer.id}`;
  cycleRoomCast('r-1');
  const castA2 = pickRoomCast('r-1');
  const sigA2 = `${castA2.supervisor.id}:${castA2.implementer.id}:${castA2.reviewer.id}`;
  assert.notEqual(sigA, sigA2, 'cycleRoomCast should randomize characters for the room');
});

test('U1 & U2: seasonal obstacles, foreground furniture layers, and seated micro-animation', () => {
  const winterObs = getObstaclesForSeason('winter');
  const summerObs = getObstaclesForSeason('summer');
  assert.ok(winterObs.some((o) => o.id === 'winter_kotatsu'), 'Winter must include kotatsu collider');
  assert.ok(summerObs.some((o) => o.id === 'summer_fan'), 'Summer must include standing fan collider');
  assert.ok(!winterObs.some((o) => o.id === 'summer_fan'), 'Winter must not include summer fan');

  // Winter path across kotatsu center should detour around the kotatsu rectangle
  const path = findNavPath({ x: 96, y: 92 }, { x: 164, y: 92 }, 'winter');
  assert.ok(path.length >= 2, 'Detour path should have multiple waypoints');
  for (const pt of path) {
    const insideKotatsu = pt.x > 124 && pt.x < 146 && pt.y > 82 && pt.y < 93;
    assert.equal(insideKotatsu, false, `Waypoint (${pt.x},${pt.y}) clipped inside winter kotatsu`);
  }

  // Foreground layers
  const summerLayers = getForegroundFurnitureLayers('summer');
  assert.ok(summerLayers.some((l) => l.id === 'left_dual_monitor_desk'));
  assert.ok(summerLayers.some((l) => l.id === 'summer_standing_fan'));
  const winterLayers = getForegroundFurnitureLayers('winter');
  assert.ok(winterLayers.some((l) => l.id === 'winter_kotatsu'));
  assert.ok(!winterLayers.some((l) => l.id === 'summer_standing_fan'));

  // Seated human micro-animation stays strictly bounded
  const workOff = computeSeatedMicroOffset(true, 1234, 3);
  const idleOff = computeSeatedMicroOffset(false, 1234, 3);
  assert.ok(Math.abs(workOff.dx) <= 0.5 && Math.abs(workOff.dy) <= 0.5);
  assert.ok(Math.abs(idleOff.dx) <= 0.3 && Math.abs(idleOff.dy) <= 0.3);
});

test('U3 & U4: pet facing hysteresis and seasonal warmth/coolness spots', () => {
  let back = false;
  back = updatePetFacingBack(back, -0.08);
  assert.equal(back, false, 'vy=-0.08 should not flip to back');
  back = updatePetFacingBack(back, -0.18);
  assert.equal(back, true, 'vy=-0.18 should flip to back');
  back = updatePetFacingBack(back, -0.10);
  assert.equal(back, true, 'vy=-0.10 inside hysteresis band should stay back');
  back = updatePetFacingBack(back, -0.02);
  assert.equal(back, false, 'vy=-0.02 should flip back to front');

  const winCat = getSeasonalPetSpots('winter', 'cat');
  const sumDog = getSeasonalPetSpots('summer', 'dog');
  assert.ok(winCat.restLabel.includes('暖桌'));
  assert.ok(sumDog.restLabel.includes('電扇') || sumDog.restLabel.includes('納涼'));
});

test('U6: offline hemisphere season resolution', () => {
  assert.equal(resolveSeasonForDate(new Date(2026, 0, 15), 'north'), 'winter');
  assert.equal(resolveSeasonForDate(new Date(2026, 0, 15), 'south'), 'summer');
  assert.equal(resolveSeasonForDate(new Date(2026, 3, 15), 'north'), 'spring');
  assert.equal(resolveSeasonForDate(new Date(2026, 3, 15), 'south'), 'autumn');
  assert.equal(resolveSeasonForDate(new Date(2026, 6, 15), 'north'), 'summer');
  assert.equal(resolveSeasonForDate(new Date(2026, 6, 15), 'south'), 'winter');
  assert.equal(resolveSeasonForDate(new Date(2026, 9, 15), 'north'), 'autumn');
  assert.equal(resolveSeasonForDate(new Date(2026, 9, 15), 'south'), 'spring');
});

