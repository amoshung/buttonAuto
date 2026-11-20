// 和後端（server.py）溝通：讀取 /api/team/* 與 SSE /api/stream；寫入 POST 一律帶 X-Token 標頭。
import type {
  CePlanInfo,
  OfficeSnapshot,
  RoomProgressInfo,
  SessionMode,
  SshHostInfo,
  TeamEvent,
  TeamInboxItem,
  TeamModelsView,
  TeamProject,
  TeamRoom,
  TeamScanResult,
  TeamState,
  TeamUsageSummary,
  WikiView,
} from './types';

function token(): string {
  if (typeof document === 'undefined') return '';
  return document.querySelector('meta[name="aea-token"]')?.getAttribute('content') ?? '';
}

export interface ActionResult {
  ok: boolean;
  text: string;
  room?: TeamRoom;
  snapshot?: OfficeSnapshot;
  [key: string]: unknown;
}

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(path, { cache: 'no-store' });
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

async function postJson(path: string, body: unknown): Promise<ActionResult> {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Token': token() },
      body: JSON.stringify(body),
    });
    const data = (await res.json().catch(() => ({}))) as Partial<ActionResult>;
    return {
      ...data,
      ok: Boolean(data.ok),
      text: data.text ?? (res.ok ? '已送出' : `失敗（${res.status}）`),
    };
  } catch (e) {
    return { ok: false, text: `連不上後端服務：${String(e)}` };
  }
}

