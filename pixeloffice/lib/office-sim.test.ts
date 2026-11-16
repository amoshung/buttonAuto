import test from 'node:test';
import assert from 'node:assert/strict';
import { DESK_SLOTS, LOUNGE_SPOTS, findGridPath, isWalkable } from './office-map';
import {
  advanceSimulationTick,
  applyWatcherEvent,
  createSeedWorkers,
  resolveWorkerAssistance,
} from './office-sim';

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
