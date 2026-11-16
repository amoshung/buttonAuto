import {
  DESK_SLOTS,
  LOUNGE_SPOTS,
  findGridPath,
  getDeskSlotById,
} from './office-map';
import type {
  ActivityLogEntry,
  AgentWorker,
  AssistanceCommand,
  AvatarConfig,
  ToolCategory,
  WatcherEventPayload,
  WorkerStatus,
} from './types';

const AVATAR_PRESETS: AvatarConfig[] = [
  {
    hairStyle: 'spiky',
    hairColor: '#2c1e1a',
    shirtColor: '#3b82f6',
    skinTone: '#f3c6a5',
    accessory: 'headphones',
  },
  {
    hairStyle: 'bob',
    hairColor: '#5b3222',
    shirtColor: '#10b981',
    skinTone: '#f7d3ba',
    accessory: 'glasses',
  },
  {
    hairStyle: 'cap',
    hairColor: '#1f2937',
    shirtColor: '#f59e0b',
    skinTone: '#e4b08b',
    accessory: 'none',
  },
  {
    hairStyle: 'bun',
    hairColor: '#1a1a2e',
    shirtColor: '#ec4899',
    skinTone: '#f5ccb0',
    accessory: 'glasses',
  },
  {
    hairStyle: 'short',
    hairColor: '#7c4a2d',
    shirtColor: '#8b5cf6',
    skinTone: '#ecc09f',
    accessory: 'headphones',
  },
  {
    hairStyle: 'bob',
    hairColor: '#d97706',
    shirtColor: '#06b6d4',
    skinTone: '#f9d8c2',
    accessory: 'none',
  },
  {
    hairStyle: 'short',
    hairColor: '#374151',
    shirtColor: '#ef4444',
    skinTone: '#dfb08c',
    accessory: 'glasses',
  },
  {
    hairStyle: 'spiky',
    hairColor: '#4c1d95',
    shirtColor: '#14b8a6',
    skinTone: '#f3c6a5',
    accessory: 'none',
  },
];