export const teamApi = {
  state: () => getJson<TeamState>('/api/team/state'),
  projects: () => getJson<{ projects: TeamProject[] }>('/api/team/projects'),
  scan: (path: string, sshHost = '') =>
    getJson<TeamScanResult>(
      `/api/team/scan?path=${encodeURIComponent(path)}${sshHost ? `&ssh_host=${encodeURIComponent(sshHost)}` : ''}`,
    ),
  browseDirs: (path = '', sshHost = '') =>
    getJson<{ path: string; parent: string; dirs: Array<{ name: string; path: string }>; drives: string[]; ssh_host?: string }>(
      `/api/team/dirs?path=${encodeURIComponent(path)}${sshHost ? `&ssh_host=${encodeURIComponent(sshHost)}` : ''}`,
    ),
  sshHosts: () => getJson<{ hosts: SshHostInfo[] }>('/api/team/ssh/hosts'),
  addSshHost: (target: string, label = '') =>
    postJson('/api/team/ssh/hosts', { action: 'add', target, label }),
  removeSshHost: (target: string) =>
    postJson('/api/team/ssh/hosts', { action: 'remove', target }),
  probeSsh: (sshHost: string, force = true, password = '', installKey = false, quickAuth = false) =>
    postJson('/api/team/ssh/probe', {
      ssh_host: sshHost,
      force,
      password: password || undefined,
      install_key: installKey,
      quick_auth: quickAuth,
    }),
  room: (id: string) =>
    getJson<{
      room: TeamRoom;
      events: TeamEvent[];
      inbox: TeamInboxItem[];
      ce?: CePlanInfo;
      wiki?: WikiView;
      progress?: RoomProgressInfo;
    }>(`/api/team/room?id=${encodeURIComponent(id)}`),
  roomProgress: (id: string) =>
    getJson<{ ok: boolean; progress: RoomProgressInfo }>(`/api/team/room/progress?id=${encodeURIComponent(id)}`),
  syncRoomProgress: (id: string, note = '') =>
    postJson('/api/team/rooms/progress', { id, note }),
  wiki: (roomId: string) =>
    getJson<{ wiki: WikiView; ce: CePlanInfo }>(`/api/team/wiki?room=${encodeURIComponent(roomId)}`),
  roomEvents: (id: string, ids: string[]) =>
    getJson<{ events: TeamEvent[] }>(
      `/api/team/room/events?id=${encodeURIComponent(id)}&ids=${encodeURIComponent(ids.join(','))}`,
    ),
  fleet: () => getJson<{ fleet: TeamState['fleet']; catalog: Record<string, unknown> }>('/api/team/fleet'),
  usage: () => getJson<TeamUsageSummary>('/api/team/usage'),
  models: (sshHost = '') =>
    getJson<TeamModelsView>(
      `/api/team/models${sshHost ? `?ssh_host=${encodeURIComponent(sshHost)}` : ''}`,
    ),
  prompts: (roomId = '') =>
    getJson<{ roles: Record<string, { label?: string; version?: string; sections?: Record<string, string> }> }>(
      `/api/team/prompts${roomId ? `?room_id=${encodeURIComponent(roomId)}` : ''}`,
    ),

  createRoom: (payload: {
    repo: string;
    goal: string;
    ssh_host?: string;
    reference_root?: string;
    target_subdir?: string;
    target_subdirs?: string[];
    allowed_paths?: string[];
    source?: string;
    autonomy?: string;
    impl_cli?: string;
    review_cli?: string;
    impl_model?: string;
    review_model?: string;
    supervisor_cli?: string;
    supervisor_model?: string;
    role_prefs?: Record<string, { seat: string; model: string }>;
    name?: string;
    task_mode?: 'standard' | 'triage';
    d1_fast_review?: boolean;
    use_worktree?: boolean;
    engine_version?: 'v1' | 'v2';
  }) => postJson('/api/team/rooms', { source: 'goal', impl_cli: 'auto', review_cli: 'auto', engine_version: 'v2', ...payload }),
  pauseRoom: (id: string) => postJson('/api/team/rooms/pause', { id }),
  resumeRoom: (id: string) => postJson('/api/team/rooms/resume', { id }),
  deleteRoom: (id: string, force = true) => postJson('/api/team/rooms/archive', { id, force }),
  setRoomGoal: (id: string, goal: string) => postJson('/api/team/rooms/goal', { id, goal }),
  setRoomFolders: (
    id: string,
    payload: { repo?: string; reference_root?: string; target_subdir?: string; target_subdirs?: string[] },
  ) => postJson('/api/team/rooms/folders', { id, ...payload }),
  syncRoomPlan: (id: string, plan_rel = '', apply_goal = false) =>
    postJson('/api/team/rooms/sync_plan', { id, plan_rel, apply_goal }),
  getRoomPlanContent: (id: string, plan_rel = '') =>
    postJson('/api/team/rooms/plan_content', { id, plan_rel }),
  clearRoomPlan: (id: string) =>
    postJson('/api/team/rooms/clear_plan', { id }),
  clearRoomWiki: (id: string) =>
    postJson('/api/team/rooms/clear_wiki', { id }),
  generateRoomPlan: (id: string, goal = '', seat = '', model = '') =>
    postJson('/api/team/rooms/generate_plan', { id, goal, seat, model }),
  setRoomD1FastReview: (id: string, enabled: boolean) =>
    postJson('/api/team/rooms/d1_fast_review', { id, enabled }),
  rerunRoomUnit: (id: string, unit_id: string) =>
    postJson('/api/team/rooms/unit/rerun', { id, unit_id }),
  waiveRoomUnit: (id: string, unit_id: string, reason = '') =>
    postJson('/api/team/rooms/unit/waive', { id, unit_id, reason }),
  editRoomUnit: (id: string, unit_id: string, payload: { check_cmd?: string; text?: string }) =>
    postJson('/api/team/rooms/unit/edit', { id, unit_id, ...payload }),
  brainstormRoom: (payload: {
    id: string;
    messages: Array<{ role: 'user' | 'assistant'; content: string }>;
    mode?: 'chat' | 'generate_plan';
    seat?: string;
    model?: string;
    goal_draft?: string;
  }) => postJson('/api/team/rooms/brainstorm', payload),
  saveBrainstormPlan: (payload: {
    id: string;
    filename: string;
    content: string;
    apply_goal?: boolean;
  }) => postJson('/api/team/rooms/brainstorm/save_plan', payload),
  resetRoom: (id: string) => postJson('/api/team/rooms/reset', { id }),
  handoffRoom: (id: string, goal = '', name = '') =>
    postJson('/api/team/rooms/handoff', { id, goal, name }),
  setRoomModel: (
    id: string,
    role: 'supervisor' | 'impl' | 'review' | 'po' | 'tech_lead' | 'scrum' | 'qa',
    seat: string,
    model = ''
  ) => postJson('/api/team/rooms/model', { id, role, seat, model }),
  setRoomDelegate: (id: string, on: boolean) =>
    postJson('/api/team/rooms/delegate', { id, on }),
  answerInbox: (itemId: string, answer: string, text = '') =>
    postJson('/api/team/answer', { item_id: itemId, answer, text }),
  sendRoom: (id: string, text: string) => postJson('/api/team/send', { id, text }),
  pauseAll: (paused: boolean) => postJson('/api/team/pause_all', { paused }),
  fleetProbe: (seat: string) => postJson('/api/team/fleet/probe', { seat }),
  fleetReset: (seat: string) => postJson('/api/team/fleet/reset', { seat }),
  saveModels: (payload: Record<string, unknown>) => postJson('/api/team/models', payload),
  detectModels: (seat = 'all') => postJson('/api/team/models/detect', { seat: seat || 'all' }),
  startDemo: (autonomy = 'balanced') => postJson('/api/team/demo', { autonomy }),
  preflight: () => getJson<Record<string, unknown>>('/api/team/preflight'),
  testPreflight: (payload: { base_url: string; key?: string; glm_model?: string }) =>
    postJson('/api/team/preflight/test', payload),
  savePreflight: (payload: {
    base_url: string;
    key?: string;
    decision1_key?: string;
    glm_model?: string;
    d1_model?: string;
    sovereign_base_url?: string;
    sovereign_key?: string;
    embedding_model?: string;
    embedding_dim?: number;
    rerank_model?: string;
    wiki_base_url?: string;
    wiki_key?: string;
  }) => postJson('/api/team/preflight/save', payload),
  getWeather: (params?: { lat?: number; lon?: number; city?: string; refresh?: boolean }) => {
    const qs = new URLSearchParams();
    if (params?.lat !== undefined) qs.set('lat', String(params.lat));
    if (params?.lon !== undefined) qs.set('lon', String(params.lon));
    if (params?.city) qs.set('city', params.city);
    if (params?.refresh) qs.set('refresh', '1');
    const q = qs.toString();
    return getJson<{
      ok: boolean;
      degraded: boolean;
      stale?: boolean;
      cached?: boolean;
      lat: number;
      lon: number;
      city: string;
      hemisphere: 'north' | 'south';
      condition: 'clear' | 'cloudy' | 'rain' | 'snow';
      weather_code: number | null;
      temperature_c: number | null;
      is_day: boolean | null;
      fetched_at: number | null;
      message?: string;
    }>(`/api/team/weather${q ? `?${q}` : ''}`);
  },
  getModelCatalog: () =>
    getJson<{
      ok: boolean;
      source_url: string;
      price_unit: string;
      last_scanned_at: number | null;
      degraded: boolean;
      error?: string;
      models: Array<{
        id: string;
        label: string;
        provider?: string;
        mode?: string;
        input_usd_per_1m: number | null;
        output_usd_per_1m: number | null;
        source?: string;
      }>;
    }>('/api/team/catalog'),
  scanModelCatalog: () => postJson('/api/team/catalog/scan', {}),
  budget: () =>
    getJson<{
      ok: boolean;
      daily_token_limit: number;
      today_tokens: number;
      exceeded: boolean;
      running_rooms: number;
      total_rooms: number;
    }>('/api/team/budget'),
  saveBudget: (daily_token_limit: number) => postJson('/api/team/budget', { daily_token_limit }),
  exportProfile: () => getJson<Record<string, unknown>>('/api/team/profile/export'),
  importProfile: (data: Record<string, unknown>) => postJson('/api/team/profile/import', data),
  savePrompt: (role: string, sections: Record<string, string>, scope = 'global', roomId = '') =>
    postJson('/api/team/prompts', { role, scope, room_id: roomId, sections }),
  resetPrompt: (role: string, scope = 'global', roomId = '') =>
    postJson('/api/team/prompts/reset', { role, scope, room_id: roomId }),

  subscribe(onTeam: (state: TeamState) => void): () => void {
    if (typeof EventSource === 'undefined') return () => {};
    const es = new EventSource('/api/stream');
    const handler = (ev: MessageEvent) => {
      try {
        const parsed = JSON.parse(ev.data) as TeamState;
        onTeam(parsed);
      } catch {
        // 略過非 JSON 心跳
      }
    };
    es.addEventListener('team', handler);
    return () => {
      es.removeEventListener('team', handler);
      es.close();
    };
  },
};

// 相容舊 officeApi 呼叫點（逐步替換為 teamApi）
export const officeApi = {
  state: () => getJson<OfficeSnapshot>('/api/state'),
  assist: (workerId: string, actionLabel: string, message: string) =>
    postJson('/api/team/send', { id: workerId, text: message || actionLabel }),
  setMode: (workerId: string, mode: SessionMode) =>
    postJson('/api/team/rooms/delegate', { id: workerId, on: mode === 'auto' }),
  setRunning: (running: boolean) => postJson('/api/team/pause_all', { paused: !running }),
};
