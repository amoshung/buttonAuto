import test from 'node:test';
import assert from 'node:assert/strict';
import {
  findNavPath,
  getPetNativePosture,
  resetRoomPhysics,
  stepRoomPhysics,
  type RoomPhysicsIntent,
} from './roomPhysics';
import {
  computeSolarLight,
  pickWindowVista,
  setRoomVistaOverride,
} from './roomAtmosphere';
import { SPOT } from './pixelRoomScene';

function makeIntent(overrides?: Partial<RoomPhysicsIntent>): RoomPhysicsIntent {
  return {
    roomId: 'test-physics-room',
    season: 'winter',
    isNight: false,
    isRainOrSnow: false,
    isRoomIdle: true,
    humans: {
      supervisor: { standbySpot: 'coffeeA', targetSpot: 'coffeeA', targetPose: 'mug', isWorking: false, hasError: false },
      implementer: { standbySpot: 'sofaA', targetSpot: 'sofaA', targetPose: 'sofa', isWorking: false, hasError: false },
      reviewer: { standbySpot: 'sofaB', targetSpot: 'sofaB', targetPose: 'sofa', isWorking: false, hasError: false },
    },
    pets: {
      dog: { charId: 'd01_formosan', nativePosture: getPetNativePosture('d01_formosan'), trait: 'herding_dash' },
      cat: { charId: 'c04_black_cat', nativePosture: getPetNativePosture('c04_black_cat'), trait: 'cat_pounce' },
    },
    spots: SPOT,
    ...overrides,
  };
}

test('all opened-room pets including shiba and orange/black cats wake, stretch and actively roam', () => {
  resetRoomPhysics('test-all-pets-active');
  const intent = makeIntent({
    roomId: 'test-all-pets-active',
    pets: {
      dog: { charId: 'd02_shiba_red', nativePosture: getPetNativePosture('d02_shiba_red'), trait: 'shiba_drill' },
      cat: { charId: 'c02_orange', nativePosture: getPetNativePosture('c02_orange'), trait: 'cat_pounce' },
    },
  });
  assert.equal(intent.pets.cat.nativePosture, 'sit');
  assert.equal(intent.pets.dog.nativePosture, 'sit');

  let t = 10000;
  const first = stepRoomPhysics(intent, t);
  const startCatX = first.pets.cat.x;
  const startCatY = first.pets.cat.y;
  const startDogX = first.pets.dog.x;
  const startDogY = first.pets.dog.y;
  let maxCatDist = 0;
  let maxDogDist = 0;

  // Step 30 seconds of simulated time (750 frames @ 40ms)
  for (let i = 0; i < 750; i++) {
    t += 40;
    const out = stepRoomPhysics(intent, t);
    maxCatDist = Math.max(maxCatDist, Math.hypot(out.pets.cat.x - startCatX, out.pets.cat.y - startCatY));
    maxDogDist = Math.max(maxDogDist, Math.hypot(out.pets.dog.x - startDogX, out.pets.dog.y - startDogY));
  }
  assert.ok(maxCatDist > 6, `orange cat should actively roam the room, moved ${maxCatDist}`);
  assert.ok(maxDogDist > 6, `red shiba should actively roam the room, moved ${maxDogDist}`);
});

test('standing cat transitions loaf -> waking -> stretching before walking, with zero velocity while loafing', () => {
  resetRoomPhysics('test-stand-cat');
  const intent = makeIntent({
    roomId: 'test-stand-cat',
    pets: {
      dog: { charId: 'd01_formosan', nativePosture: 'stand', trait: 'herding_dash' },
      cat: { charId: 'c01_tabby_tw', nativePosture: 'stand', trait: 'cat_pounce' },
    },
  });

  let t = 20000;
  const seenStates = new Set<string>();
  for (let i = 0; i < 600; i++) {
    t += 40;
    const out = stepRoomPhysics(intent, t);
    const cat = out.pets.cat;
    seenStates.add(cat.behavior);
    if (cat.behavior === 'loaf' || cat.behavior === 'waking' || cat.behavior === 'stretching') {
      assert.ok(cat.speed < 0.001, `cat must not slide during ${cat.behavior}, got speed=${cat.speed}`);
    }
  }
  assert.ok(seenStates.has('loaf'), 'standing cat should rest in loaf');
  assert.ok(seenStates.has('waking'), 'standing cat should wake up before walking');
  assert.ok(seenStates.has('stretching'), 'standing cat should stretch after waking');
});

test('backend task start smoothly transitions implementer from cushion to desk seat via A* path without teleporting', () => {
  resetRoomPhysics('test-backend-cmd');
  const idleIntent = makeIntent({ roomId: 'test-backend-cmd' });
  let t = 30000;
  const idleOut = stepRoomPhysics(idleIntent, t);
  assert.equal(idleOut.humans.implementer.posturePhase, 'seated');

  // Backend sends task start (running / build) -> target implSeat
  const workIntent = makeIntent({
    roomId: 'test-backend-cmd',
    isRoomIdle: false,
    humans: {
      ...idleIntent.humans,
      implementer: {
        standbySpot: 'sofaA',
        targetSpot: 'implSeat',
        targetPose: 'type',
        isWorking: true,
        hasError: false,
      },
    },
  });

  const seenPhases = new Set<string>();
  let prevX = idleOut.humans.implementer.x;
  let prevY = idleOut.humans.implementer.y;

  for (let i = 0; i < 220; i++) {
    t += 35;
    const out = stepRoomPhysics(workIntent, t);
    const h = out.humans.implementer;
    seenPhases.add(h.posturePhase);
    const stepJump = Math.hypot(h.x - prevX, h.y - prevY);
    assert.ok(stepJump < 4.5, `character must not teleport on backend command (jump=${stepJump})`);
    prevX = h.x;
    prevY = h.y;
  }

  assert.ok(seenPhases.has('standing_up'), 'should stand up smoothly before walking');
  assert.ok(seenPhases.has('walking'), 'should walk along A* path');
  assert.ok(seenPhases.has('seating') || seenPhases.has('seated'), 'should arrive and sit at desk');
  const finalPos = stepRoomPhysics(workIntent, t + 35).humans.implementer;
  assert.ok(Math.hypot(finalPos.x - SPOT.implSeat.x, finalPos.y - SPOT.implSeat.y) < 3.0);
});

test('findNavPath avoids central irori hearth obstacle', () => {
  const path = findNavPath({ x: 118, y: 89 }, { x: 158, y: 89 });
  assert.ok(path.length >= 2);
  for (const pt of path) {
    const insideHearth = pt.x > 130 && pt.x < 145 && pt.y > 83 && pt.y < 95;
    assert.equal(insideHearth, false, `nav waypoint (${pt.x},${pt.y}) must not pass through hearth`);
  }
});

test('SunCalc solar lighting & selectable room window vista overrides work accurately', () => {
  const now = Date.now();
  const noon = computeSolarLight(now, 'noon');
  const night = computeSolarLight(now, 'night');
  const sunset = computeSolarLight(now, 'sunset');

  assert.equal(noon.isNight, false);
  assert.ok(noon.daylight > 0.7);
  assert.equal(night.isNight, true);
  assert.ok(sunset.golden > 0.3);

  setRoomVistaOverride('room-skyline-test', 'skyline');
  const vista = pickWindowVista(
    { id: 'spring_sakura', season: 'spring', weather: 'sakura', skyTop: '', skyBottom: '', foliagePrimary: '', foliageSecondary: '' },
    'room-skyline-test'
  );
  assert.equal(vista, 'skyline');
  setRoomVistaOverride('room-skyline-test', 'auto');
});