export function createSeedWorkers(nowIso = new Date().toISOString()): AgentWorker[] {
  const desk1 = getDeskSlotById('desk-1');
  const desk2 = getDeskSlotById('desk-2');
  const desk3 = getDeskSlotById('desk-3');
  const desk5 = getDeskSlotById('desk-5');
  const desk6 = getDeskSlotById('desk-6');
  const desk7 = getDeskSlotById('desk-7');
  const coffee1 = LOUNGE_SPOTS[0];

  return [
    {
      id: 'cursor-ide',
      name: '小碼 (Cursor)',
      role: '主架構工程師',
      appName: 'Cursor IDE',
      windowTitle: 'src/engine/renderer.ts — workspace',
      category: 'ide',
      status: 'working',
      activity: '正在編輯 Canvas 2D 渲染管線與骨架資料結構',
      deskId: desk1.id,
      avatar: AVATAR_PRESETS[0],
      lastHeartbeat: nowIso,
      source: 'demo',
      logs: [
        {
          id: 'log-1',
          timestamp: nowIso,
          type: 'info',
          message: '偵測到視窗焦點切換至 renderer.ts，開始高頻編碼',
        },
      ],
      pos: { ...desk1.seatTile },
      path: [],
      facing: 'up',
      behaviorState: 'at_desk',
      celebrateUntil: 0,
    },
    {
      id: 'vscode-api',
      name: '艾達 (VS Code)',
      role: '後端 API 開發',
      appName: 'Visual Studio Code',
      windowTitle: 'api/checkout/route.ts — build failed',
      category: 'ide',
      status: 'needs_help',
      activity: '等待確認 TypeScript 型別修正方案',
      helpPrompt:
        '偵測到 tsc 編譯中斷：OrderPayload 缺少 currency 欄位。需要您決定是否自動補齊預設值或改為手動檢查。',
      quickActions: ['套用預設 TWD 補丁', '開啟檔案手動修復', '略過型別檢查繼續'],
      deskId: desk2.id,
      avatar: AVATAR_PRESETS[1],
      lastHeartbeat: nowIso,
      source: 'demo',
      logs: [
        {
          id: 'log-2a',
          timestamp: nowIso,
          type: 'info',
          message: '執行 npm run typecheck',
        },
        {
          id: 'log-2b',
          timestamp: nowIso,
          type: 'help_request',
          message: '發現 OrderPayload 型別缺漏，發出協助請求',
        },
      ],
      pos: { ...desk2.seatTile },
      path: [],
      facing: 'up',
      behaviorState: 'at_desk',
      celebrateUntil: 0,
    },
    {
      id: 'chrome-dev',
      name: '小布 (Chrome)',
      role: '前端預覽與除錯',
      appName: 'Google Chrome',
      windowTitle: 'localhost:3000/monitor — DevTools Network',
      category: 'browser',
      status: 'working',
      activity: '正在監看即時串流封包與 DOM 渲染效能',
      deskId: desk5.id,
      avatar: AVATAR_PRESETS[2],
      lastHeartbeat: nowIso,
      source: 'demo',
      logs: [
        {
          id: 'log-3',
          timestamp: nowIso,
          type: 'info',
          message: '頁面載入完成 (LCP 410ms)，持續監聽 SSE 事件',
        },
      ],
      pos: { ...desk5.seatTile },
      path: [],
      facing: 'up',
      behaviorState: 'at_desk',
      celebrateUntil: 0,
    },
    {
      id: 'chrome-qa',
      name: '小測 (Playwright)',
      role: '自動化驗證瀏覽器',
      appName: 'Chromium QA',
      windowTitle: 'OAuth 授權確認頁面 — auth.example.com',
      category: 'browser',
      status: 'needs_help',
      activity: '自動化流程停在人機驗證 / 登入確認視窗',
      helpPrompt:
        '瀏覽器視窗停留在第三方授權確認畫面，自動化腳本無法代按同意。請在實機視窗完成登入後點擊「已完成，繼續執行」。',
      quickActions: ['我已完成授權，繼續', '重新整理該分頁', '暫停自動化腳本'],
      deskId: desk7.id,
      avatar: AVATAR_PRESETS[3],
      lastHeartbeat: nowIso,
      source: 'demo',
      logs: [
        {
          id: 'log-4',
          timestamp: nowIso,
          type: 'help_request',
          message: '偵測到非預期授權彈窗，暫停等待使用者介入',
        },
      ],
      pos: { ...desk7.seatTile },
      path: [],
      facing: 'up',
      behaviorState: 'at_desk',
      celebrateUntil: 0,
    },
    {
      id: 'iterm-build',
      name: '阿端 (Terminal)',
      role: 'CI / 容器建置工具',
      appName: 'iTerm2 · zsh',
      windowTitle: 'docker compose logs -f worker',
      category: 'terminal',
      status: 'working',
      activity: '正在執行單元測試與熱重載監聽 (14/14 passed)',
      deskId: desk3.id,
      avatar: AVATAR_PRESETS[4],
      lastHeartbeat: nowIso,
      source: 'demo',
      logs: [
        {
          id: 'log-5',
          timestamp: nowIso,
          type: 'info',
          message: '測試套件全數通過，持續監聽檔案變更',
        },
      ],
      pos: { ...desk3.seatTile },
      path: [],
      facing: 'up',
      behaviorState: 'at_desk',
      celebrateUntil: 0,
    },
    {
      id: 'figma-ui',
      name: '菲比 (Figma)',
      role: '介面與設計系統',
      appName: 'Figma Desktop',
      windowTitle: 'PixelDesk Design Tokens.fig',
      category: 'design',
      status: 'idle',
      activity: '視窗閒置超過 5 分鐘，正在茶水間喝咖啡休息',
      deskId: desk6.id,
      avatar: AVATAR_PRESETS[5],
      lastHeartbeat: nowIso,
      source: 'demo',
      logs: [
        {
          id: 'log-6',
          timestamp: nowIso,
          type: 'status_change',
          message: '偵測到無操作超過 300 秒，進入待機休息狀態',
        },
      ],
      pos: { ...coffee1.tile },
      path: [],
      facing: coffee1.facing,
      behaviorState: 'at_coffee',
      celebrateUntil: 0,
      targetSpotId: coffee1.id,
    },
  ];
}

