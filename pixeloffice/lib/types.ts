export type ToolCategory = 'ide' | 'browser' | 'terminal' | 'design' | 'other';

export type WorkerStatus = 'working' | 'needs_help' | 'idle' | 'paused' | 'error';

export type FacingDirection = 'up' | 'down' | 'left' | 'right';

export interface AvatarConfig {
  hairStyle: 'short' | 'bob' | 'spiky' | 'bun' | 'cap';
  hairColor: string;
  shirtColor: string;
  skinTone: string;
  accessory: 'none' | 'glasses' | 'headphones';
}

export interface ActivityLogEntry {
  id: string;
  timestamp: string;
  type: 'info' | 'help_request' | 'assisted' | 'error' | 'status_change';
  message: string;
}

export interface Point {
  x: number;
  y: number;
}

export interface AgentWorker {
  id: string;
  name: string;
  role: string;
  appName: string;
  windowTitle: string;
  category: ToolCategory;
  status: WorkerStatus;
  activity: string;
  helpPrompt?: string;
  quickActions?: string[];
  deskId: string;
  avatar: AvatarConfig;
  lastHeartbeat: string;
  source: 'demo' | 'watcher';
  logs: ActivityLogEntry[];
  // Simulation coordinates (in tile units, floats for smooth movement)
  pos: Point;
  path: Point[];
  facing: FacingDirection;
  behaviorState: 'at_desk' | 'walking' | 'at_coffee' | 'at_lounge';
  celebrateUntil: number; // epoch ms for [✓] bubble after user assists
  targetSpotId?: string;
  // ---- AI 工程助理提供的欄位（dashboard.py）
  mode?: SessionMode;
  modeLocked?: boolean; // 助理自己的專案：固定關閉
  phase?: 'goal' | 'spec' | 'build' | 'done';
  phaseLabel?: string;
  goal?: string;
  verified?: number;
  core?: number;
  hourSends?: number;
  hourCap?: number;
  approvalCommand?: string; // 代理等你允許執行的指令原文
  statusLabel?: string; // 給人看的一句狀態（例如「等你允許」「代理停下來，助理接手推進」）
  needsYou?: boolean;
  chatTitle?: string;
  pausedReason?: string;
  acceptance?: AcceptanceItem[];
  commits?: CommitInfo[];
  nextStep?: { action: string; reason: string; prompt: string; at: string };
  lastIssues?: string[];
}

export interface AcceptanceItem {
  text: string;
  status: 'verified' | 'claimed' | 'todo' | string;
  manual: boolean;
  checked: boolean;
}

export interface CommitInfo {
  hash: string;
  desc: string;
  branch: string;
  check: string;
}

export type SessionMode = 'auto' | 'suggest' | 'off';

export interface OfficeUsage {
  today: { auto_clicks: number; auto_sends: number; user_actions: number; verifies: number; asks: number };
  reads: Record<string, number>;
  models: Record<string, number>;
}

export interface AssistanceCommand {
  id: string;
  workerId: string;
  workerName: string;
  appName: string;
  actionLabel: string;
  message: string;
  createdAt: string;
  status: 'pending' | 'acknowledged';
}

export interface WatcherEventPayload {
  id?: string;
  name?: string;
  role?: string;
  appName: string;
  windowTitle: string;
  category?: ToolCategory;
  status: WorkerStatus;
  activity: string;
  helpPrompt?: string;
  quickActions?: string[];
}

export interface OfficeSnapshot {
  workers: AgentWorker[];
  commands: AssistanceCommand[];
  lastUpdated: string;
  externalEventsCount: number;
  running?: boolean; // 助理是否在運作（F8 全部暫停／繼續）
  usage?: OfficeUsage;
}

// ---- /api/team/* & /api/stream (Plan B) ----

export type TeamInboxKind =
  | 'approve_spec'
  | 'approve_plan'
  | 'permission'
  | 'question'
  | 'merge'
  | 'retry'
  | 'error'
  | 'unverified'
  | 'stuck'
  | 'recommend'
  | 'login';

