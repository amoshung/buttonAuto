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