export function inferCategory(appName: string, explicit?: ToolCategory): ToolCategory {
  if (explicit) return explicit;
  const lower = appName.toLowerCase();
  if (/(code|cursor|intellij|webstorm|vim|neovim|xcode|android studio|zed)/.test(lower)) {
    return 'ide';
  }
  if (/(chrome|safari|firefox|edge|arc|browser|playwright|puppeteer)/.test(lower)) {
    return 'browser';
  }
  if (/(term|terminal|iterm|zsh|bash|powershell|warp|docker|node|python)/.test(lower)) {
    return 'terminal';
  }
  if (/(figma|sketch|photoshop|blender|illustrator)/.test(lower)) {
    return 'design';
  }
  return 'other';
}

export function allocateDeskId(workers: AgentWorker[], category: ToolCategory): string {
  const occupied = new Set(workers.map((w) => w.deskId));
  const preferredFree = DESK_SLOTS.find(
    (d) => !occupied.has(d.id) && d.preferredCategory === category
  );
  if (preferredFree) return preferredFree.id;
  const anyFree = DESK_SLOTS.find((d) => !occupied.has(d.id));
  if (anyFree) return anyFree.id;
  // Fallback round-robin if >8 workers
  return DESK_SLOTS[workers.length % DESK_SLOTS.length].id;
}

export function pickLoungeSpot(workers: AgentWorker[], workerId: string) {
  const taken = new Set(
    workers
      .filter((w) => w.id !== workerId && w.targetSpotId)
      .map((w) => w.targetSpotId!)
  );
  const free = LOUNGE_SPOTS.find((s) => !taken.has(s.id));
  if (free) return free;
  let hash = 0;
  for (let i = 0; i < workerId.length; i++) {
    hash = (hash * 31 + workerId.charCodeAt(i)) >>> 0;
  }
  return LOUNGE_SPOTS[hash % LOUNGE_SPOTS.length];
}

export function routeWorkerForStatus(
  worker: AgentWorker,
  allWorkers: AgentWorker[]
): AgentWorker {
  const desk = getDeskSlotById(worker.deskId);
  if (worker.status === 'idle') {
    const spot =
      LOUNGE_SPOTS.find((s) => s.id === worker.targetSpotId) ??
      pickLoungeSpot(allWorkers, worker.id);
    const dist =
      Math.abs(worker.pos.x - spot.tile.x) + Math.abs(worker.pos.y - spot.tile.y);
    if (dist < 0.15) {
      return {
        ...worker,
        pos: { ...spot.tile },
        path: [],
        facing: spot.facing,
        behaviorState: spot.kind === 'coffee' ? 'at_coffee' : 'at_lounge',
        targetSpotId: spot.id,
      };
    }
    const path = findGridPath(worker.pos, spot.tile);
    return {
      ...worker,
      path,
      behaviorState: 'walking',
      targetSpotId: spot.id,
    };
  }

  // working, needs_help, paused, error -> head to desk seat
  const dist =
    Math.abs(worker.pos.x - desk.seatTile.x) +
    Math.abs(worker.pos.y - desk.seatTile.y);
  if (dist < 0.15) {
    return {
      ...worker,
      pos: { ...desk.seatTile },
      path: [],
      facing: 'up',
      behaviorState: 'at_desk',
      targetSpotId: undefined,
    };
  }
  const path = findGridPath(worker.pos, desk.seatTile);
  return {
    ...worker,
    path,
    behaviorState: 'walking',
    targetSpotId: undefined,
  };
}