export interface TeamCriterion {
  id: string;
  text: string;
  check?: string;
  status?: string;
  done?: boolean;
}

export interface TeamRoom {
  id: string;
  name?: string;
  slug: string;
  goal: string;
  repo: string;
  ssh_host?: string;
  reference_root?: string;
  target_subdir?: string;
  target_subdirs?: string[];
  allowed_paths?: string[];
  branch?: string;
  status: string;
  phase: string;
  persona?: string;
  demo?: boolean;
  autonomy?: string;
  delegate?: boolean;
  impl_cli?: string;
  review_cli?: string;
  impl_seat?: string;
  review_seat?: string;
  impl_model?: string;
  review_model?: string;
  impl_model_actual?: string;
  review_model_actual?: string;
  impl_by?: string;
  fallback_from?: string;
  impl_pref?: { seat?: string; model?: string; no_global?: boolean };
  review_pref?: { seat?: string; model?: string; no_global?: boolean };
  supervisor_cli?: string;
  supervisor_seat?: string;
  supervisor_model?: string;
  supervisor_model_actual?: string;
  supervisor_pref?: { seat?: string; model?: string };
  role_prefs?: Record<string, { seat?: string; model?: string }>;
  seat?: string;
  model?: string;
  activity?: string;
  error?: string;
  criteria?: TeamCriterion[];
  events?: TeamEvent[];
  updated_at?: string;
  created_at?: string;
  rounds_done?: number;
  commits?: number;
  last_event?: string;
  context_pct?: number;
  // v2 LangGraph StateGraph + Decision-1 欄位
  engine_version?: string;
  task_mode?: 'standard' | 'triage' | string;
  d1_fast_review?: boolean;
  graph_node?: string;
  current_unit_id?: string;
  selected_plan_rel?: string;
  unit_artifacts?: Record<string, {
    unit_id?: string;
    title?: string;
    status?: string;
    commit_sha?: string;
    completed_at?: string;
    verify?: {
      cmd?: string;
      exit_code?: number;
      seconds?: number;
      tail_output?: string;
      passed?: boolean;
      failure_class?: string;
    };
    review?: {
      d1_scores?: Record<string, number>;
      d1_risk?: string;
      d1_focus_areas?: string[];
      deep_reviewed?: boolean;
      blocking?: string[];
      suggestions?: string[];
    };
  }>;
}

export interface SshHostInfo {
  target: string;
  label: string;
  source: 'ssh_config' | 'custom' | string;
  hostname?: string;
  user?: string;
  port?: number | null;
  probed?: boolean;
  reachable?: boolean | null;
  available_seats?: string[];
  probed_at?: string;
  error?: string;
  has_password?: boolean;
  probe?: {
    ok?: boolean;
    auth_required?: boolean;
    error?: string;
    available_seats?: string[];
    hostname?: string;
    platform?: string;
    checked_at?: number;
  };
}

export interface SshProbeResult {
  ok: boolean;
  ssh_host: string;
  reachable?: boolean;
  auth_required?: boolean;
  has_password?: boolean;
  key_installed?: boolean;
  key_message?: string;
  hostname?: string;
  platform?: string;
  home?: string;
  available_seats: string[];
  clis: Record<
    string,
    {
      name: string;
      label: string;
      path: string;
      protocol: string;
      callable: boolean;
      version?: string;
      models?: Array<{ id: string; name?: string; description?: string }>;
      current_model?: string;
      current?: string;
    }
  >;
  probed_at?: string;
  error?: string;
  text?: string;
}

export interface TeamModelOption {
  id: string;
  name?: string;
  tier?: string;
  cost?: number;
}

