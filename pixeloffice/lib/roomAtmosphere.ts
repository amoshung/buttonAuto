import * as SunCalc from 'suncalc';
import { createNoise2D } from 'simplex-noise';

export type TimeOfDayOverride = 'auto' | 'dawn' | 'noon' | 'sunset' | 'night';
export type WindowVistaKind = 'garden' | 'skyline' | 'harbor' | 'alpine';

export interface SolarLighting {
  /** Sun altitude in radians (-PI/2 .. +PI/2) */
  altitude: number;
  /** Sun azimuth in radians */
  azimuth: number;
  /** 0 = deep night, 1 = bright midday */
  daylight: number;
  /** 0..1 golden hour warmth */
  golden: number;
  isNight: boolean;
  phaseLabel: string;
  skyTop: string;
  skyMid: string;
  skyHorizon: string;
  sunbeamColor: string;
  sunbeamAlpha: number;
  /** Horizontal skew of sunbeams entering the left-wall window */
  beamSlopeX: number;
  /** Dynamic cast shadow offset for characters & pets */
  shadowDx: number;
  shadowDy: number;
  shadowAlpha: number;
  indoorOverlayColor: string;
  indoorOverlayAlpha: number;
}

export interface AtmosphereThemeInput {
  id: string;
  season: 'spring' | 'summer' | 'autumn' | 'winter';
  weather: string;
  skyTop: string;
  skyBottom: string;
  foliagePrimary: string;
  foliageSecondary: string;
}

const noise2D = createNoise2D(() => 0.42197);

// Window opening polygon matching iso-room-clean.png wooden beams, shoji frames, and dual monitor tops
export const WINDOW_OPENING_POLY: [number, number][] = [
  [66.0, 44.5],
  [99.0, 26.8],
  [99.0, 55.2],
  [84.5, 55.2],
  [72.5, 62.5],
  [72.5, 70.0],
  [66.0, 73.5],
];

// Global user-selectable time-of-day preview override ('auto' uses live wall-clock + SunCalc)
let globalTimeOverride: TimeOfDayOverride = 'auto';

// Per-room selectable window vista override ('auto' | 'garden' | 'skyline' | 'harbor' | 'alpine')
export type WindowVistaSelection = 'auto' | WindowVistaKind;
const roomVistaOverrides = new Map<string, WindowVistaSelection>();

export function setTimeOfDayOverride(mode: TimeOfDayOverride) {
  globalTimeOverride = mode;
}

export function getTimeOfDayOverride(): TimeOfDayOverride {
  return globalTimeOverride;
}

export function setRoomVistaOverride(roomId: string, vista: WindowVistaSelection) {
  if (vista === 'auto') roomVistaOverrides.delete(roomId);
  else roomVistaOverrides.set(roomId, vista);
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(`pixeloffice:vista:${roomId}`, vista);
    } catch {
      // ignore storage quota errors
    }
  }
}

export function getRoomVistaOverride(roomId: string): WindowVistaSelection {
  const cached = roomVistaOverrides.get(roomId);
  if (cached) return cached;
  if (typeof window !== 'undefined') {
    try {
      const saved = window.localStorage.getItem(`pixeloffice:vista:${roomId}`) as WindowVistaSelection | null;
      if (saved === 'garden' || saved === 'skyline' || saved === 'harbor' || saved === 'alpine') {
        roomVistaOverrides.set(roomId, saved);
        return saved;
      }
    } catch {
      // ignore
    }
  }
  return 'auto';
}

/**
 * Maps each room theme + roomId to a distinct window vista so different rooms look out onto
 * skyscrapers (Taipei skyline), harbor ocean, alpine sea-of-clouds, or traditional seasonal gardens!
 */
export function pickWindowVista(theme: AtmosphereThemeInput, roomId: string): WindowVistaKind {
  const override = getRoomVistaOverride(roomId);
  if (override !== 'auto') return override;
  if (theme.weather === 'seaside') return 'harbor';
  if (theme.weather === 'mist' || theme.weather === 'bamboo' || theme.weather === 'starry' || theme.weather === 'onsen') {
    return 'alpine';
  }
  if (theme.weather === 'sunset' || theme.weather === 'thunder' || theme.weather === 'rain') {
    return 'skyline';
  }
  // Deterministic variety across rooms so not every room only shows two trees
  let h = 0;
  for (let i = 0; i < roomId.length; i++) h = (h * 31 + roomId.charCodeAt(i)) >>> 0;
  const cycle: WindowVistaKind[] = ['garden', 'skyline', 'harbor', 'alpine'];
  return cycle[h % cycle.length];
}

function effectiveDateForSolar(now: number, override: TimeOfDayOverride = globalTimeOverride): Date {
  const d = new Date(now);
  if (override === 'auto') return d;
  const times = SunCalc.getTimes(d, 25.033, 121.565);
  if (override === 'dawn') return times.goldenHourEnd || new Date(d.setHours(6, 30, 0, 0));
  if (override === 'noon') return times.solarNoon || new Date(d.setHours(12, 0, 0, 0));
  if (override === 'sunset') return times.goldenHour || new Date(d.setHours(16, 50, 0, 0));
  return times.nadir || new Date(d.setHours(22, 30, 0, 0));
}

/**
 * Computes physically grounded solar angle, sky scattering colors, indoor sun-shaft geometry,
 * and dynamic cast-shadow vector using SunCalc (Taipei: 25.033°N, 121.565°E).
 */