function appendLog(
  logs: ActivityLogEntry[],
  entry: Omit<ActivityLogEntry, 'id'>
): ActivityLogEntry[] {
  const next = [
    {
      ...entry,
      id: `log-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    },
    ...logs,
  ];
  return next.slice(0, 25);
}

export function applyWatcherEvent(
  workers: AgentWorker[],
  payload: WatcherEventPayload,
  nowIso = new Date().toISOString()
): { workers: AgentWorker[]; updatedWorker: AgentWorker; isNew: boolean } {
  const normalizedId =
    payload.id?.trim() ||
    payload.appName
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') ||
    `worker-${workers.length + 1}`;

  const existingIdx = workers.findIndex((w) => w.id === normalizedId);
  const category = inferCategory(payload.appName, payload.category);

  if (existingIdx >= 0) {
    const prev = workers[existingIdx];
    const statusChanged = prev.status !== payload.status;
    const activityChanged =
      prev.activity !== payload.activity || prev.windowTitle !== payload.windowTitle;

    let logs = prev.logs;
    if (statusChanged || activityChanged) {
      logs = appendLog(logs, {
        timestamp: nowIso,
        type:
          payload.status === 'needs_help'
            ? 'help_request'
            : payload.status === 'error'
              ? 'error'
              : statusChanged
                ? 'status_change'
                : 'info',
        message:
          payload.status === 'needs_help' && payload.helpPrompt
            ? payload.helpPrompt
            : `${payload.appName}: ${payload.activity}`,
      });
    }

    const updatedBase: AgentWorker = {
      ...prev,
      name: payload.name?.trim() || prev.name,
      role: payload.role?.trim() || prev.role,
      appName: payload.appName,
      windowTitle: payload.windowTitle,
      category,
      status: payload.status,
      activity: payload.activity,
      helpPrompt:
        payload.status === 'needs_help' || payload.status === 'error'
          ? payload.helpPrompt || prev.helpPrompt || '此視窗回報需要人工確認或協助。'
          : undefined,
      quickActions:
        payload.status === 'needs_help' || payload.status === 'error'
          ? payload.quickActions ?? ['確認繼續執行', '重新檢查狀態', '暫停監看']
          : undefined,
      lastHeartbeat: nowIso,
      source: 'watcher',
      logs,
    };

    const routed = routeWorkerForStatus(updatedBase, workers);
    const nextWorkers = [...workers];
    nextWorkers[existingIdx] = routed;
    return { workers: nextWorkers, updatedWorker: routed, isNew: false };
  }

  // Create new worker
  const deskId = allocateDeskId(workers, category);
  const desk = getDeskSlotById(deskId);
  const avatar = AVATAR_PRESETS[workers.length % AVATAR_PRESETS.length];

  const createdBase: AgentWorker = {
    id: normalizedId,
    name: payload.name?.trim() || `${payload.appName}`,
    role:
      payload.role?.trim() ||
      (category === 'ide'
        ? '程式碼開發視窗'
        : category === 'browser'
          ? '瀏覽器監看目標'
          : category === 'terminal'
            ? '終端機與背景服務'
            : '桌面工具視窗'),
    appName: payload.appName,
    windowTitle: payload.windowTitle,
    category,
    status: payload.status,
    activity: payload.activity,
    helpPrompt:
      payload.status === 'needs_help' || payload.status === 'error'
        ? payload.helpPrompt || '此視窗回報需要人工確認或協助。'
        : undefined,
    quickActions:
      payload.status === 'needs_help' || payload.status === 'error'
        ? payload.quickActions ?? ['確認繼續執行', '重新檢查狀態', '暫停監看']
        : undefined,
    deskId,
    avatar,
    lastHeartbeat: nowIso,
    source: 'watcher',
    logs: [
      {
        id: `log-${Date.now()}`,
        timestamp: nowIso,
        type: payload.status === 'needs_help' ? 'help_request' : 'info',
        message: `新視窗接入辦公室：${payload.windowTitle}`,
      },
    ],
    pos: { ...desk.seatTile },
    path: [],
    facing: 'up',
    behaviorState: 'at_desk',
    celebrateUntil: 0,
  };

  const routed = routeWorkerForStatus(createdBase, workers);
  return {
    workers: [...workers, routed],
    updatedWorker: routed,
    isNew: true,
  };
}

export function resolveWorkerAssistance(
  workers: AgentWorker[],
  commands: AssistanceCommand[],
  workerId: string,
  actionLabel: string,
  message: string,
  nowMs = Date.now()
): {
  workers: AgentWorker[];
  commands: AssistanceCommand[];
  resolvedWorker?: AgentWorker;
  command?: AssistanceCommand;
} {
  const idx = workers.findIndex((w) => w.id === workerId);
  if (idx < 0) {
    return { workers, commands };
  }

  const nowIso = new Date(nowMs).toISOString();
  const prev = workers[idx];
  const nextStatus: WorkerStatus =
    actionLabel.includes('暫停') || actionLabel.toLowerCase().includes('pause')
      ? 'paused'
      : 'working';

  const summaryMsg = message.trim()
    ? `使用者介入「${actionLabel}」：${message.trim()}`
    : `使用者已核准協助：「${actionLabel}」`;

  const command: AssistanceCommand = {
    id: `cmd-${nowMs}-${Math.random().toString(36).slice(2, 6)}`,
    workerId: prev.id,
    workerName: prev.name,
    appName: prev.appName,
    actionLabel,
    message: message.trim(),
    createdAt: nowIso,
    status: 'pending',
  };

  const updatedBase: AgentWorker = {
    ...prev,
    status: nextStatus,
    activity:
      nextStatus === 'paused'
        ? '已依指示暫停作業'
        : `已收到支援（${actionLabel}），恢復執行中`,
    helpPrompt: undefined,
    quickActions: undefined,
    lastHeartbeat: nowIso,
    celebrateUntil: nowMs + 3500,
    logs: appendLog(prev.logs, {
      timestamp: nowIso,
      type: 'assisted',
      message: summaryMsg,
    }),
  };

  const routed = routeWorkerForStatus(updatedBase, workers);
  const nextWorkers = [...workers];
  nextWorkers[idx] = routed;

  return {
    workers: nextWorkers,
    commands: [command, ...commands].slice(0, 50),
    resolvedWorker: routed,
    command,
  };
}

export function advanceSimulationTick(
  workers: AgentWorker[],
  dtSeconds: number
): AgentWorker[] {
  const clampedDt = Math.min(Math.max(dtSeconds, 0), 0.1);
  const walkSpeedTilesPerSec = 3.2;

  return workers.map((w) => {
    if (w.path.length === 0) return w;

    let remainingMove = walkSpeedTilesPerSec * clampedDt;
    let currPos = { ...w.pos };
    let facing = w.facing;
    const nextPath = [...w.path];

    while (remainingMove > 0 && nextPath.length > 0) {
      const target = nextPath[0];
      const dx = target.x - currPos.x;
      const dy = target.y - currPos.y;
      const dist = Math.hypot(dx, dy);

      if (Math.abs(dx) > Math.abs(dy)) {
        facing = dx > 0 ? 'right' : 'left';
      } else if (Math.abs(dy) > 0.001) {
        facing = dy > 0 ? 'down' : 'up';
      }

      if (dist <= remainingMove) {
        currPos = { ...target };
        nextPath.shift();
        remainingMove -= dist;
      } else {
        currPos = {
          x: currPos.x + (dx / dist) * remainingMove,
          y: currPos.y + (dy / dist) * remainingMove,
        };
        remainingMove = 0;
      }
    }

    let behaviorState = w.behaviorState;
    if (nextPath.length === 0) {
      if (w.status === 'idle') {
        const spot = LOUNGE_SPOTS.find((s) => s.id === w.targetSpotId);
        facing = spot?.facing ?? 'up';
        behaviorState = spot?.kind === 'coffee' ? 'at_coffee' : 'at_lounge';
      } else {
        facing = 'up';
        behaviorState = 'at_desk';
      }
    }

    return {
      ...w,
      pos: currPos,
      path: nextPath,
      facing,
      behaviorState,
    };
  });
}