export interface TeamModelsView {
  ssh_host?: string;
  available_seats?: string[];
  seats?: Record<
    string,
    {
      available?: boolean;
      installed?: boolean;
      detecting?: boolean;
      known?: boolean;
      models?: Array<{ modelId?: string; id?: string; name?: string; description?: string }>;
      selected?: string;
    }
  >;
  catalog?: Record<string, TeamModelOption[]>;
  [key: string]: unknown;
}

export interface TeamInboxItem {
  id: string;
  room_id: string;
  rooms?: string[];
  kind: TeamInboxKind;
  title: string;
  text: string;
  detail?: string;
  options?: Array<{ id: string; label: string; hint?: string }>;
  handoff?: boolean;
  created_at?: string;
}

export type InboxItem = TeamInboxItem;

export interface TeamEvent {
  id: string;
  room_id: string;
  ts: string;
  kind: string;
  role?: string;
  text: string;
  outcome?: string;
  has_detail?: boolean;
  detail?: Record<string, unknown>;
}

export interface TeamSeatStatus {
  seat: string;
  label?: string;
  installed?: boolean;
  exhausted?: boolean;
  state: 'ok' | 'exhausted' | 'unavailable' | string;
  until?: string;
  since?: string;
  reason?: string;
}

export interface ArchivedRoomSummary {
  id: string;
  name: string;
  repo: string;
  target_subdir?: string;
  target_subdirs?: string[];
  reference_root?: string;
  goal?: string;
  branch?: string;
  phase?: string;
  status?: string;
  updated_at?: string;
  criteria_count?: number;
}

export interface RoomProgressInfo {
  markdown: string;
  store_path?: string;
  notes_path?: string;
  updated_at?: string;
  active_criterion?: TeamCriterion | null;
  target_subdirs?: string[];
}

export interface CePlanInfo {
  has_plan?: boolean;
  active_plan?: {
    filename: string;
    rel_path: string;
    title: string;
    date?: string;
    objective?: string;
    test_cmd?: string;
    units?: Array<{ id: string; text: string; check?: string; done?: boolean }>;
  } | null;
  plans?: Array<{
    filename: string;
    rel_path: string;
    title: string;
    date?: string;
    objective?: string;
    unit_count?: number;
  }>;
  objective?: string;
  criteria?: TeamCriterion[];
  test_cmd?: string;
}

export interface WikiPageSummary {
  id: string;
  title: string;
  summary: string;
  namespace: string;
  rooms?: string[];
  links?: string[];
  updated_at?: string;
}

export interface WikiView {
  repo_id?: string;
  wiki_dir?: string;
  index_md?: string;
  pages?: WikiPageSummary[];
  namespaces?: Record<string, string>;
}

export interface TeamState {
  rooms: TeamRoom[];
  archived_rooms?: ArchivedRoomSummary[];
  inbox: TeamInboxItem[];
  fleet: Record<string, TeamSeatStatus>;
  paused_all?: boolean;
  demo?: boolean;
  version?: string;
}

export interface TeamProject {
  repo?: string;
  path?: string;
  name: string;
}

export interface TeamScanResult {
  repo?: string;
  path?: string;
  name: string;
  problem?: string;
  branch?: string;
  checks?: string[];
  branches?: string[];
  subdirs?: string[];
  dirty?: boolean | string[];
  progress?: {
    file?: string;
    task?: string;
    excerpt?: string;
  };
  docs?: Array<{
    relpath: string;
    title: string;
    modified?: string;
  }>;
}

export interface TeamUsageSummary {
  today?: Record<string, number>;
  by_model?: Record<string, number>;
  by_seat?: Record<string, number>;
  calls?: number;
  [key: string]: unknown;
}

export interface BrainstormMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface BrainstormResponse {
  ok: boolean;
  text?: string;
  mode?: 'chat' | 'generate_plan';
  seat?: string;
  model?: string;
  reply?: string;
  filename?: string;
  content?: string;
}

export interface SaveBrainstormPlanResponse {
  ok: boolean;
  text?: string;
  plan_rel?: string;
  path?: string;
  room?: TeamRoom;
  ce?: CePlanInfo;
}