export function computeSolarLight(now: number, override: TimeOfDayOverride = globalTimeOverride): SolarLighting {
  const date = effectiveDateForSolar(now, override);
  const pos = SunCalc.getPosition(date, 25.033, 121.565);
  // Installed suncalc returns altitude (-90..+90) and azimuth (0..360) in degrees
  const altDeg = pos.altitude;
  const azRad = (pos.azimuth * Math.PI) / 180;

  // Daylight curve: 0 below -6° (civil twilight), 1 above +30°
  const daylight = Math.max(0, Math.min(1, (altDeg + 6) / 36));
  // Golden hour peaks around -2° .. +14° altitude
  const golden = Math.max(0, 1 - Math.abs(altDeg - 6) / 14) * (altDeg > -6 ? 1 : 0);
  const isNight = altDeg < -2;

  let phaseLabel = '正午日光';
  let skyTop = '#3b82f6';
  let skyMid = '#7dd3fc';
  let skyHorizon = '#fef3c7';
  let sunbeamColor = '255, 246, 214';
  let indoorOverlayColor = '255, 248, 225';
  let indoorOverlayAlpha = 0.0;

  if (isNight) {
    phaseLabel = '靜謐星夜';
    skyTop = '#0b1026';
    skyMid = '#162044';
    skyHorizon = '#25335e';
    sunbeamColor = '148, 197, 255';
    indoorOverlayColor = '15, 23, 52';
    indoorOverlayAlpha = 0.22;
  } else if (golden > 0.35) {
    const isMorning = date.getHours() < 12;
    phaseLabel = isMorning ? '晨曦微光' : '茜色夕照';
    skyTop = isMorning ? '#4f6ea8' : '#3b2d5e';
    skyMid = isMorning ? '#f59e8b' : '#e06c4f';
    skyHorizon = isMorning ? '#fde68a' : '#fbbf24';
    sunbeamColor = isMorning ? '255, 220, 165' : '255, 168, 92';
    indoorOverlayColor = isMorning ? '255, 214, 150' : '245, 130, 68';
    indoorOverlayAlpha = 0.11 * golden;
  } else {
    phaseLabel = '晴朗日間';
    skyTop = '#2e86de';
    skyMid = '#74b9ff';
    skyHorizon = '#dff9fb';
    sunbeamColor = '255, 250, 224';
    indoorOverlayAlpha = 0.02;
  }

  // Sunbeam enters from the left-back window toward the right-front tatami; angle shifts with solar azimuth
  const azNorm = Math.max(-1, Math.min(1, Math.sin(azRad)));
  const beamSlopeX = 24 + azNorm * 14;
  const sunbeamAlpha = isNight ? 0.1 : 0.12 + daylight * 0.12 + golden * 0.08;

  // Dynamic cast shadow direction (opposite the window light source)
  const shadowLen = isNight ? 1.8 : Math.max(1.4, Math.min(4.8, 4.4 - daylight * 2.5));
  const shadowDx = shadowLen * (0.85 + azNorm * 0.25);
  const shadowDy = shadowLen * 0.42;
  const shadowAlpha = isNight ? 0.16 : 0.22 + daylight * 0.12;

  return {
    altitude: pos.altitude,
    azimuth: pos.azimuth,
    daylight,
    golden,
    isNight,
    phaseLabel,
    skyTop,
    skyMid,
    skyHorizon,
    sunbeamColor,
    sunbeamAlpha,
    beamSlopeX,
    shadowDx,
    shadowDy,
    shadowAlpha,
    indoorOverlayColor,
    indoorOverlayAlpha,
  };
}

function clipWindowOpening(ctx: CanvasRenderingContext2D) {
  ctx.beginPath();
  ctx.moveTo(WINDOW_OPENING_POLY[0][0], WINDOW_OPENING_POLY[0][1]);
  for (let i = 1; i < WINDOW_OPENING_POLY.length; i++) {
    ctx.lineTo(WINDOW_OPENING_POLY[i][0], WINDOW_OPENING_POLY[i][1]);
  }
  ctx.closePath();
  ctx.clip();
}

function drawSkyBackdrop(ctx: CanvasRenderingContext2D, solar: SolarLighting, theme: AtmosphereThemeInput, t: number) {
  if (typeof ctx.createLinearGradient !== 'function') return;
  const grad = ctx.createLinearGradient(82, 26, 82, 72);
  const isStorm = theme.weather === 'rain' || theme.weather === 'thunder' || theme.weather === 'blizzard';
  if (isStorm && !solar.isNight) {
    grad.addColorStop(0, '#334155');
    grad.addColorStop(0.55, '#475569');
    grad.addColorStop(1, '#64748b');
  } else {
    grad.addColorStop(0, solar.skyTop);
    grad.addColorStop(0.55, solar.skyMid);
    grad.addColorStop(1, solar.skyHorizon);
  }
  ctx.fillStyle = grad;
  ctx.fillRect(65, 25, 36, 50);

  // Stars & moon at night
  if (solar.isNight || theme.weather === 'starry') {
    for (let i = 0; i < 14; i++) {
      const sx = 67 + ((i * 7.3) % 30);
      const sy = 28 + ((i * 5.1) % 20);
      const tw = 0.35 + 0.65 * Math.abs(noise2D(i * 1.7, t / 1800));
      ctx.fillStyle = `rgba(254, 249, 195, ${tw.toFixed(2)})`;
      ctx.fillRect(sx, sy, 0.55, 0.55);
    }
    // Crescent moon
    ctx.fillStyle = '#fef9c3';
    ctx.beginPath();
    ctx.arc(91, 33, 2.2, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = solar.skyTop;
    ctx.beginPath();
    ctx.arc(92.1, 32.4, 2.0, 0, Math.PI * 2);
    ctx.fill();
  } else if (!isStorm) {
    // Drifting soft pixel clouds driven by time
    const cloudCol = solar.golden > 0.35 ? 'rgba(255, 224, 178, 0.65)' : 'rgba(255, 255, 255, 0.62)';
    ctx.fillStyle = cloudCol;
    for (let c = 0; c < 3; c++) {
      const cx = 64 + (((t / 650 + c * 15) % 44) - 4);
      const cy = 32 + c * 4.5;
      ctx.fillRect(cx, cy, 7.5, 1.8);
      ctx.fillRect(cx + 1.5, cy - 1.1, 4.5, 1.2);
    }
  }
}

function drawSkylineVista(ctx: CanvasRenderingContext2D, solar: SolarLighting, theme: AtmosphereThemeInput, t: number) {
  drawSkyBackdrop(ctx, solar, theme, t);

  // Far misty skyline silhouette
  const farCol = solar.isNight ? '#111827' : solar.golden > 0.35 ? '#5b425e' : '#64748b';
  ctx.fillStyle = farCol;
  const farTowers = [
    [66, 46, 5, 28],
    [72, 41, 4.5, 33],
    [77, 44, 5.5, 30],
    [84, 37, 4.2, 37], // Taipei 101 inspired tiered tower!
    [90, 43, 5, 31],
    [95, 48, 4.5, 26],
  ];
  for (const [bx, by, bw, bh] of farTowers) {
    ctx.fillRect(bx, by, bw, bh);
  }
  // Tiered spire on the tall tower
  ctx.fillRect(85.1, 33.5, 2.0, 3.8);
  ctx.fillRect(85.85, 30.8, 0.5, 3.0);

  // Near high-rise buildings with glass curtain walls
  const nearCol = solar.isNight ? '#0f172a' : solar.golden > 0.35 ? '#3b2c44' : '#334155';
  const glassTint = solar.isNight
    ? 'rgba(56, 189, 248, 0.14)'
    : solar.golden > 0.35
      ? 'rgba(251, 146, 60, 0.35)'
      : 'rgba(186, 230, 253, 0.32)';
  const nearTowers = [
    [67.5, 51, 6, 24],
    [75.5, 48, 6.5, 27],
    [87.5, 49, 6.2, 26],
    [94.2, 53, 5.2, 22],
  ];
  for (const [bx, by, bw, bh] of nearTowers) {
    ctx.fillStyle = nearCol;
    ctx.fillRect(bx, by, bw, bh);
    ctx.fillStyle = glassTint;
    ctx.fillRect(bx + 0.6, by + 0.8, bw - 1.2, bh - 1.2);

    // Lit office windows (glow brightly at dusk/night, subtle daytime glints)
    const litProb = solar.isNight ? 0.62 : solar.golden > 0.35 ? 0.35 : 0.12;
    for (let wy = by + 2; wy < 68; wy += 2.3) {
      for (let wx = bx + 1.1; wx < bx + bw - 1.2; wx += 1.6) {
        const n = (noise2D(wx * 0.7, wy * 0.7 + Math.floor(t / 9000) * 0.2) + 1) * 0.5;
        if (n < litProb) {
          ctx.fillStyle =
            n < litProb * 0.3
              ? 'rgba(125, 211, 252, 0.88)'
              : 'rgba(253, 224, 71, 0.88)';
          ctx.fillRect(wx, wy, 0.9, 1.1);
        }
      }
    }
  }

  // Blinking red aviation warning beacons on skyscraper rooftops
  const blink = Math.sin(t / 420) > 0.15;
  if (blink) {
    ctx.fillStyle = '#ef4444';
    ctx.fillRect(85.7, 30.4, 0.9, 0.9);
    ctx.fillRect(78.2, 47.2, 0.8, 0.8);
  }

  // Distant moving elevated highway traffic lights at night/sunset
  if (solar.isNight || solar.golden > 0.35) {
    for (let i = 0; i < 4; i++) {
      const tx = 66 + (((t / 90 + i * 11) % 34));
      ctx.fillStyle = i % 2 === 0 ? 'rgba(251, 113, 133, 0.78)' : 'rgba(254, 240, 138, 0.75)';
      ctx.fillRect(tx, 66.5 + (i % 2) * 1.1, 1.6, 0.55);
    }
  }
}

function drawHarborVista(ctx: CanvasRenderingContext2D, solar: SolarLighting, theme: AtmosphereThemeInput, t: number) {
  drawSkyBackdrop(ctx, solar, theme, t);

  // Distant island silhouette
  ctx.fillStyle = solar.isNight ? '#1e293b' : solar.golden > 0.35 ? '#6d4c6b' : '#4a7c7e';
  ctx.beginPath();
  ctx.moveTo(65, 53);
  ctx.lineTo(74, 48.5);
  ctx.lineTo(83, 51.5);
  ctx.lineTo(93, 47.5);
  ctx.lineTo(100, 52);
  ctx.lineTo(100, 55);
  ctx.lineTo(65, 55);
  ctx.closePath();
  ctx.fill();

  // Ocean plane
  const seaGrad = ctx.createLinearGradient(82, 52, 82, 74);
  if (solar.isNight) {
    seaGrad.addColorStop(0, '#0f172a');
    seaGrad.addColorStop(1, '#1e3a5f');
  } else if (solar.golden > 0.35) {
    seaGrad.addColorStop(0, '#b45309');
    seaGrad.addColorStop(1, '#1e3a5f');
  } else {
    seaGrad.addColorStop(0, '#0284c7');
    seaGrad.addColorStop(1, '#0369a1');
  }
  ctx.fillStyle = seaGrad;
  ctx.fillRect(65, 52, 36, 23);

  // Animated water specular sparkles driven by simplex-noise
  const glintCol = solar.isNight
    ? 'rgba(186, 230, 253, 0.55)'
    : solar.golden > 0.35
      ? 'rgba(253, 224, 71, 0.75)'
      : 'rgba(224, 242, 254, 0.68)';
  ctx.fillStyle = glintCol;
  for (let i = 0; i < 16; i++) {
    const wx = 67 + ((i * 6.7) % 30);
    const wy = 53.5 + ((i * 3.1) % 16);
    const n = noise2D(wx * 0.25 + t / 1400, wy * 0.4);
    if (n > 0.15) {
      ctx.fillRect(wx, wy, 1.8 + n * 1.6, 0.45);
    }
  }

  // Gently gliding sailboat
  const boatX = 68 + ((t / 700) % 28);
  const boatY = 57 + Math.sin(t / 450) * 0.35;
  ctx.fillStyle = '#f8fafc';
  ctx.beginPath();
  ctx.moveTo(boatX, boatY);
  ctx.lineTo(boatX + 1.8, boatY - 3.6);
  ctx.lineTo(boatX + 3.2, boatY);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = '#7c2d12';
  ctx.fillRect(boatX - 0.5, boatY, 4.2, 1.0);

  // Lighthouse beacon on distant headland at night
  if (solar.isNight) {
    const pulse = (Math.sin(t / 600) + 1) * 0.45;
    ctx.fillStyle = `rgba(254, 240, 138, ${pulse.toFixed(2)})`;
    ctx.beginPath();
    ctx.arc(92.5, 47.2, 1.5, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawAlpineVista(ctx: CanvasRenderingContext2D, solar: SolarLighting, theme: AtmosphereThemeInput, t: number) {
  drawSkyBackdrop(ctx, solar, theme, t);

  // Aurora ribbons on starry/winter nights
  if (solar.isNight && (theme.season === 'winter' || theme.weather === 'starry')) {
    for (let i = 0; i < 10; i++) {
      const ax = 67 + i * 3.2;
      const ay = 32 + noise2D(i * 0.3, t / 2400) * 4;
      ctx.fillStyle = i % 2 === 0 ? 'rgba(52, 211, 153, 0.28)' : 'rgba(56, 189, 248, 0.24)';
      ctx.fillRect(ax, ay, 2.6, 8);
    }
  }

  // Far snow-capped mountain range
  ctx.fillStyle = solar.isNight ? '#1e293b' : solar.golden > 0.35 ? '#7c4d6b' : '#64748b';
  ctx.beginPath();
  ctx.moveTo(65, 58);
  ctx.lineTo(75, 40);
  ctx.lineTo(84, 49);
  ctx.lineTo(93, 37);
  ctx.lineTo(100, 48);
  ctx.lineTo(100, 74);
  ctx.lineTo(65, 74);
  ctx.closePath();
  ctx.fill();

  // Snow caps
  ctx.fillStyle = solar.golden > 0.35 ? '#fde68a' : '#f1f5f9';
  ctx.beginPath();
  ctx.moveTo(72.5, 44.5);
  ctx.lineTo(75, 40);
  ctx.lineTo(77.5, 44.2);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(90.2, 41.8);
  ctx.lineTo(93, 37);
  ctx.lineTo(95.6, 41.5);
  ctx.closePath();
  ctx.fill();

  // Flowing sea of clouds (Simplex Noise)
  for (let i = 0; i < 8; i++) {
    const cx = 65 + (((t / 520 + i * 5.2) % 38) - 3);
    const cy = 53 + noise2D(i * 0.8, t / 3200) * 2.4 + (i % 3) * 2.2;
    ctx.fillStyle = solar.isNight
      ? 'rgba(148, 163, 184, 0.24)'
      : solar.golden > 0.35
        ? 'rgba(254, 215, 170, 0.55)'
        : 'rgba(248, 250, 252, 0.62)';
    ctx.fillRect(cx, cy, 9.5, 2.2);
  }

  // Foreground bamboo stalks when bamboo theme
  if (theme.weather === 'bamboo') {
    for (let b = 0; b < 5; b++) {
      const bx = 68 + b * 6.8;
      const sway = noise2D(b * 1.1, t / 1800) * 1.1;
      ctx.strokeStyle = solar.isNight ? '#14532d' : '#15803d';
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.moveTo(bx, 74);
      ctx.quadraticCurveTo(bx + sway * 0.6, 50, bx + sway, 26);
      ctx.stroke();
    }
  }
}

function drawWeatherParticles(
  ctx: CanvasRenderingContext2D,
  theme: AtmosphereThemeInput,
  solar: SolarLighting,
  roomId: string,
  t: number
) {
  let seed = 0;
  for (let i = 0; i < roomId.length; i++) seed = (seed * 31 + roomId.charCodeAt(i)) >>> 0;

  if (theme.weather === 'rain' || theme.weather === 'thunder') {
    ctx.strokeStyle = 'rgba(224, 242, 254, 0.68)';
    ctx.lineWidth = 0.32;
    for (let r = 0; r < 18; r++) {
      const wind = noise2D(r * 0.4, t / 1200) * 1.2;
      const prog = ((t / 22 + r * 9 + seed) % 34) / 34;
      const rx = 67 + ((r * 6.5) % 30) - prog * (4.2 + wind);
      const ry = 27 + prog * 40;
      ctx.beginPath();
      ctx.moveTo(rx, ry);
      ctx.lineTo(rx - 0.85, ry + 2.8);
      ctx.stroke();
    }
    if (theme.weather === 'thunder' && t % 4800 < 140) {
      ctx.fillStyle = 'rgba(254, 249, 195, 0.42)';
      ctx.fillRect(65, 25, 36, 50);
    }
    return;
  }

  if (theme.weather === 'snow' || theme.weather === 'blizzard' || theme.season === 'winter') {
    const speed = theme.weather === 'blizzard' ? 20 : 52;
    ctx.fillStyle = 'rgba(255, 255, 255, 0.88)';
    for (let s = 0; s < 16; s++) {
      const prog = ((t / speed + s * 8.3 + seed) % 32) / 32;
      const curl = noise2D(s * 0.7, t / 900) * 2.2;
      const sx = 68 + ((s * 5.7) % 28) + curl - prog * (theme.weather === 'blizzard' ? 7 : 1.8);
      const sy = 27 + prog * 39;
      ctx.beginPath();
      ctx.arc(sx, sy, 0.42, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }

  if (theme.weather === 'firefly' || (solar.isNight && theme.season === 'summer')) {
    for (let f = 0; f < 9; f++) {
      const fx = 69 + ((f * 5.3) % 26) + noise2D(f * 1.3, t / 1100) * 3.2;
      const fy = 33 + ((f * 6.1) % 26) + noise2D(f * 2.1, t / 1000) * 2.4;
      const glow = 0.25 + 0.65 * Math.abs(Math.sin(t / 280 + f * 1.7));
      ctx.fillStyle = `rgba(190, 242, 100, ${glow.toFixed(2)})`;
      ctx.beginPath();
      ctx.arc(fx, fy, 0.5, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }

  // Seasonal drifting petals / autumn leaves driven by simplex-noise wind field
  const cols =
    theme.weather === 'sakura' || theme.season === 'spring'
      ? ['#fbcfe8', '#f472b6', '#ffe4e6']
      : theme.weather === 'ginkgo'
        ? ['#fde047', '#facc15', '#fef08a']
        : theme.season === 'autumn'
          ? ['#f87171', '#fb923c', '#fbbf24']
          : ['#a7f3d0', '#fef08a', '#ffffff'];
  for (let i = 0; i < 10; i++) {
    const cycle = (t / 56 + i * 13 + (seed % 37)) % 36;
    const wind = noise2D(i * 0.9, t / 1300) * 2.4;
    const lx = 67 + ((i * 6.8 + cycle * 0.42) % 29) + wind;
    const ly = 28 + (cycle % 34);
    ctx.fillStyle = cols[(i + seed) % cols.length];
    ctx.beginPath();
    ctx.arc(lx, ly, 0.42, 0, Math.PI * 2);
    ctx.fill();
  }
}

/**
 * Renders the room's high-detail pixel-art window scenery (Taipei 101 skyline / Shonan harbor /
 * snowy alpine sea-of-clouds / spring sakura or native seasonal garden), physical weather particles,
 * and volumetric SunCalc light shafts entering the room.
 */
export function drawRichWindowAndSunShafts(
  ctx: CanvasRenderingContext2D,
  theme: AtmosphereThemeInput,
  roomId: string,
  solar: SolarLighting,
  t: number,
  vistaImg?: HTMLImageElement | null
) {
  if (typeof ctx.save !== 'function' || typeof ctx.beginPath !== 'function' || typeof ctx.clip !== 'function') return;

  const vista = pickWindowVista(theme, roomId);

  ctx.save();
  clipWindowOpening(ctx);

  const isRainy = theme.weather === 'rain' || theme.weather === 'thunder';
  const isOvercast = isRainy || theme.weather === 'blizzard' || theme.weather === 'mist';

  if (vistaImg && typeof ctx.drawImage === 'function') {
    // Draw the high-detail 16-bit pixel art vista edge-to-edge inside the window opening polygon!
    ctx.save();
    if ('filter' in ctx) {
      if (theme.weather === 'thunder') {
        ctx.filter = 'saturate(0.22) brightness(0.48) contrast(1.12)';
      } else if (theme.weather === 'rain') {
        ctx.filter = 'saturate(0.32) brightness(0.68) contrast(1.04)';
      } else if (theme.weather === 'blizzard') {
        ctx.filter = 'saturate(0.28) brightness(0.84) contrast(0.95)';
      } else if (theme.weather === 'mist') {
        ctx.filter = 'saturate(0.52) brightness(0.88) contrast(0.9)';
      }
    }
    ctx.drawImage(vistaImg, 65.5, 26.5, 34.0, 47.5);
    ctx.restore();

    // Heavy overcast raincloud ceiling gradient so sunny blue skies genuinely turn into moody overcast clouds!
    if (isOvercast && typeof ctx.createLinearGradient === 'function') {
      const cloudGrad = ctx.createLinearGradient(82, 25, 82, 72);
      if (theme.weather === 'thunder') {
        cloudGrad.addColorStop(0, 'rgba(20, 28, 48, 0.84)');
        cloudGrad.addColorStop(0.55, 'rgba(38, 50, 74, 0.62)');
        cloudGrad.addColorStop(1, 'rgba(51, 65, 85, 0.35)');
      } else if (theme.weather === 'rain') {
        cloudGrad.addColorStop(0, 'rgba(71, 85, 105, 0.78)');
        cloudGrad.addColorStop(0.55, 'rgba(100, 116, 139, 0.52)');
        cloudGrad.addColorStop(1, 'rgba(148, 163, 184, 0.25)');
      } else if (theme.weather === 'blizzard') {
        cloudGrad.addColorStop(0, 'rgba(148, 163, 184, 0.68)');
        cloudGrad.addColorStop(0.6, 'rgba(203, 213, 225, 0.48)');
        cloudGrad.addColorStop(1, 'rgba(226, 232, 240, 0.28)');
      } else {
        // mist
        cloudGrad.addColorStop(0, 'rgba(203, 213, 225, 0.45)');
        cloudGrad.addColorStop(0.5, 'rgba(226, 232, 240, 0.52)');
        cloudGrad.addColorStop(1, 'rgba(241, 245, 249, 0.32)');
      }
      ctx.fillStyle = cloudGrad;
      ctx.fillRect(65, 25, 36, 50);
    }

    // Apply physical solar time-of-day color grading (sunset golden glow / night cooling)
    if (solar.isNight && vista !== 'skyline') {
      ctx.save();
      ctx.globalCompositeOperation = 'multiply';
      ctx.fillStyle = 'rgba(28, 42, 86, 0.72)';
      ctx.fillRect(65, 25, 36, 50);
      ctx.restore();
    } else if (!isOvercast && solar.golden > 0.3) {
      ctx.save();
      ctx.globalCompositeOperation = 'soft-light';
      ctx.fillStyle = `rgba(255, 165, 78, ${(solar.golden * 0.55).toFixed(2)})`;
      ctx.fillRect(65, 25, 36, 50);
      ctx.restore();
    }
  } else if (vista === 'skyline') {
    drawSkylineVista(ctx, solar, theme, t);
  } else if (vista === 'harbor') {
    drawHarborVista(ctx, solar, theme, t);
  } else if (vista === 'alpine') {
    drawAlpineVista(ctx, solar, theme, t);
  } else {
    // 'garden': preserve crisp native seasonal trees, transform sky into overcast grey during rain/thunder/mist
    if (isOvercast && typeof ctx.createLinearGradient === 'function') {
      const cloudGrad = ctx.createLinearGradient(82, 25, 82, 72);
      cloudGrad.addColorStop(0, theme.weather === 'thunder' ? 'rgba(20, 28, 48, 0.85)' : 'rgba(71, 85, 105, 0.78)');
      cloudGrad.addColorStop(0.6, theme.weather === 'thunder' ? 'rgba(38, 50, 74, 0.62)' : 'rgba(100, 116, 139, 0.52)');
      cloudGrad.addColorStop(1, 'rgba(148, 163, 184, 0.28)');
      ctx.fillStyle = cloudGrad;
      ctx.fillRect(65, 25, 36, 50);
    } else if (solar.isNight) {
      ctx.save();
      ctx.globalCompositeOperation = 'multiply';
      ctx.fillStyle = 'rgba(24, 38, 78, 0.68)';
      ctx.fillRect(65, 25, 36, 50);
      ctx.restore();
    } else if (solar.golden > 0.3) {
      ctx.save();
      ctx.globalCompositeOperation = 'soft-light';
      ctx.fillStyle = `rgba(255, 170, 85, ${(solar.golden * 0.48).toFixed(2)})`;
      ctx.fillRect(65, 25, 36, 50);
      ctx.restore();
    }
  }

  drawWeatherParticles(ctx, theme, solar, roomId, t);
  ctx.restore();

  // === Volumetric Sun / Moon / Cool Overcast Window Light entering the room ===
  if (typeof ctx.createLinearGradient === 'function') {
    ctx.save();
    if (isOvercast) {
      // Rainy / overcast: NO harsh yellow sunbeam! Soft cool diffuse skylight + subtle rainy indoor mood
      const coolGrad = ctx.createLinearGradient(74, 46, 110, 96);
      coolGrad.addColorStop(0, theme.weather === 'thunder' ? 'rgba(120, 148, 188, 0.14)' : 'rgba(165, 190, 216, 0.18)');
      coolGrad.addColorStop(1, 'rgba(140, 170, 200, 0)');
      ctx.fillStyle = coolGrad;
      ctx.beginPath();
      ctx.moveTo(67, 45);
      ctx.lineTo(98, 28);
      ctx.lineTo(118, 86);
      ctx.lineTo(84, 100);
      ctx.closePath();
      ctx.fill();

      // Cool rainy-day atmospheric tint across the room (makes hearth & monitors pop!)
      ctx.fillStyle = theme.weather === 'thunder' ? 'rgba(18, 28, 52, 0.26)' : 'rgba(36, 56, 84, 0.15)';
      ctx.fillRect(0, 0, 240, 135);

      // Periodic thunder lightning illuminating the indoor floor
      if (theme.weather === 'thunder' && t % 4800 < 140) {
        ctx.fillStyle = 'rgba(224, 242, 254, 0.22)';
        ctx.fillRect(0, 0, 240, 135);
      }
    } else {
      const beamGrad = ctx.createLinearGradient(74, 46, 74 + solar.beamSlopeX + 26, 98);
      beamGrad.addColorStop(0, `rgba(${solar.sunbeamColor}, ${(solar.sunbeamAlpha * 0.95).toFixed(3)})`);
      beamGrad.addColorStop(0.55, `rgba(${solar.sunbeamColor}, ${(solar.sunbeamAlpha * 0.42).toFixed(3)})`);
      beamGrad.addColorStop(1, `rgba(${solar.sunbeamColor}, 0)`);

      ctx.fillStyle = beamGrad;
      ctx.beginPath();
      ctx.moveTo(67, 45);
      ctx.lineTo(98, 28);
      ctx.lineTo(98 + solar.beamSlopeX + 18, 86);
      ctx.lineTo(67 + solar.beamSlopeX + 12, 102);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  // Subtle indoor time-of-day atmospheric color grade
  if (solar.indoorOverlayAlpha > 0.005) {
    ctx.save();
    ctx.fillStyle = `rgba(${solar.indoorOverlayColor}, ${solar.indoorOverlayAlpha.toFixed(3)})`;
    ctx.fillRect(0, 0, 240, 135);
    ctx.restore();
  }
}

/**
 * Seasonal Centerpiece Physics at the central irori pit (137.5, 90.2):
 * - Summer: NO fire! Covers the hearth pit with a wooden low tea table (和室夏茶几) topped with
 *   a chilled watermelon slice & iced barley tea carafe + gentle cool-breeze shimmer.
 * - Spring: Gentle simmering hearth embers + small steaming iron tea kettle (茶釜).
 * - Autumn: Cozy crackling charcoal fire with golden embers.
 * - Winter: Roaring warm hearth fire with wide amber bounce glow across the tatami.
 */
export function drawDynamicHearthFire(
  ctx: CanvasRenderingContext2D,
  season: 'spring' | 'summer' | 'autumn' | 'winter',
  solar: SolarLighting,
  t: number
) {
  const hx = 137.5;
  const hy = 90.2;

  // === SUMMER: No fire! Transform hearth pit into a cool cedar summer tea table with watermelon & iced tea ===
  if (season === 'summer') {
    if (typeof ctx.save !== 'function' || typeof ctx.beginPath !== 'function') return;
    ctx.save();
    // Isometric cedar tabletop lid covering the fire pit cleanly
    ctx.fillStyle = '#8b5a34';
    ctx.strokeStyle = '#3b2416';
    ctx.lineWidth = 0.65;
    ctx.beginPath();
    ctx.moveTo(hx, hy - 5.2);
    ctx.lineTo(hx + 9.2, hy - 0.6);
    ctx.lineTo(hx, hy + 4.0);
    ctx.lineTo(hx - 9.2, hy - 0.6);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // Top wood highlight edge
    ctx.fillStyle = '#a46e42';
    ctx.beginPath();
    ctx.moveTo(hx, hy - 4.6);
    ctx.lineTo(hx + 8.0, hy - 0.6);
    ctx.lineTo(hx, hy + 3.2);
    ctx.lineTo(hx - 8.0, hy - 0.6);
    ctx.closePath();
    ctx.fill();

    // Chilled glass iced-tea pitcher on the left side of the table
    ctx.fillStyle = '#bae6fd';
    ctx.fillRect(hx - 4.0, hy - 3.6, 2.4, 3.0);
    ctx.fillStyle = '#d97706';
    ctx.fillRect(hx - 3.7, hy - 2.3, 1.8, 1.5);
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(hx - 3.5, hy - 3.3, 0.5, 1.8); // glass highlight

    // Ceramic plate with fresh red watermelon wedges on the right side
    ctx.fillStyle = '#e2e8f0';
    ctx.beginPath();
    ctx.ellipse(hx + 2.4, hy - 0.8, 3.0, 1.6, 0, 0, Math.PI * 2);
    ctx.fill();
    // Watermelon rind & red flesh
    ctx.fillStyle = '#15803d';
    ctx.beginPath();
    ctx.arc(hx + 2.4, hy - 1.1, 1.8, 0, Math.PI);
    ctx.fill();
    ctx.fillStyle = '#ef4444';
    ctx.beginPath();
    ctx.arc(hx + 2.4, hy - 1.2, 1.4, 0, Math.PI);
    ctx.fill();
    ctx.fillStyle = '#1e293b';
    ctx.fillRect(hx + 1.9, hy - 0.9, 0.35, 0.35);
    ctx.fillRect(hx + 2.7, hy - 0.8, 0.35, 0.35);

    // Subtle cool-air breeze shimmer over the iced tea
    const shimmer = (Math.sin(t / 420) + 1) * 0.25;
    ctx.fillStyle = `rgba(186, 230, 253, ${shimmer.toFixed(2)})`;
    ctx.beginPath();
    ctx.arc(hx - 2.8, hy - 4.3 - Math.sin(t / 500) * 0.4, 0.55, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    return;
  }

  const n1 = noise2D(t / 240, 0.3);
  const n2 = noise2D(t / 150, 2.7);
  const seasonScale = season === 'winter' ? 1.22 : season === 'spring' ? 0.72 : 1.0;
  const intensity = (1 + n1 * 0.16 + n2 * 0.08) * seasonScale;

  // 1. Warm bounce glow across surrounding tatami & hearth rim
  if (typeof ctx.createRadialGradient === 'function') {
    const baseRadius = (season === 'winter' ? 35 : season === 'spring' ? 20 : 27) + (solar.isNight ? 8 : 0);
    const radius = baseRadius * (1 + n1 * 0.1);
    const alphaBoost = (solar.isNight ? 0.14 : 0) + (season === 'winter' ? 0.08 : 0);
    const grad = ctx.createRadialGradient(hx, hy - 1.5, 1.0, hx, hy - 1.5, radius);
    grad.addColorStop(0, `rgba(255, 190, 75, ${(0.36 + alphaBoost).toFixed(2)})`);
    grad.addColorStop(0.45, `rgba(255, 130, 35, ${(0.15 + alphaBoost * 0.5).toFixed(2)})`);
    grad.addColorStop(1, 'rgba(255, 95, 15, 0)');
    ctx.save();
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(hx, hy - 1.5, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  // 2. Layered pixel flame tongues (outer crimson-orange -> mid amber -> white-yellow core)
  if (typeof ctx.beginPath === 'function') {
    ctx.save();
    const tongues = [
      { ox: -1.4, h: 3.8 * seasonScale, w: 1.5, phase: 1.1, col: 'rgba(239, 68, 68, 0.85)' },
      { ox: 1.3, h: 3.5 * seasonScale, w: 1.4, phase: 2.8, col: 'rgba(249, 115, 22, 0.88)' },
      { ox: 0.0, h: 5.0 * seasonScale, w: 1.9, phase: 0.2, col: 'rgba(251, 146, 60, 0.92)' },
      { ox: 0.0, h: 3.3 * seasonScale, w: 1.2, phase: 4.1, col: 'rgba(254, 240, 138, 0.94)' },
    ];
    for (const fl of tongues) {
      const sway = noise2D(t / 190 + fl.phase, fl.phase) * 0.95;
      const fh = fl.h * (0.85 + Math.abs(noise2D(t / 160, fl.phase * 2)) * 0.35);
      ctx.fillStyle = fl.col;
      ctx.beginPath();
      ctx.moveTo(hx + fl.ox - fl.w, hy + 0.4);
      ctx.quadraticCurveTo(hx + fl.ox - fl.w * 0.5, hy - fh * 0.55, hx + fl.ox + sway, hy - fh);
      ctx.quadraticCurveTo(hx + fl.ox + fl.w * 0.5, hy - fh * 0.55, hx + fl.ox + fl.w, hy + 0.4);
      ctx.closePath();
      ctx.fill();
    }

    // 3. Rising glowing embers (buoyancy + curl noise drift)
    const emberCount = season === 'winter' ? 8 : season === 'spring' ? 3 : 6;
    for (let e = 0; e < emberCount; e++) {
      const life = ((t / 950 + e / emberCount) % 1);
      const ex = hx + noise2D(e * 3.1, t / 700) * (1.8 + life * 2.2);
      const ey = hy - 2.2 - life * (8.5 * Math.min(1.2, seasonScale));
      const alpha = (1 - life) * 0.9;
      ctx.fillStyle = life < 0.4 ? `rgba(254, 240, 138, ${alpha.toFixed(2)})` : `rgba(251, 113, 133, ${alpha.toFixed(2)})`;
      ctx.fillRect(ex, ey, 0.5, 0.5);
    }
    ctx.restore();
  }
}

/**
 * Live Wall Clock over iso-room-clean.png clock ellipse at (164.74, 39.11):
 * - Cleanly repaints the dial face to hide the static baked-in hands
 * - Renders isometric-projected hour, minute, and smoothly sweeping red second hand!
 */
export function drawAccurateClock(ctx: CanvasRenderingContext2D, now: number) {
  const cx = 164.75;
  const cy = 39.1;
  const rx = 2.65;
  const ry = 3.45;

  if (typeof ctx.save !== 'function' || typeof ctx.ellipse !== 'function') return;

  const d = new Date(now);
  const sec = d.getSeconds() + d.getMilliseconds() / 1000;
  const min = d.getMinutes() + sec / 60;
  const hr = (d.getHours() % 12) + min / 60;

  const hAng = (hr / 12) * Math.PI * 2;
  const mAng = (min / 60) * Math.PI * 2;
  const sAng = (sec / 60) * Math.PI * 2;

  ctx.save();
  // Cover static painted hands with clean ivory dial matching iso-room-clean.png
  ctx.fillStyle = '#f3ece0';
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx, ry, -0.18, 0, Math.PI * 2);
  ctx.fill();

  // Crisp 12/3/6/9 dial ticks
  ctx.fillStyle = '#5c4636';
  for (let i = 0; i < 4; i++) {
    const a = (i * Math.PI) / 2;
    const tx = cx + Math.sin(a) * (rx * 0.8);
    const ty = cy - Math.cos(a) * (ry * 0.8);
    ctx.fillRect(tx - 0.25, ty - 0.25, 0.5, 0.5);
  }

  // Hour hand
  ctx.strokeStyle = '#241710';
  ctx.lineWidth = 0.65;
  ctx.lineCap = 'round';
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.sin(hAng) * (rx * 0.52), cy - Math.cos(hAng) * (ry * 0.52));
  ctx.stroke();

  // Minute hand
  ctx.strokeStyle = '#3b2416';
  ctx.lineWidth = 0.5;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.sin(mAng) * (rx * 0.78), cy - Math.cos(mAng) * (ry * 0.78));
  ctx.stroke();

  // Live red sweeping second hand
  ctx.strokeStyle = '#dc2626';
  ctx.lineWidth = 0.35;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.sin(sAng) * (rx * 0.84), cy - Math.cos(sAng) * (ry * 0.84));
  ctx.stroke();

  // Brass center pin
  ctx.fillStyle = '#241710';
  ctx.beginPath();
  ctx.arc(cx, cy, 0.4, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

/**
 * Tints the 4 tatami zabuton cushions smoothly with the selected room season/theme color
 * using Canvas 'color' blend mode so the crisp pixel-art shading of iso-room-clean.png is 100% preserved!
 */
export function drawSeasonalIndoorCushions(
  ctx: CanvasRenderingContext2D,
  cushionMain: string,
  indoorAmbient: string
) {
  if (typeof ctx.save !== 'function' || typeof ctx.beginPath !== 'function') return;
  const cushions: [number, number][] = [
    [123, 85],
    [123, 100],
    [152, 85],
    [152, 100],
  ];
  ctx.save();
  ctx.globalCompositeOperation = 'color';
  ctx.globalAlpha = 0.52;
  ctx.fillStyle = cushionMain;
  for (const [cx, cy] of cushions) {
    ctx.beginPath();
    ctx.ellipse(cx, cy, 7.2, 4.1, 0, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();

  if (indoorAmbient) {
    ctx.save();
    ctx.fillStyle = indoorAmbient;
    ctx.fillRect(0, 0, 240, 135);
    ctx.restore();
  }
}

/**
 * Rising curling steam wisps driven by Simplex Noise over the pour-over coffee carafe
 * (176.5, 62.0) and gooseneck kettle spout (184.5, 64.5) on the right counter.
 */
export function drawCoffeeSteam(ctx: CanvasRenderingContext2D, t: number) {
  if (typeof ctx.save !== 'function' || typeof ctx.beginPath !== 'function') return;

  const sources = [
    { x: 176.4, y: 61.6, count: 4, height: 6.2 }, // Pour-over coffee dripper
    { x: 183.6, y: 64.2, count: 3, height: 5.2 }, // Gooseneck kettle spout
  ];

  ctx.save();
  for (const src of sources) {
    for (let i = 0; i < src.count; i++) {
      const life = ((t / 1800 + i / src.count) % 1);
      const y = src.y - life * src.height;
      const curl = noise2D(i * 1.9 + src.x * 0.1, t / 900 + life * 1.5) * (0.6 + life * 1.1);
      const x = src.x + curl;
      const alpha = Math.sin(life * Math.PI) * 0.48;
      const r = 0.45 + life * 0.75;
      ctx.fillStyle = `rgba(255, 250, 240, ${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.restore();
}
