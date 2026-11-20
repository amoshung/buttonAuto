'use client';

import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  Archive,
  BookOpen,
  Bot,
  CheckCircle2,
  Circle,
  CircleDot,
  Copy,
  Cpu,
  Eye,
  FileText,
  FolderGit2,
  GitCommit,
  MessageSquare,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
  Sparkles,
  Target,
  Trash2,
  Users,
  X,
} from 'lucide-react';
import type {
  AgentWorker,
  CePlanInfo,
  InboxItem,
  RoomProgressInfo,
  SessionMode,
  TeamEvent,
  TeamModelsView,
  TeamRoom,
  WikiView,
} from '@/lib/types';
import { teamApi } from '@/lib/api';
import GoalBrainstormModal from './GoalBrainstormModal';
import {
  type PersonaSlotKey,
  buildRoomScene,
  findActiveCriterionIndex,
  getPersonaRecentRecords,
  sortEventsDesc,
} from '@/lib/pixelRoomScene';
import { PHASES, copyTextToClipboard, formatTime, progressPct, statusTone } from '@/lib/ui';

interface SessionPanelProps {
  worker: AgentWorker | null;
  room?: TeamRoom | null;
  inboxItems?: InboxItem[];
  modelsView?: TeamModelsView | null;
  selectedPersonaSlot?: PersonaSlotKey | null;
  onSelectPersonaSlot?: (slot: PersonaSlotKey | null) => void;
  onClose?: () => void;
  onSelectRoom?: (roomId: string) => void;
  onAssist: (workerId: string, actionLabel: string, message: string) => Promise<void>;
  onSetMode: (workerId: string, mode: SessionMode) => Promise<void>;
  onRoomUpdated?: () => Promise<void> | void;
  onRefreshModels?: () => Promise<void> | void;
  onToast?: (msg: string) => void;
  busy: boolean;
}

export const SEAT_CHOICES = [
  { id: 'auto', label: '自動派工（Decision-1）' },
  { id: 'claude', label: 'Claude' },
  { id: 'kiro', label: 'Kiro' },
  { id: 'agy', label: 'Antigravity' },
  { id: 'glm', label: 'GLM-5.3（一般思考）' },
  { id: 'glm_high', label: 'GLM-5.3（高思考）' },
];

export function filterCallableSeatChoices(
  view: TeamModelsView | null | undefined,
): Array<{ id: string; label: string }> {
  if (!view) return SEAT_CHOICES;
  const availSet = Array.isArray(view.available_seats)
    ? new Set(view.available_seats.map((s) => String(s)))
    : view.seats
      ? new Set(
          Object.entries(view.seats)
            .filter(([, v]) => Boolean(v?.available ?? v?.installed))
            .map(([k]) => k),
        )
      : null;
  if (!availSet) return SEAT_CHOICES;
  return SEAT_CHOICES.filter((c) => c.id === 'auto' || availSet.has(c.id));
}

const FALLBACK_MODELS: Record<string, Array<{ id: string; label: string }>> = {
  claude: [
    { id: 'sonnet', label: 'sonnet（Claude 最新標準別名）' },
    { id: 'opus', label: 'opus（Claude 最新旗艦別名）' },
    { id: 'haiku', label: 'haiku（Claude 最新輕量別名）' },
    { id: 'fable', label: 'fable（Claude Fable 別名）' },
    { id: 'claude-opus-5-5', label: 'claude-opus-5-5' },
    { id: 'claude-sonnet-5-5', label: 'claude-sonnet-5-5' },
    { id: 'claude-haiku-5-5', label: 'claude-haiku-5-5' },
    { id: 'claude-fable-5-2', label: 'claude-fable-5-2' },
    { id: 'claude-sonnet-4-6', label: 'claude-sonnet-4-6' },
    { id: 'claude-sonnet-4-5', label: 'claude-sonnet-4-5' },
    { id: 'claude-opus-4-5', label: 'claude-opus-4-5' },
    { id: 'claude-haiku-4-5', label: 'claude-haiku-4-5' },
  ],
  kiro: [
    { id: 'claude-sonnet-5.5', label: 'claude-sonnet-5.5' },
    { id: 'claude-opus-5.5', label: 'claude-opus-5.5' },
    { id: 'claude-haiku-5.5', label: 'claude-haiku-5.5' },
    { id: 'gpt-5.6-sol', label: 'gpt-5.6-sol' },
    { id: 'gpt-5.6-terra', label: 'gpt-5.6-terra' },
    { id: 'grok-4.7', label: 'grok-4.7' },
  ],
  agy: [
    { id: 'gemini-4-carbon', label: 'Gemini 4 Carbon' },
    { id: 'gemini-4-hydrogen', label: 'Gemini 4 Hydrogen' },
    { id: 'gemini-4-argon', label: 'Gemini 4 Argon' },
    { id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash' },
    { id: 'gemini-3.1-pro', label: 'Gemini 3.1 Pro' },
  ],
  glm: [{ id: 'glm-5.3', label: 'glm-5.3（一般思考）' }],
  glm_high: [{ id: 'glm-5.3', label: 'glm-5.3（高思考）' }],
};

export function seatModelOptions(
  view: TeamModelsView | null | undefined,
  seat: string,
  currentModel?: string
): Array<{ id: string; label: string }> {
  if (!seat || seat === 'auto') return [];
  const seen = new Set<string>();
  const out: Array<{ id: string; label: string }> = [];
  const add = (id: string, label: string) => {
    const clean = id.trim();
    if (!clean || clean === 'auto' || seen.has(clean)) return;
    seen.add(clean);
    out.push({ id: clean, label: label || clean });
  };

  if (currentModel && currentModel !== 'auto') {
    add(currentModel, `${currentModel}（目前選用）`);
  }

  const detected = view?.seats?.[seat]?.models ?? [];
  for (const m of detected) {
    const id = String(m.modelId || m.id || '');
    const name = String(m.name || id);
    add(id, name !== id ? `${name} (${id})` : id);
  }

  const cat = view?.catalog?.[seat] ?? [];
  for (const m of cat) {
    add(String(m.id || ''), String(m.name || m.id || ''));
  }

  for (const fb of FALLBACK_MODELS[seat] ?? []) {
    add(fb.id, fb.label);
  }

  return out;
}

export type JobRoleKey = 'supervisor' | 'impl' | 'review';

export interface JobRoleDefinition {
  id: JobRoleKey;
  title: string;
  seatLabel: string;
  modelLabel: string;
  tier: 'supervisor' | 'developer';
  autoSeatLabel: string;
  autoDefaultModelLabel: string;
  defaultLitellmModel: string;
  description: string;
}

export const ROLE_DEFINITIONS: JobRoleDefinition[] = [
  {
    id: 'supervisor',
    title: '👔 高層主管',
    seatLabel: '👔 高層主管（主決策：Decision-1）',
    modelLabel: '🤝 與 Decision-1 協作的生成模型',
    tier: 'supervisor',
    autoSeatLabel: 'Decision-1 ＋ 協作生成模型（預設）',
    autoDefaultModelLabel: '預設：glm-5.3（與 Decision-1 雙模型協作）',
    defaultLitellmModel: 'glm-5.3',
    description:
      '高層主管（雙模型協作大腦）：由 Decision-1 負責機率與多選一裁決，搭配你指定的協作生成模型（預設 GLM-5.3）進行規格計畫定錨（ce-brainstorm / ce-plan）、權限把關、回答提問，並在審查工程師指出目標偏離時拍板修正方向。',
  },
  {
    id: 'impl',
    title: '🛠️ 研發工程師',
    seatLabel: '🛠️ 研發工程師 執行工具',
    modelLabel: '研發工程師 模型',
    tier: 'developer',
    autoSeatLabel: '自動派工（Decision-1）',
    autoDefaultModelLabel: '預設：sonnet / glm-5.3（依難度自動選）',
    defaultLitellmModel: 'glm-5.3',
    description:
      '研發工程師（全流程實作與除錯）：依核定計畫執行 TDD 測試、功能實作、根因除錯、程式碼精簡與 Git Commit（ce-work / ce-debug / ce-simplify-code / ce-commit）；當審查工程師指出偏離位置與高層主管決定方向後，由研發工程師決定具體程式碼改法。',
  },
  {
    id: 'review',
    title: '🔍 審查工程師',
    seatLabel: '🔍 審查工程師 執行工具',
    modelLabel: '審查工程師 模型',
    tier: 'developer',
    autoSeatLabel: '自動派工（Decision-1）',
    autoDefaultModelLabel: '預設：glm-5.3 高思考 / opus（盤中防偏離＋獨立審查）',
    defaultLitellmModel: 'glm-5.3',
    description:
      '審查工程師（盤中增量防偏離 ＋ 唯讀獨立審查）：開發過程中隨新進度即透過 Decision-1 檢查是否偏離目標；只精確指出偏離位置（檔案:行號與原因）交由高層主管與研發工程師決定怎麼改，並透過 ce-compound 將經驗沉澱至 LLM Wiki。',
  },
];

export function getRoleTooltip(slotOrRole: string): string {
  const key = String(slotOrRole || '').toLowerCase();
  if (key === 'supervisor' || key === 'po' || key === 'tech_lead' || key === 'scrum' || key === 'qa') {
    return ROLE_DEFINITIONS[0].description;
  }
  if (key === 'impl' || key === 'implementer' || key === 'developer' || key === 'fixer') {
    return ROLE_DEFINITIONS[1].description;
  }
  if (key === 'review' || key === 'reviewer') {
    return ROLE_DEFINITIONS[2].description;
  }
  return '';
}

export function defaultModelForSeat(
  seat: string,
  view: TeamModelsView | null | undefined,
  catalogModels: Array<{ id: string; label?: string }> = [],
  role: JobRoleKey = 'impl',
): string {
  if (!seat || seat === 'auto') return '';
  const roleDef = ROLE_DEFINITIONS.find((r) => r.id === role);
  if (seat === 'litellm') {
    const preferred = roleDef?.defaultLitellmModel || 'glm-5.3';
    if (catalogModels.some((m) => m.id === preferred)) return preferred;
    return preferred;
  }
  const selected = view?.seats?.[seat]?.selected;
  if (selected && selected !== 'auto') return selected;
  const opts = seatModelOptions(view, seat);
  return opts[0]?.id || '';
}

export function buildRoleModelDropdownOptions(
  role: JobRoleKey,
  seat: string,
  currentModel: string,
  view: TeamModelsView | null | undefined,
  catalogModels: Array<{
    id: string;
    label: string;
    input_usd_per_1m?: number | null;
    output_usd_per_1m?: number | null;
  }> = [],
  callableChoices: Array<{ id: string; label: string }> = [],
  otherDevSeat = '',
): Array<{ id: string; label: string }> {
  const litellmItems = () => {
    const seen = new Set<string>();
    const list: Array<{ id: string; label: string }> = [];
    const pushItem = (id: string, label: string) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      list.push({ id, label });
    };
    pushItem('Decision-1', 'Decision-1（高階決策／機率路由模型）');
    pushItem('glm-5.3', 'glm-5.3（主力程式碼與推理模型）');
    for (const m of catalogModels) {
      const priceStr =
        m.input_usd_per_1m !== null && m.input_usd_per_1m !== undefined
          ? ` ($${m.input_usd_per_1m}/$${m.output_usd_per_1m ?? '—'} per 1M)`
          : '';
      pushItem(m.id, `${m.label || m.id}${priceStr}`);
    }
    return list;
  };

  if (seat === 'litellm') {
    return litellmItems();
  }
  if (seat && seat !== 'auto') {
    return seatModelOptions(view, seat, currentModel);
  }

  // 當座位為 'auto' 時，仍列出所有可調用 CLI 與 LiteLLM 的模型（編碼為 @seat:model），讓使用者可直接從模型下拉選單挑選！
  const combined: Array<{ id: string; label: string }> = [];
  for (const choice of callableChoices) {
    if (choice.id === 'auto') continue;
    if ((role === 'impl' || role === 'review') && otherDevSeat && otherDevSeat !== 'auto' && choice.id === otherDevSeat) {
      continue;
    }
    const sOpts = seatModelOptions(view, choice.id);
    for (const m of sOpts) {
      combined.push({
        id: `@${choice.id}:${m.id}`,
        label: `[${choice.label}] ${m.label}`,
      });
    }
  }
  for (const lm of litellmItems()) {
    combined.push({
      id: `@litellm:${lm.id}`,
      label: `[LiteLLM] ${lm.label}`,
    });
  }
  return combined;
}

export function extractRoleSeatAndModel(
  room: TeamRoom | null | undefined,
  role: JobRoleKey,
): { seat: string; model: string } {
  if (!room) return { seat: 'auto', model: '' };
  const rolePref = room.role_prefs?.[role];
  if (rolePref?.seat && rolePref.seat !== 'auto') {
    return {
      seat: rolePref.seat,
      model: rolePref.model && rolePref.model !== 'auto' ? rolePref.model : '',
    };
  }
  if (role === 'impl' || role === 'review') {
    const pref = role === 'impl' ? room.impl_pref : room.review_pref;
    const rawCli = role === 'impl' ? room.impl_cli : room.review_cli;
    const rawSeat = role === 'impl' ? room.impl_seat : room.review_seat;
    const seat =
      (pref && pref.seat && pref.seat !== 'auto' ? pref.seat : '') ||
      (rawCli && rawCli !== 'auto' ? rawCli : '') ||
      (rawSeat && rawSeat !== 'auto' ? rawSeat : '') ||
      'auto';
    const model =
      (pref && pref.model ? pref.model : '') ||
      (role === 'impl' ? room.impl_model || '' : room.review_model || '');
    return { seat, model: model === 'auto' ? '' : model };
  }
  // 高階主管職位（po / tech_lead / scrum / qa）若無個別設定，繼承 supervisor_pref
  const supSeat =
    room.supervisor_pref?.seat || room.role_prefs?.supervisor?.seat || room.supervisor_cli || 'auto';
  const supModel =
    room.supervisor_pref?.model || room.role_prefs?.supervisor?.model || room.supervisor_model || '';
  return {
    seat: supSeat || 'auto',
    model: supModel === 'auto' ? '' : supModel,
  };
}

function actionTone(label: string): string {
  const l = label.toLowerCase();
  if (/(deny|reject|skip|^no\b|拒絕|略過|取消)/.test(l)) return 'bg-rose-500/90 hover:bg-rose-400 text-white';
  if (/(always|一律|永遠|in this conversation)/.test(l)) return 'bg-[#23293b] hover:bg-[#2d354d] text-slate-300 border border-[#323a52]';
  if (/(allow|accept|run|yes|允許|送出|執行|同意|恢復|合併|重試)/.test(l)) return 'bg-amber-400 hover:bg-amber-300 text-slate-950';
  return 'bg-[#23293b] hover:bg-[#2d354d] text-slate-100 border border-[#323a52]';
}

const ACTION_LABEL: Record<string, string> = {
  request_spec: '請代理同步 CE 計畫',
  revise_spec: '請代理更新 CE 計畫',
  await_spec_confirm: '等你確認 CE 計畫',
  request_evidence: '要代理提出證據',
  next_task: '推進下一個工作',
  self_decide: '請代理自行決定做法',
  answer: '回答代理的問題',
  rollover: '開新對話接續',
  ask_goal: '詢問最終目標',
  ask_human: '交給你決定',
  approve: '處理允許',
  wait: '等代理回應',
  done: '已達成',
  new_goal: '開始新目標',
};

export default function SessionPanel({
  worker,
  room,
  inboxItems = [],
  modelsView,
  selectedPersonaSlot,
  onSelectPersonaSlot,
  onClose,
  onSelectRoom,
  onAssist,
  onSetMode,
  onRoomUpdated,
  onRefreshModels,
  onToast,
  busy,
}: SessionPanelProps) {
  const [tab, setTab] = useState<'overview' | 'ce' | 'wiki' | 'log'>('overview');
  const [localPersonaSlot, setLocalPersonaSlot] = useState<PersonaSlotKey | null>('implementer');
  const activePersonaSlot = selectedPersonaSlot !== undefined ? selectedPersonaSlot : localPersonaSlot;
  const setActivePersonaSlot = (slot: PersonaSlotKey | null) => {
    setLocalPersonaSlot(slot);
    onSelectPersonaSlot?.(slot);
  };
  const [text, setText] = useState('');
  const [goalDraft, setGoalDraft] = useState<string>(room?.goal ?? worker?.goal ?? '');
  const [repoDraft, setRepoDraft] = useState<string>(room?.repo ?? '');
  const [refRootDraft, setRefRootDraft] = useState<string>(room?.reference_root ?? '');
  const [targetSubdirDraft, setTargetSubdirDraft] = useState<string>(room?.target_subdir ?? '');
  const [targetSubdirsDraft, setTargetSubdirsDraft] = useState<string[]>(
    room?.target_subdirs && room.target_subdirs.length > 0
      ? room.target_subdirs
      : room?.target_subdir
        ? [room.target_subdir]
        : [],
  );
  const [extraTargetInput, setExtraTargetInput] = useState<string>('');
  const [folderEditorOpen, setFolderEditorOpen] = useState(false);
  const [brainstormOpen, setBrainstormOpen] = useState(false);
  const [browserTarget, setBrowserTarget] = useState<'repo' | 'ref' | null>(null);
  const [browserData, setBrowserData] = useState<{
    path: string;
    parent: string;
    dirs: Array<{ name: string; path: string }>;
    drives: string[];
  } | null>(null);
  const [refSubdirs, setRefSubdirs] = useState<string[]>([]);
  const [inboxReply, setInboxReply] = useState<Record<string, string>>({});
  const [teamEvents, setTeamEvents] = useState<TeamEvent[]>([]);
  const [ceInfo, setCeInfo] = useState<CePlanInfo | null>(null);
  const [wikiView, setWikiView] = useState<WikiView | null>(null);
  const [progressInfo, setProgressInfo] = useState<RoomProgressInfo | null>(null);
  const [progressExpanded, setProgressExpanded] = useState<boolean>(false);
  const [selectedPlanRel, setSelectedPlanRel] = useState<string>('');
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [busyRoomCounts, setBusyRoomCounts] = useState<Record<string, number>>({});
  const ctrlBusy = Boolean(worker?.id && (busyRoomCounts[worker.id] ?? 0) > 0);
  const setRoomBusy = (rid: string, busy: boolean) => {
    if (!rid) return;
    setBusyRoomCounts((prev) => {
      const cur = prev[rid] ?? 0;
      const next = busy ? cur + 1 : Math.max(0, cur - 1);
      if (next === cur) return prev;
      return { ...prev, [rid]: next };
    });
  };
  const [detectingModels, setDetectingModels] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string>('');

  const handleCopy = async (key: string, content: string) => {
    const ok = await copyTextToClipboard(content);
    if (ok) {
      setCopiedKey(key);
      onToast?.('已複製訊息到剪貼簿');
      setTimeout(() => setCopiedKey((prev) => (prev === key ? '' : prev)), 2000);
    }
  };

  const buildInitialRoleMap = (rm: TeamRoom | null | undefined): Record<JobRoleKey, { seat: string; model: string }> => ({
    supervisor: extractRoleSeatAndModel(rm, 'supervisor'),
    impl: extractRoleSeatAndModel(rm, 'impl'),
    review: extractRoleSeatAndModel(rm, 'review'),
  });
  const [roleMap, setRoleMap] = useState<Record<JobRoleKey, { seat: string; model: string }>>(() =>
    buildInitialRoleMap(room),
  );
  const [catalogModels, setCatalogModels] = useState<
    Array<{ id: string; label: string; input_usd_per_1m: number | null; output_usd_per_1m: number | null }>
  >([]);
  const [scanningCatalog, setScanningCatalog] = useState(false);
  const [roomModelsView, setRoomModelsView] = useState<TeamModelsView | null>(null);
  const roomSshHost = (room?.ssh_host || '').trim();
  const [sshAuthOpen, setSshAuthOpen] = useState(false);
  const [sshAuthPw, setSshAuthPw] = useState('');
  const [sshAuthBusy, setSshAuthBusy] = useState(false);
  const [sshAuthVerified, setSshAuthVerified] = useState(false);
  const [sshAuthKeyInstalled, setSshAuthKeyInstalled] = useState(false);
  const sshErrorHint = Boolean(
    roomSshHost &&
      room?.error &&
      /ssh|permission denied|connection closed|找不到目標資料夾|連線失敗/i.test(room.error),
  );
  const showSshAuthBox = Boolean(roomSshHost && (sshAuthOpen || (sshErrorHint && !sshAuthVerified)));
  useEffect(() => {
    setSshAuthVerified(false);
    setSshAuthKeyInstalled(false);
    setSshAuthOpen(false);
    setSshAuthPw('');
  }, [room?.id, roomSshHost]);
  const submitSshAuth = async () => {
    if (!roomSshHost || !sshAuthPw || sshAuthBusy) return;
    setSshAuthBusy(true);
    try {
      const res = await teamApi.probeSsh(roomSshHost, true, sshAuthPw, true, true);
      if (res.ok) {
        setSshAuthPw('');
        setSshAuthOpen(false);
        setSshAuthVerified(true);
        setSshAuthKeyInstalled(Boolean(res.key_installed));
        onToast?.(
          `✅ SSH 連線成功（${roomSshHost}）${res.key_installed ? '，已自動安裝免密碼公鑰' : ''}；可按「重試／繼續」推進任務`,
        );
        // 驗證成功後同步刷新遠端模型清單與唯讀參考子資料夾，避免下拉選單停留在舊狀態
        void teamApi.models(roomSshHost).then(setRoomModelsView).catch(() => {});
        if (room?.reference_root) {
          void loadRefSubdirs(room.reference_root).catch(() => {});
        }
        await onRoomUpdated?.();
      } else {
        setSshAuthVerified(false);
        onToast?.(String(res.error || res.text || 'SSH 密碼驗證失敗'));
      }
    } catch (err) {
      setSshAuthVerified(false);
      onToast?.(`SSH 密碼驗證失敗：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSshAuthBusy(false);
    }
  };
  const effectiveModelsView = roomSshHost ? roomModelsView ?? modelsView : modelsView;

  useEffect(() => {
    void teamApi
      .getModelCatalog()
      .then((res) => {
        if (res?.models) setCatalogModels(res.models);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    setRoleMap(buildInitialRoleMap(room));
    setGoalDraft(room?.goal ?? worker?.goal ?? '');
    setRepoDraft(room?.repo ?? '');
    setRefRootDraft(room?.reference_root ?? '');
    setTargetSubdirDraft(room?.target_subdir ?? '');
    setTargetSubdirsDraft(
      room?.target_subdirs && room.target_subdirs.length > 0
        ? room.target_subdirs
        : room?.target_subdir
          ? [room.target_subdir]
          : [],
    );
    setTeamEvents([]);
    if (roomSshHost) {
      void teamApi.models(roomSshHost).then(setRoomModelsView).catch(() => {});
    } else {
      setRoomModelsView(null);
    }
    if (room?.reference_root) {
      void teamApi
        .browseDirs(room.reference_root, roomSshHost)
        .then((d) => {
          if (d?.dirs) setRefSubdirs(d.dirs.map((x) => x.name));
        })
        .catch(() => {});
    } else {
      setRefSubdirs([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room?.id, room?.repo, room?.ssh_host, room?.reference_root, room?.target_subdir, (room?.target_subdirs ?? []).join(',')]);

  const loadRoomEvents = async (wid?: string) => {
    const id = wid ?? worker?.id;
    if (!id) return;
    setLoadingEvents(true);
    try {
      const r = await teamApi.room(id);
      if (r?.ce) {
        setCeInfo(r.ce);
        const preferredRel = r.room?.selected_plan_rel || r.ce.active_plan?.rel_path || '';
        if (preferredRel && (!selectedPlanRel || r.room?.selected_plan_rel)) {
          setSelectedPlanRel(preferredRel);
        }
      }
      if (r?.wiki) {
        setWikiView(r.wiki);
      }
      if (r?.progress) {
        setProgressInfo(r.progress);
      }
      if (r?.events) {
        // 轉為 DESC 排序（最新在最上面），並為最新具有 detail 的事件載入詳細內容
        const desc = sortEventsDesc(r.events);
        setTeamEvents(desc);
        const needIds = desc.filter((e) => e.has_detail).slice(0, 10).map((e) => e.id);
        if (needIds.length > 0) {
          const det = await teamApi.roomEvents(id, needIds);
          if (det?.events) {
            const byId = new Map(det.events.map((d) => [d.id, d]));
            setTeamEvents((prev) => prev.map((ev) => byId.get(ev.id) ?? ev));
          }
        }
      }
    } finally {
      setLoadingEvents(false);
    }
  };

  const latestSnapEventId = room?.events && room.events.length > 0 ? room.events[room.events.length - 1]?.id : '';

  useEffect(() => {
    if (worker?.id) {
      void loadRoomEvents(worker.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [worker?.id, room?.updated_at, latestSnapEventId]);

  const handleDetectAllModels = async (seat = 'all') => {
    setDetectingModels(true);
    try {
      if (roomSshHost) {
        const res = await teamApi.probeSsh(roomSshHost, true);
        onToast?.(res.text || `已重新偵測遠端 ${roomSshHost} 可調用 CLI 與模型`);
        const m = await teamApi.models(roomSshHost);
        setRoomModelsView(m);
      } else {
        const res = await teamApi.detectModels(seat);
        onToast?.(res.text || '已開始偵測所有 CLI 可用模型');
        await onRefreshModels?.();
        setTimeout(() => void onRefreshModels?.(), 1000);
        setTimeout(() => void onRefreshModels?.(), 2500);
      }
    } finally {
      setDetectingModels(false);
    }
  };

  const changeModel = async (role: JobRoleKey, rawSeat: string, rawModel: string) => {
    if (!worker) return;
    let nextSeat = rawSeat;
    let nextModel = rawModel;
    if (rawModel.startsWith('@')) {
      const colonIdx = rawModel.indexOf(':');
      if (colonIdx > 1) {
        nextSeat = rawModel.slice(1, colonIdx);
        nextModel = rawModel.slice(colonIdx + 1);
      }
    } else if (nextSeat !== 'auto' && !nextModel) {
      nextModel = defaultModelForSeat(nextSeat, effectiveModelsView, catalogModels, role);
    }

    setRoleMap((prev) => ({
      ...prev,
      [role]: { seat: nextSeat, model: nextSeat === 'auto' ? '' : nextModel },
    }));

    if (nextSeat !== 'auto' && ['claude', 'kiro', 'agy'].includes(nextSeat)) {
      const seatInfo = effectiveModelsView?.seats?.[nextSeat];
      if (!seatInfo?.known && !seatInfo?.detecting) {
        void teamApi.detectModels(nextSeat).then(() => {
          setTimeout(() => void onRefreshModels?.(), 900);
        });
      }
    }

    const rid = worker.id;
    setRoomBusy(rid, true);
    try {
      if (role === 'impl' || role === 'review') {
        const otherRole: JobRoleKey = role === 'impl' ? 'review' : 'impl';
        const otherSeat = roleMap[otherRole]?.seat || 'auto';
        if (nextSeat !== 'auto' && nextSeat !== 'litellm' && otherSeat === nextSeat) {
          setRoleMap((prev) => ({
            ...prev,
            [otherRole]: { seat: 'auto', model: '' },
          }));
          await teamApi.setRoomModel(rid, otherRole, 'auto', '');
        }
      }
      const r = await teamApi.setRoomModel(
        rid,
        role,
        nextSeat === 'auto' ? 'auto' : nextSeat,
        nextSeat === 'auto' ? '' : nextModel,
      );
      onToast?.(r.text || (r.ok ? '已更新職位座位與模型' : '更新失敗'));
      await onRoomUpdated?.();
      await loadRoomEvents(rid);
    } finally {
      setRoomBusy(rid, false);
    }
  };

  const runCtrlAction = async (errLabel: string, fn: () => Promise<void>) => {
    const rid = worker?.id || '';
    setRoomBusy(rid, true);
    try {
      await fn();
    } catch (err) {
      onToast?.(`${errLabel}：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setRoomBusy(rid, false);
    }
  };

  const saveGoal = async (nextGoal: string) => {
    if (!worker) return;
    await runCtrlAction('儲存房間目標失敗', async () => {
      const r = await teamApi.setRoomGoal(worker.id, nextGoal);
      setGoalDraft(nextGoal);
      onToast?.(r.text || '已儲存房間目標');
      await onRoomUpdated?.();
      await loadRoomEvents(worker.id);
    });
  };

  const openDirBrowser = async (target: 'repo' | 'ref', startPath?: string) => {
    setBrowserTarget(target);
    const seed = startPath ?? (target === 'ref' ? refRootDraft || repoDraft : repoDraft || refRootDraft);
    try {
      const d = await teamApi.browseDirs(seed || '', roomSshHost);
      if (d) setBrowserData(d);
    } catch (err) {
      onToast?.(`讀取資料夾清單失敗：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const loadRefSubdirs = async (refPath: string) => {
    if (!refPath.trim()) {
      setRefSubdirs([]);
      return;
    }
    try {
      const d = await teamApi.browseDirs(refPath.trim(), roomSshHost);
      if (d?.dirs) {
        setRefSubdirs(d.dirs.map((x) => x.name));
      }
    } catch {
      // 略過背景參考子資料夾讀取失敗，避免干擾操作
    }
  };

  const autoFillParentAsRef = async () => {
    const cur = (repoDraft || room?.repo || '').trim().replace(/[\\/]+$/, '');
    const idx = Math.max(cur.lastIndexOf('\\'), cur.lastIndexOf('/'));
    if (idx <= 2) return;
    const parent = cur.slice(0, idx);
    const sub = cur.slice(idx + 1);
    setRefRootDraft(parent);
    setTargetSubdirDraft(sub);
    setTargetSubdirsDraft((prev) => (prev.length > 0 ? prev : sub ? [sub] : []));
    await loadRefSubdirs(parent);
  };

  const saveFolders = async () => {
    if (!worker) return;
    await runCtrlAction('更新資料夾設定失敗', async () => {
      const effectiveSubdirs =
        targetSubdirsDraft.length > 0
          ? targetSubdirsDraft
          : targetSubdirDraft.trim()
            ? [targetSubdirDraft.trim()]
            : [];
      const r = await teamApi.setRoomFolders(worker.id, {
        repo: repoDraft.trim(),
        reference_root: refRootDraft.trim(),
        target_subdir: effectiveSubdirs[0] || targetSubdirDraft.trim(),
        target_subdirs: effectiveSubdirs,
      });
      onToast?.(r.text || (r.ok ? '已儲存目標工作資料夾與唯讀參考設定' : '更新資料夾設定失敗'));
      if (r.ok) {
        setFolderEditorOpen(false);
        setBrowserTarget(null);
        await onRoomUpdated?.();
        await loadRoomEvents(worker.id);
      }
    });
  };

  const refreshRoomProgress = async () => {
    if (!worker) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    try {
      const r = await teamApi.syncRoomProgress(rid);
      if (r.progress) {
        setProgressInfo(r.progress as RoomProgressInfo);
      }
      onToast?.(r.text || '已同步更新房間 progress.md 斷點紀錄');
      await loadRoomEvents(rid);
    } finally {
      setRoomBusy(rid, false);
    }
  };

  const [expandedArtifactId, setExpandedArtifactId] = useState<string>('');
  const [generatingPlanRoomId, setGeneratingPlanRoomId] = useState<string>('');
  const [busyStatus, setBusyStatus] = useState<{ roomId: string; text: string }>({ roomId: '', text: '' });
  const generatingPlan = Boolean(worker?.id && generatingPlanRoomId === worker.id);
  const activeBusyStatus = worker?.id && busyStatus.roomId === worker.id ? busyStatus.text : '';

  const handleToggleD1FastReview = async (enabled: boolean) => {
    if (!worker) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: enabled ? '正在開啟 Decision-1 快速審查分流…' : '正在關閉 Decision-1 快速審查分流…' });
    try {
      const r = await teamApi.setRoomD1FastReview(rid, enabled);
      onToast?.(
        r.ok
          ? enabled
            ? '已開啟 Decision-1 15 題快速審查分流'
            : '已關閉快速分流（全部單元皆走大模型深度審查）'
          : r.text || '切換失敗',
      );
      if (r.ok) {
        await onRoomUpdated?.();
        await loadRoomEvents(rid);
        if (r.plan_rel) {
          void openPlanPreview(String(r.plan_rel));
        }
      }
    } finally {
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const handleRerunUnit = async (unitId: string) => {
    if (!worker || !unitId) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: `正在重置單元 [${unitId}] 為待重跑狀態…` });
    try {
      const r = await teamApi.rerunRoomUnit(rid, unitId);
      onToast?.(r.text || (r.ok ? `已重置單元 [${unitId}] 為待重跑` : '重跑設定失敗'));
      if (r.ok) {
        await onRoomUpdated?.();
        await loadRoomEvents(rid);
        if (r.plan_rel) {
          void openPlanPreview(String(r.plan_rel));
        }
      }
    } finally {
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const handleWaiveUnit = async (unitId: string) => {
    if (!worker || !unitId) return;
    const reason = typeof window !== 'undefined' ? window.prompt(`豁免跳過單元 [${unitId}] 的原因（可留空）：`, '') : '';
    if (reason === null) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: `正在豁免跳過單元 [${unitId}]…` });
    try {
      const r = await teamApi.waiveRoomUnit(rid, unitId, reason || '');
      onToast?.(r.text || (r.ok ? `已豁免單元 [${unitId}]` : '豁免失敗'));
      if (r.ok) {
        await onRoomUpdated?.();
        await loadRoomEvents(rid);
        if (r.plan_rel) {
          void openPlanPreview(String(r.plan_rel));
        }
      }
    } finally {
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const handleEditUnitCheck = async (unitId: string, currentCheck = '') => {
    if (!worker || !unitId || typeof window === 'undefined') return;
    const nextCheck = window.prompt(`修改單元 [${unitId}] 的驗證指令（check_cmd）：`, currentCheck);
    if (nextCheck === null) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: `正在更新單元 [${unitId}] 的驗證指令…` });
    try {
      const r = await teamApi.editRoomUnit(rid, unitId, { check_cmd: nextCheck });
      onToast?.(r.text || (r.ok ? `已更新單元 [${unitId}] 驗證指令` : '更新失敗'));
      if (r.ok) {
        await onRoomUpdated?.();
        await loadRoomEvents(rid);
        if (r.plan_rel) {
          void openPlanPreview(String(r.plan_rel));
        }
      }
    } finally {
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const syncCePlan = async (planRel = '', applyGoal = false) => {
    if (!worker) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: `正在同步 CE 計畫${planRel ? `（${planRel.split('/').pop()}）` : ''}與階段單元…` });
    onToast?.('⏳ 正在同步 CE 計畫與里程碑，請稍候…');
    try {
      const r = await teamApi.syncRoomPlan(rid, planRel || selectedPlanRel, applyGoal);
      if (r.ce) {
        setCeInfo(r.ce as CePlanInfo);
      }
      if (applyGoal && r.room?.goal) {
        setGoalDraft(r.room.goal);
      }
      onToast?.(r.text || '已跟隨 Compound Engineering 計畫同步階段與知識庫');
      await onRoomUpdated?.();
      await loadRoomEvents(rid);
      if (r.ce) {
        setCeInfo(r.ce as CePlanInfo);
      }
    } finally {
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const clearCePlanAndProgress = async () => {
    if (!worker) return;
    if (typeof window !== 'undefined' && !window.confirm('確定要清空此房間的 CE 計畫單元與 progress.md 進度紀錄？（房間目標與程式碼分支會保留）')) {
      return;
    }
    const rid = worker.id;
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: '正在清空 CE 計畫單元與 progress.md 斷點紀錄…' });
    onToast?.('⏳ 正在清空計畫與進度，請稍候…');
    try {
      const r = await teamApi.clearRoomPlan(rid);
      if (r.progress) {
        setProgressInfo(r.progress as RoomProgressInfo);
      }
      onToast?.(r.text || (r.ok ? '已清空 CE 計畫與進度紀錄' : '清空 CE 計畫與進度失敗'));
      if (r.ok) {
        await onRoomUpdated?.();
        await loadRoomEvents(rid);
        if (r.plan_rel) {
          void openPlanPreview(String(r.plan_rel));
        }
      }
    } finally {
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const clearRoomWiki = async () => {
    if (!worker) return;
    if (typeof window !== 'undefined' && !window.confirm('確定要清空此專案的 LLM Wiki 知識庫？（清空後將隨著執行過程重新演化累積）')) {
      return;
    }
    const rid = worker.id;
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: '正在清空專案 LLM Wiki 知識庫…' });
    onToast?.('⏳ 正在清空 LLM Wiki 知識庫，請稍候…');
    try {
      const r = await teamApi.clearRoomWiki(rid);
      if (r.wiki) {
        setWikiView(r.wiki as WikiView);
      }
      onToast?.(r.text || (r.ok ? '已清空專案 LLM Wiki 知識庫' : '清空 LLM Wiki 失敗'));
      if (r.ok) {
        await onRoomUpdated?.();
        await loadRoomEvents(rid);
        if (r.plan_rel) {
          void openPlanPreview(String(r.plan_rel));
        }
      }
    } finally {
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const [planPreview, setPlanPreview] = useState<{ open: boolean; title: string; content: string; loading: boolean }>({
    open: false,
    title: '',
    content: '',
    loading: false,
  });

  const openPlanPreview = async (planRel?: string) => {
    if (!worker) return;
    const targetRel = (planRel ?? selectedPlanRel ?? '').trim();
    if (!targetRel) {
      onToast?.('目前尚未選擇或生成任何 CE 計畫檔');
      return;
    }
    setPlanPreview({ open: true, title: targetRel, content: '', loading: true });
    try {
      const r = await teamApi.getRoomPlanContent(worker.id, targetRel);
      if (r?.ok) {
        setPlanPreview({ open: true, title: String(r.plan_rel || targetRel), content: String(r.content || ''), loading: false });
      } else {
        setPlanPreview({ open: true, title: targetRel, content: String(r?.text || '讀取計畫內容失敗'), loading: false });
      }
    } catch (e) {
      setPlanPreview({ open: true, title: targetRel, content: `讀取計畫發生錯誤：${String(e)}`, loading: false });
    }
  };

  const generateCePlanFromGoal = async () => {
    if (!worker) return;
    const effectiveGoal = (goalDraft || room?.goal || '').trim();
    if (!effectiveGoal) {
      onToast?.('請先填寫房間目標，才能根據目標生成計畫');
      return;
    }
    const rid = worker.id;
    setGeneratingPlanRoomId(rid);
    setRoomBusy(rid, true);
    setBusyStatus({ roomId: rid, text: '正在呼叫模型根據目標生成 CE 計畫與階段單元（約 10～30 秒），請稍候…' });
    onToast?.('⏳ 正在呼叫模型根據目標生成 CE 計畫，請稍候（約 10～30 秒）…');
    try {
      const r = await teamApi.generateRoomPlan(rid, effectiveGoal);
      if (r.progress) {
        setProgressInfo(r.progress as RoomProgressInfo);
      }
      if (r.plan_rel) {
        setSelectedPlanRel(String(r.plan_rel));
      }
      onToast?.(r.text || (r.ok ? '已根據目標生成並套用 CE 計畫' : '根據目標生成計畫失敗'));
      if (r.ok) {
        await onRoomUpdated?.();
        await loadRoomEvents(rid);
        if (r.plan_rel) {
          void openPlanPreview(String(r.plan_rel));
        }
      }
    } catch (e) {
      onToast?.(`根據目標生成計畫發生錯誤：${String(e)}`);
    } finally {
      setGeneratingPlanRoomId((cur) => (cur === rid ? '' : cur));
      setBusyStatus((cur) => (cur.roomId === rid ? { roomId: '', text: '' } : cur));
      setRoomBusy(rid, false);
    }
  };

  const handleResetRoom = async () => {
    if (!worker) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    try {
      const r = await teamApi.resetRoom(rid);
      setGoalDraft('');
      onToast?.(r.text || '已將專案恢復至 AI 工程助理介入前狀態');
      await onRoomUpdated?.();
      await loadRoomEvents(rid);
    } finally {
      setRoomBusy(rid, false);
    }
  };

  const handleHandoffNewRoom = async () => {
    if (!worker) return;
    const rid = worker.id;
    setRoomBusy(rid, true);
    try {
      const r = await teamApi.handoffRoom(rid, goalDraft.trim());
      onToast?.(r.text || '已封存舊房間並開啟新房間接續開發');
      await onRoomUpdated?.();
      if (r.room?.id) {
        onSelectRoom?.(r.room.id);
      }
    } finally {
      setRoomBusy(rid, false);
    }
  };

  const toggleDelegate = async (on: boolean) => {
    if (!worker) return;
    if (!room) {
      await onSetMode(worker.id, on ? 'auto' : 'suggest');
      return;
    }
    await runCtrlAction('切換代管模式失敗', async () => {
      const r = await teamApi.setRoomDelegate(worker.id, on);
      onToast?.(r.text || (on ? '已開啟 AI 高層主管代管' : '已改為交給我決定'));
      await onRoomUpdated?.();
    });
  };

  const togglePause = async () => {
    if (!worker) return;
    const canResume = room?.status === 'paused' || room?.status === 'error';
    await runCtrlAction(canResume ? '繼續／重試房間失敗' : '暫停房間失敗', async () => {
      const r = canResume ? await teamApi.resumeRoom(worker.id) : await teamApi.pauseRoom(worker.id);
      onToast?.(r.text || (canResume ? '已繼續房間' : '已暫停房間'));
      await onRoomUpdated?.();
      await loadRoomEvents(worker.id);
    });
  };

  const removeRoom = async () => {
    if (!worker) return;
    await runCtrlAction('封存房間失敗', async () => {
      const r = await teamApi.deleteRoom(worker.id, true);
      onToast?.(r.text || (r.ok ? '已關閉並封存房間（開發紀錄已存入 LLM Wiki）' : '封存失敗'));
      await onRoomUpdated?.();
      onClose?.();
    });
  };

  const answerInboxItem = async (itemId: string, optionId: string) => {
    if (!worker) return;
    await runCtrlAction('送出決定失敗', async () => {
      const customText = (inboxReply[itemId] || '').trim();
      const r = await teamApi.answerInbox(itemId, optionId, customText);
      onToast?.(r.text || '已送出決定');
      setInboxReply((prev) => ({ ...prev, [itemId]: '' }));
      await onRoomUpdated?.();
      await loadRoomEvents(worker.id);
    });
  };

  if (!worker) {
    return (
      <div className="h-full flex items-center justify-center p-8 text-base text-slate-500 text-center">
        點選左側辦公室房間卡片，即可在此切換 CLI／模型、查看 CE 計畫與 LLM Wiki 知識庫，或關閉封存／開新房接續開發。
      </div>
    );
  }

  const w = worker;
  const normalizedPhase = w.phase === 'spec' ? 'goal' : w.phase ?? 'goal';
  const phaseIdx = Math.max(0, PHASES.findIndex((p) => p.id === normalizedPhase));
  const actions = w.quickActions ?? [];
  const scene = room ? buildRoomScene(room, inboxItems) : null;
  const isArchivedOrDone = room?.status === 'archived' || room?.status === 'done';
  const ceUnits = room?.criteria ?? [];
  const wikiCount = wikiView?.pages?.length ?? 0;
  const descEvents = sortEventsDesc([...teamEvents, ...(room?.events ?? [])]);
  const activeCritIdx = findActiveCriterionIndex(ceUnits, room, descEvents);
  const activeCrit = activeCritIdx >= 0 ? ceUnits[activeCritIdx] : null;
  const completedCritCount = ceUnits.filter((u) => Boolean(u.done || u.status === 'verified')).length;

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    const msg = text.trim();
    if (!msg || busy || ctrlBusy) return;
    await runCtrlAction('送出指示失敗', async () => {
      if (room) {
        const r = (await teamApi.sendRoom(room.id, msg)) as {
          ok?: boolean;
          text?: string;
          goal_updated?: boolean;
          goal?: string;
          added_criterion_id?: string;
        };
        onToast?.(r.text || '已傳話給高層主管並交辦研發工程師');
        if (r.ok) {
          setText('');
          if (typeof r.goal === 'string') {
            setGoalDraft(r.goal);
          }
          await onRoomUpdated?.();
          await loadRoomEvents(room.id);
        }
      } else {
        await onAssist(w.id, '自訂協助指令', msg);
        setText('');
      }
    });
  };

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* 抬頭：名稱、房間控制列、階段 */}
      <div className="shrink-0 px-4 sm:px-5 pt-4 pb-3 border-b border-[#252a3a]">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <div className="text-[13px] text-slate-400 font-mono truncate">{room?.repo || w.appName}</div>
              {room && !isArchivedOrDone && (
                <button
                  type="button"
                  onClick={() => setFolderEditorOpen((v) => !v)}
                  className="px-2 py-0.5 rounded bg-[#1d2436] hover:bg-[#283149] border border-sky-500/30 text-sky-300 text-xs font-semibold cursor-pointer shrink-0"
                >
                  {folderEditorOpen ? '收起資料夾設定' : '📂 手動選擇目標／參考資料夾'}
                </button>
              )}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
              {roomSshHost && (
                <>
                  <span className="px-1.5 py-0.5 rounded bg-indigo-500/20 border border-indigo-400/40 text-indigo-200 font-semibold font-mono">
                    🌐 SSH 遠端：{roomSshHost}
                  </span>
                  {(sshAuthVerified || (!sshErrorHint && room?.status === 'running')) && (
                    <span className="px-1.5 py-0.5 rounded bg-emerald-500/20 border border-emerald-400/40 text-emerald-300 font-semibold">
                      ✅ SSH 連線成功{sshAuthKeyInstalled ? '（已安裝免密碼公鑰）' : ''}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => setSshAuthOpen((v) => !v)}
                    className="px-1.5 py-0.5 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-400/40 text-amber-200 font-semibold cursor-pointer"
                    title="若剛重啟服務或 SSH 密碼過期，點此重新驗證 SSH 密碼並自動嘗試安裝公鑰"
                  >
                    🔐 {showSshAuthBox ? '收起 SSH 密碼驗證' : sshAuthVerified ? '重設 SSH 密碼' : '輸入 SSH 密碼'}
                  </button>
                </>
              )}
              <span className="px-1.5 py-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 font-semibold">
                🎯 目標資料夾 B（可讀寫）：
                {room?.target_subdirs && room.target_subdirs.length > 0
                  ? room.target_subdirs.map((s) => `${s}/`).join(' + ')
                  : room?.target_subdir
                    ? `${room.target_subdir}/`
                    : room?.repo || w.name}
              </span>
              {room?.reference_root ? (
                <span className="px-1.5 py-0.5 rounded bg-amber-500/15 border border-amber-500/30 text-amber-200">
                  🔒 唯讀參考母資料夾 A：{room.reference_root}
                </span>
              ) : (
                room && (
                  <button
                    type="button"
                    onClick={() => {
                      setFolderEditorOpen(true);
                      void autoFillParentAsRef();
                    }}
                    className="px-1.5 py-0.5 rounded bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/30 text-amber-300 cursor-pointer"
                  >
                    ＋ 設定唯讀參考母資料夾 A
                  </button>
                )
              )}
              {room && (
                <button
                  type="button"
                  onClick={() => {
                    setTab('ce');
                    setProgressExpanded(true);
                  }}
                  className="px-1.5 py-0.5 rounded bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-sky-300 font-semibold cursor-pointer"
                  title="查看房間自動維護的 progress.md 斷點紀錄，任何時候中斷皆可無縫接續"
                >
                  📄 progress.md 已就緒
                </button>
              )}
            </div>
            {showSshAuthBox && (
              <div className="mt-2 p-2.5 rounded-lg bg-amber-500/10 border border-amber-400/40 space-y-1.5 text-xs">
                <div className="text-amber-200 font-semibold">
                  🔐 SSH 密碼驗證（{roomSshHost}）— 密碼僅暫存於後端記憶體，不會寫入磁碟
                </div>
                <div className="flex items-center gap-1.5">
                  <input
                    type="password"
                    autoComplete="off"
                    value={sshAuthPw}
                    onChange={(e) => setSshAuthPw(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        void submitSshAuth();
                      }
                    }}
                    placeholder={`輸入 ${roomSshHost} 的 SSH 密碼…`}
                    className="flex-1 px-2.5 py-1.5 rounded bg-[#141720] border border-amber-400/40 text-slate-100 placeholder:text-slate-500"
                  />
                  <button
                    type="button"
                    disabled={!sshAuthPw || sshAuthBusy}
                    onClick={() => void submitSshAuth()}
                    className="px-2.5 py-1.5 rounded bg-amber-400 hover:bg-amber-300 text-slate-950 font-bold cursor-pointer disabled:opacity-50 shrink-0"
                  >
                    {sshAuthBusy ? '驗證中…' : '🔓 驗證密碼'}
                  </button>
                </div>
              </div>
            )}

            {folderEditorOpen && room && (
              <div className="mt-2 p-2.5 rounded-xl bg-[#0e1017] border border-sky-500/40 space-y-2.5 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[13px] font-bold text-sky-300">📁 手動設定目標工作資料夾 B 與唯讀參考資料夾 A</span>
                  <button
                    type="button"
                    onClick={() => void autoFillParentAsRef()}
                    className="px-2 py-0.5 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/30 text-amber-200 text-xs font-semibold cursor-pointer"
                    title="自動將目前資料夾的上層母資料夾設為唯讀參考區 A，並限定只可修改目前子資料夾 B"
                  >
                    ⬆️ 自動將上層設為唯讀母資料夾 A
                  </button>
                </div>

                {/* 唯讀參考母資料夾 A */}
                <div className="space-y-1">
                  <label className="block text-xs font-semibold text-amber-300">
                    🔒 唯讀參考母資料夾 A（選填：設定後，除目標 B 以外的 C/D/E 兄弟資料夾皆為唯讀參考）
                  </label>
                  <div className="flex gap-1.5">
                    <input
                      type="text"
                      value={refRootDraft}
                      onChange={(e) => {
                        setRefRootDraft(e.target.value);
                        void loadRefSubdirs(e.target.value);
                      }}
                      placeholder="例如：D:\@BOF456 Proj\MMI_Kernel_Origin（留空代表不限制）"
                      className="flex-1 px-2 py-1 rounded bg-[#141824] border border-[#2a3147] text-[13px] text-slate-100 font-mono"
                    />
                    <button
                      type="button"
                      onClick={() => void openDirBrowser('ref', refRootDraft || repoDraft)}
                      className="px-2 py-1 rounded bg-[#23293b] hover:bg-[#2e354d] text-slate-200 text-xs font-semibold cursor-pointer shrink-0"
                    >
                      📂 瀏覽選取 A
                    </button>
                    {refRootDraft && (
                      <button
                        type="button"
                        onClick={() => {
                          setRefRootDraft('');
                          setTargetSubdirDraft('');
                          setRefSubdirs([]);
                        }}
                        className="px-2 py-1 rounded bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 text-xs cursor-pointer shrink-0"
                      >
                        清除
                      </button>
                    )}
                  </div>
                </div>

                {/* 若已指定母資料夾 A，提供下拉選單與多選按鈕直接選一或多個目標子資料夾 B */}
                {refRootDraft && refSubdirs.length > 0 && (
                  <div className="space-y-1.5">
                    <label className="block text-xs font-semibold text-emerald-300">
                      🎯 從母資料夾 A 底下選擇「可修改的目標子資料夾 B」（支援單選或複選多個，例如同時勾選前後端；其餘自動設為唯讀參考）：
                    </label>
                    <select
                      aria-label="選擇目標子資料夾 B"
                      value={targetSubdirDraft}
                      onChange={(e) => {
                        const sub = e.target.value;
                        setTargetSubdirDraft(sub);
                        setTargetSubdirsDraft(sub ? [sub] : []);
                        if (sub && refRootDraft) {
                          const sep = refRootDraft.includes('\\') ? '\\' : '/';
                          setRepoDraft(`${refRootDraft.replace(/[\\/]+$/, '')}${sep}${sub}`);
                        }
                      }}
                      className="w-full px-2 py-1.5 rounded bg-[#141824] border border-emerald-500/40 text-emerald-200 text-[13px]"
                    >
                      <option value="">（請選擇哪個子資料夾是可讀寫目標 B…）</option>
                      {refSubdirs.map((sub) => (
                        <option key={sub} value={sub}>
                          🎯 目標可修改 B：{sub}/ （其他兄弟資料夾唯讀）
                        </option>
                      ))}
                    </select>
                    <div className="space-y-1 pt-0.5">
                      <div className="text-xs text-slate-400">
                        或點擊下方複選多個目標資料夾 B（勾選 2 個以上時將以母資料夾 A 統一追蹤，並同時開放所選目標資料夾讀寫）：
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {refSubdirs.map((sub) => {
                          const activeList =
                            targetSubdirsDraft.length > 0
                              ? targetSubdirsDraft
                              : targetSubdirDraft
                                ? [targetSubdirDraft]
                                : [];
                          const checked = activeList.includes(sub);
                          return (
                            <button
                              key={sub}
                              type="button"
                              onClick={() => {
                                const next = checked
                                  ? activeList.filter((item) => item !== sub)
                                  : [...activeList, sub];
                                setTargetSubdirsDraft(next);
                                setTargetSubdirDraft(next[0] ?? '');
                                if (refRootDraft) {
                                  const sep = refRootDraft.includes('\\') ? '\\' : '/';
                                  if (next.length === 1) {
                                    setRepoDraft(`${refRootDraft.replace(/[\\/]+$/, '')}${sep}${next[0]}`);
                                  } else if (next.length > 1) {
                                    setRepoDraft(refRootDraft.replace(/[\\/]+$/, ''));
                                  }
                                }
                              }}
                              className={`px-2 py-0.5 rounded border text-xs font-mono cursor-pointer transition-colors ${
                                checked
                                  ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-200 font-semibold'
                                  : 'bg-[#141824] border-[#252a3a] text-slate-400 hover:text-slate-200'
                              }`}
                            >
                              {checked ? '☑ 🎯 ' : '☐ '}
                              {sub}/
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                )}

                {/* 手動新增／管理複數目標資料夾 B */}
                <div className="space-y-1">
                  <label className="block text-xs font-semibold text-emerald-300">
                    ➕ 手動指定複數個目標資料夾 B（可輸入子資料夾名稱或第二個專案完整路徑，例如前後端分離專案）：
                  </label>
                  <div className="flex gap-1.5">
                    <input
                      type="text"
                      value={extraTargetInput}
                      onChange={(e) => setExtraTargetInput(e.target.value)}
                      placeholder="輸入目標子資料夾名稱（如 frontend, backend）或完整路徑後按新增"
                      className="flex-1 px-2 py-1 rounded bg-[#141824] border border-[#2a3147] text-[13px] text-slate-100 font-mono"
                    />
                    <button
                      type="button"
                      onClick={() => {
                        const raw = extraTargetInput.trim();
                        if (!raw) return;
                        const parts = raw
                          .split(/[,;]+/)
                          .map((p) => p.trim())
                          .filter(Boolean);
                        setTargetSubdirsDraft((prev) => {
                          const base = prev.length > 0 ? prev : targetSubdirDraft ? [targetSubdirDraft] : [];
                          const merged = Array.from(new Set([...base, ...parts]));
                          setTargetSubdirDraft(merged[0] ?? '');
                          return merged;
                        });
                        setExtraTargetInput('');
                      }}
                      className="px-2.5 py-1 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-200 text-xs font-semibold cursor-pointer shrink-0"
                    >
                      ＋ 新增目標 B
                    </button>
                  </div>
                  {targetSubdirsDraft.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1 pt-1">
                      <span className="text-xs text-emerald-300 font-semibold">
                        目前設定的目標資料夾 B（共 {targetSubdirsDraft.length} 個）：
                      </span>
                      {targetSubdirsDraft.map((sub) => (
                        <span
                          key={sub}
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-emerald-500/15 border border-emerald-500/40 text-emerald-200 text-xs font-mono"
                        >
                          <span>🎯 {sub}/</span>
                          <button
                            type="button"
                            onClick={() => {
                              const next = targetSubdirsDraft.filter((item) => item !== sub);
                              setTargetSubdirsDraft(next);
                              setTargetSubdirDraft(next[0] ?? '');
                            }}
                            className="text-emerald-300 hover:text-rose-300 cursor-pointer"
                            title={`移除目標資料夾 ${sub}`}
                          >
                            ✕
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {/* 目標工作資料夾 B 完整路徑 */}
                <div className="space-y-1">
                  <label className="block text-xs font-semibold text-emerald-300">
                    🎯 目標工作資料夾 B 路徑（實際允許修改與建立分支的工作區）
                  </label>
                  <div className="flex gap-1.5">
                    <input
                      type="text"
                      value={repoDraft}
                      onChange={(e) => setRepoDraft(e.target.value)}
                      placeholder="例如：D:\@BOF456 Proj\MMI_Kernel_Origin\bof_knowledge_system"
                      className="flex-1 px-2 py-1 rounded bg-[#141824] border border-[#2a3147] text-[13px] text-slate-100 font-mono"
                    />
                    <button
                      type="button"
                      onClick={() => void openDirBrowser('repo', repoDraft || refRootDraft)}
                      className="px-2 py-1 rounded bg-[#23293b] hover:bg-[#2e354d] text-slate-200 text-xs font-semibold cursor-pointer shrink-0"
                    >
                      📂 瀏覽選取 B
                    </button>
                  </div>
                </div>

                {/* 內嵌資料夾瀏覽器 */}
                {browserTarget && browserData && (
                  <div className="p-2 rounded-lg bg-[#141824] border border-sky-500/40 space-y-1.5">
                    <div className="flex items-center justify-between gap-1 text-xs">
                      <span className="font-semibold text-sky-300 truncate">
                        瀏覽選取{browserTarget === 'ref' ? '「🔒 唯讀參考母資料夾 A」' : '「🎯 目標工作資料夾 B」'}：{browserData.path}
                      </span>
                      <button
                        type="button"
                        onClick={() => setBrowserTarget(null)}
                        className="text-slate-400 hover:text-slate-100 px-1 cursor-pointer"
                      >
                        ✕
                      </button>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {browserData.drives.map((drv) => (
                        <button
                          key={drv}
                          type="button"
                          onClick={() => void openDirBrowser(browserTarget, drv)}
                          className="px-1.5 py-0.5 rounded bg-[#1e2538] hover:bg-[#2a334d] text-xs font-mono text-slate-300 cursor-pointer"
                        >
                          {drv}
                        </button>
                      ))}
                      {browserData.parent && (
                        <button
                          type="button"
                          onClick={() => void openDirBrowser(browserTarget, browserData.parent)}
                          className="px-2 py-0.5 rounded bg-[#23293b] hover:bg-[#2e354d] text-xs text-amber-300 font-semibold cursor-pointer"
                        >
                          ⬆️ 上一層 ({browserData.parent})
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          if (browserTarget === 'ref') {
                            setRefRootDraft(browserData.path);
                            void loadRefSubdirs(browserData.path);
                          } else {
                            setRepoDraft(browserData.path);
                          }
                          setBrowserTarget(null);
                        }}
                        className="px-2 py-0.5 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-300 text-xs font-bold cursor-pointer"
                      >
                        ✓ 選取目前資料夾 ({browserData.path})
                      </button>
                    </div>
                    <div className="max-h-32 overflow-y-auto grid grid-cols-1 sm:grid-cols-2 gap-1 pt-1">
                      {browserData.dirs.map((d) => (
                        <div key={d.path} className="flex items-center justify-between gap-1 px-2 py-1 rounded bg-[#0e1017] border border-[#22283a] text-xs">
                          <button
                            type="button"
                            onClick={() => void openDirBrowser(browserTarget, d.path)}
                            className="text-left text-slate-200 hover:text-sky-300 truncate flex-1 cursor-pointer"
                            title={`進入 ${d.path}`}
                          >
                            📁 {d.name}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              if (browserTarget === 'ref') {
                                setRefRootDraft(d.path);
                                void loadRefSubdirs(d.path);
                              } else {
                                setRepoDraft(d.path);
                                if (refRootDraft && d.path.toLowerCase().startsWith(refRootDraft.toLowerCase())) {
                                  setTargetSubdirDraft(d.name);
                                }
                              }
                              setBrowserTarget(null);
                            }}
                            className="px-1.5 py-0.5 rounded bg-sky-500/20 hover:bg-sky-500/30 text-sky-300 font-semibold shrink-0 cursor-pointer"
                          >
                            選取
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div className="flex items-center justify-end gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => {
                      setFolderEditorOpen(false);
                      setBrowserTarget(null);
                    }}
                    className="px-2.5 py-1 rounded bg-[#23293b] hover:bg-[#2d354d] text-slate-300 text-[13px] cursor-pointer"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    disabled={ctrlBusy}
                    onClick={saveFolders}
                    className="px-3 py-1 rounded bg-emerald-400 hover:bg-emerald-300 text-slate-950 font-bold text-[13px] cursor-pointer disabled:opacity-50"
                  >
                    💾 儲存資料夾設定
                  </button>
                </div>
              </div>
            )}
            <h2 className="text-xl font-bold text-slate-50 truncate">{w.name}</h2>
            <div className={`text-sm mt-0.5 flex items-center gap-1 ${statusTone(w)}`}>
              {w.needsYou ? <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" /> : <CircleDot className="w-3.5 h-3.5" aria-hidden="true" />}
              <span>{w.statusLabel}</span>
              <span className="text-slate-600">· 更新於 {formatTime(w.lastHeartbeat)}</span>
            </div>
          </div>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              aria-label="關閉詳情"
              className="p-1.5 rounded text-slate-400 hover:text-white hover:bg-[#23293b] cursor-pointer lg:hidden"
            >
              <X className="w-5 h-5" aria-hidden="true" />
            </button>
          )}
        </div>

        {/* 房間決策模式與運作控制 */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <div className="flex-1 min-w-[180px] flex items-center gap-1 bg-[#0e1017] p-1 rounded-lg border border-[#232838]" role="radiogroup" aria-label="房間決策模式">
            <button
              type="button"
              role="radio"
              aria-checked={Boolean(room ? room.delegate : w.mode === 'auto')}
              disabled={ctrlBusy}
              onClick={() => toggleDelegate(true)}
              className={`flex-1 px-2 py-1.5 text-sm font-semibold rounded-md transition-colors cursor-pointer ${
                (room ? room.delegate : w.mode === 'auto')
                  ? 'bg-emerald-400 text-slate-950 font-bold'
                  : 'text-slate-400 hover:text-slate-100'
              }`}
              title="由高階主管自動審查規格與低風險權限"
            >
              🤖 AI 高層主管代管
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={!(room ? room.delegate : w.mode === 'auto')}
              disabled={ctrlBusy}
              onClick={() => toggleDelegate(false)}
              className={`flex-1 px-2 py-1.5 text-sm font-semibold rounded-md transition-colors cursor-pointer ${
                !(room ? room.delegate : w.mode === 'auto')
                  ? 'bg-sky-400 text-slate-950 font-bold'
                  : 'text-slate-400 hover:text-slate-100'
              }`}
              title="規格確認、權限與問題一律送收件匣由你決定"
            >
              👤 關鍵決策交給我
            </button>
          </div>

          {room && !isArchivedOrDone && (
            <button
              type="button"
              disabled={ctrlBusy}
              onClick={togglePause}
              className="px-2.5 py-2 rounded-lg bg-[#23293b] hover:bg-[#2e354d] text-sm font-semibold text-slate-100 flex items-center gap-1 cursor-pointer border border-[#323a52] disabled:opacity-50"
            >
              {room.status === 'paused' || room.status === 'error' ? (
                <Play className="w-3.5 h-3.5 text-emerald-400" />
              ) : (
                <Pause className="w-3.5 h-3.5 text-amber-300" />
              )}
              <span>{room.status === 'error' ? '重試' : room.status === 'paused' ? '繼續' : '暫停'}</span>
            </button>
          )}
        </div>

        {/* 階段（Compound Engineering 三階段） */}
        <ol className="mt-3 grid grid-cols-3 gap-1.5" aria-label="開發階段">
          {PHASES.map((p, i) => (
            <li key={p.id} title={p.hint} className="min-w-0">
              <div className={`h-1 rounded ${i < phaseIdx ? 'bg-emerald-400' : i === phaseIdx ? 'bg-amber-400' : 'bg-[#262c3d]'}`} />
              <div className={`mt-1 text-xs truncate ${i === phaseIdx ? 'text-amber-200 font-semibold' : 'text-slate-500'}`}>
                {p.step} {p.label}
              </div>
            </li>
          ))}
        </ol>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {/* 房間專屬收件匣項目（完整顯示 detail / error 與操作說明） */}
        {inboxItems.length > 0 ? (
          <section className="px-4 sm:px-5 py-3.5 border-b border-amber-500/30 bg-[#211b14] space-y-3">
            <h3 className="text-sm font-bold flex items-center gap-1.5 text-amber-300">
              <AlertTriangle className="w-4 h-4 text-amber-400" aria-hidden="true" />
              <span>需要你決定（{inboxItems.length} 件）</span>
            </h3>
            {inboxItems.map((item) => {
              const detailText = (item.detail || item.text || room?.error || w.pausedReason || '').trim();
              const copyPayload = detailText ? `${item.title}\n\n${detailText}` : item.title;
              const isCopied = copiedKey === `inbox-${item.id}`;
              const kindBadge =
                item.kind === 'error'
                  ? '❌ 錯誤中斷'
                  : item.kind === 'permission'
                    ? '🛡️ 權限確認'
                    : item.kind === 'question'
                      ? '❓ 開發問題確認'
                      : item.kind === 'merge'
                        ? '🔀 合併確認'
                        : '📋 待你裁示';
              const actionHint =
                item.kind === 'error'
                  ? '💡 說明：按「重試」會讓代理從剛才中斷的階段繼續執行；按「先放著」則維持暫停狀態。'
                  : '💡 說明：選擇下方按鈕即可讓代理繼續推進（亦可先在輸入框補充修改意見）。';
              return (
                <div key={item.id} className="rounded-xl bg-[#141722] border border-amber-400/40 p-3 space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-bold text-amber-200">{item.title}</span>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <button
                        type="button"
                        onClick={() => void handleCopy(`inbox-${item.id}`, detailText || copyPayload)}
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-semibold cursor-pointer transition-colors ${
                          isCopied
                            ? 'bg-emerald-500/20 border-emerald-400/50 text-emerald-300'
                            : 'bg-[#1c2233] hover:bg-[#283149] border-[#323d59] text-slate-200 hover:text-amber-300'
                        }`}
                        title="複製系統訊息或錯誤內容到剪貼簿，方便貼到其他地方除錯"
                      >
                        <Copy className="w-3 h-3" />
                        <span>{isCopied ? '已複製' : '複製訊息'}</span>
                      </button>
                      <span className="px-1.5 py-0.5 rounded bg-amber-500/20 border border-amber-400/30 text-xs font-semibold text-amber-200">
                        {kindBadge}
                      </span>
                    </div>
                  </div>
                  {detailText ? (
                    <pre className="text-[13px] leading-relaxed text-slate-100 bg-[#0e1017] border border-rose-500/30 rounded-md p-2.5 whitespace-pre-wrap break-words max-h-48 overflow-y-auto font-mono select-text">
                      {detailText}
                    </pre>
                  ) : (
                    <div className="text-[13px] text-slate-300 bg-[#0e1017] border border-[#252b3d] rounded-md p-2">
                      房間因等待裁示或異常而暫停，請選擇「重試」繼續或「先放著」維持暫停。
                    </div>
                  )}
                  <div className="text-xs text-amber-200/80 leading-relaxed">{actionHint}</div>
                  <input
                    type="text"
                    value={inboxReply[item.id] ?? ''}
                    onChange={(e) => setInboxReply((prev) => ({ ...prev, [item.id]: e.target.value }))}
                    placeholder="可選：補充修改意見或回答內容…"
                    className="w-full px-2.5 py-1.5 rounded bg-[#0e1017] border border-[#2a3147] text-sm text-slate-100 placeholder:text-slate-500"
                  />
                  <div className="flex flex-wrap gap-1.5">
                    {(item.options ?? []).map((opt) => (
                      <button
                        key={opt.id}
                        type="button"
                        disabled={ctrlBusy}
                        onClick={() => answerInboxItem(item.id, opt.id)}
                        className={`px-2.5 py-1.5 rounded-md text-sm font-semibold cursor-pointer disabled:opacity-50 ${actionTone(opt.label)}`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </section>
        ) : room?.error ? (
          <section className="px-4 sm:px-5 py-3 border-b border-rose-500/30 bg-rose-950/25 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm font-bold text-rose-300 flex items-center gap-1.5">
                <AlertTriangle className="w-4 h-4 text-rose-400" />
                <span>最近一次錯誤訊息</span>
              </div>
              <div className="flex items-center gap-1.5 shrink-0">
                {!isArchivedOrDone && (
                  <button
                    type="button"
                    disabled={ctrlBusy}
                    onClick={togglePause}
                    className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded bg-amber-400 hover:bg-amber-300 text-slate-950 text-xs font-bold cursor-pointer disabled:opacity-50"
                    title="從剛才中斷的階段重新嘗試繼續執行"
                  >
                    <Play className="w-3 h-3" />
                    <span>重試／繼續</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void handleCopy('room-error', room.error || '')}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-[#1c2233] hover:bg-[#283149] border border-[#323d59] text-xs font-semibold text-slate-200 hover:text-amber-300 cursor-pointer"
                  title="複製錯誤訊息"
                >
                  <Copy className="w-3 h-3" />
                  <span>{copiedKey === 'room-error' ? '已複製' : '複製訊息'}</span>
                </button>
              </div>
            </div>
            <pre className="text-[13px] leading-relaxed text-rose-100 bg-[#0e1017] border border-rose-500/30 rounded-md p-2.5 whitespace-pre-wrap break-words max-h-40 overflow-y-auto font-mono select-text">
              {room.error}
            </pre>
          </section>
        ) : (
          (w.helpPrompt || w.approvalCommand || actions.length > 0) && (
            <section className={`px-4 sm:px-5 py-4 border-b border-[#252a3a] ${w.needsYou ? 'bg-[#211b14]' : ''}`}>
              <div className="flex items-center justify-between gap-2 mb-2">
                <h3 className="text-sm font-bold flex items-center gap-1.5">
                  {w.needsYou ? (
                    <>
                      <AlertTriangle className="w-4 h-4 text-amber-400" aria-hidden="true" />
                      <span className="text-amber-300">需要你處理</span>
                    </>
                  ) : (
                    <>
                      <Sparkles className="w-3.5 h-3.5 text-slate-400" aria-hidden="true" />
                      <span className="text-slate-300">可以做的事</span>
                    </>
                  )}
                </h3>
                {w.helpPrompt && (
                  <button
                    type="button"
                    onClick={() => void handleCopy('help-prompt', w.helpPrompt || '')}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-[#1c2233] hover:bg-[#283149] border border-[#323d59] text-xs font-semibold text-slate-200 hover:text-amber-300 cursor-pointer"
                  >
                    <Copy className="w-3 h-3" />
                    <span>{copiedKey === 'help-prompt' ? '已複製' : '複製訊息'}</span>
                  </button>
                )}
              </div>
              {w.helpPrompt && (
                <p className="text-base text-amber-50/90 leading-relaxed mb-3 whitespace-pre-wrap break-words select-text">{w.helpPrompt}</p>
              )}
              <div className="flex flex-wrap gap-2">
                {actions.map((a) => (
                  <button
                    key={a}
                    type="button"
                    disabled={busy}
                    onClick={() => onAssist(w.id, a, '')}
                    className={`max-w-full px-3 py-1.5 rounded-md text-sm font-semibold transition-colors cursor-pointer disabled:opacity-50 text-left break-words ${actionTone(a)}`}
                  >
                    {a}
                  </button>
                ))}
              </div>
            </section>
          )
        )}

        {/* v2 LangGraph StateGraph 即時管線條 */}
        {room && (room.engine_version || 'v2') === 'v2' && (
          <div className="px-4 sm:px-5 py-2 bg-[#111521] border-b border-[#242a3d]">
            <div className="flex items-center justify-between gap-2 mb-1.5 flex-wrap">
              <span className="text-[11px] font-bold tracking-wide text-sky-300 flex items-center gap-1.5">
                <span>⚡ LangGraph v2 狀態機</span>
                {!generatingPlan && room.current_unit_id && (
                  <span className="px-1.5 py-0.5 rounded bg-amber-400/20 border border-amber-400/50 text-amber-300 font-mono text-[11px]">
                    鎖定單元 {room.current_unit_id}
                  </span>
                )}
              </span>
              <span className="text-[11px] text-slate-400">
                {room.task_mode === 'triage' ? '模式：D1 批次分診' : '模式：標準開發'} ·{' '}
                {room.d1_fast_review !== false ? 'D1 快速審查分流 ON' : '全量大模型審查'}
              </span>
            </div>
            <div className="flex items-center gap-1 overflow-x-auto pb-0.5">
              {(
                [
                  ['plan', 'Plan'],
                  ['implement', 'Implement'],
                  ['verify', 'Verify'],
                  ['d1_gate', 'D1 Gate'],
                  ['review', 'Review'],
                  ['commit', 'Commit'],
                  ['done', 'Done'],
                ] as const
              ).map(([nid, lbl], idx, arr) => {
                const effectiveNode = generatingPlan ? 'plan' : room.graph_node || '';
                const active = effectiveNode === nid;
                return (
                  <React.Fragment key={nid}>
                    <span
                      className={`px-2 py-0.5 rounded text-[11px] font-mono whitespace-nowrap border ${
                        active
                          ? 'bg-amber-400 text-slate-950 border-amber-300 font-bold shadow-sm'
                          : 'bg-[#181d2b] text-slate-400 border-[#283047]'
                      }`}
                    >
                      {lbl}
                    </span>
                    {idx < arr.length - 1 && <span className="text-slate-600 text-[10px]">→</span>}
                  </React.Fragment>
                );
              })}
            </div>
          </div>
        )}

        {/* 分頁（概況與模型配置 / CE 計畫與進度 / LLM Wiki 知識庫 / 事件紀錄） */}
        <div className="sticky top-0 z-10 bg-[#161923] px-4 sm:px-5 py-2.5 border-b border-[#252a3a]">
          <div className="flex gap-2 text-sm overflow-x-auto" role="tablist">
            {(
              [
                ['overview', '概況與模型配置'],
                ['ce', `CE 計畫與進度${ceUnits.length ? ` (${ceUnits.length})` : ''}`],
                ['wiki', `LLM Wiki (${wikiCount})`],
                ['log', '事件紀錄'],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={`px-3 py-1.5 rounded-lg border text-xs sm:text-sm transition-colors cursor-pointer whitespace-nowrap ${
                  tab === id
                    ? 'bg-amber-400/15 border-amber-400/70 text-amber-200 font-semibold shadow-sm'
                    : 'bg-[#1d2230] border-[#2d3548] text-slate-300 hover:bg-[#262d40] hover:border-slate-500 hover:text-slate-100'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          {activeBusyStatus && (
            <div
              role="status"
              aria-live="polite"
              className="mt-2 px-3 py-1.5 rounded-lg bg-sky-500/15 border border-sky-400/50 text-sky-200 text-xs font-semibold flex items-center gap-2"
            >
              <RefreshCw className="w-3.5 h-3.5 animate-spin shrink-0 text-sky-300" />
              <span className="truncate">{activeBusyStatus}</span>
            </div>
          )}
        </div>

        {tab === 'overview' && (
          <div className="px-4 sm:px-5 py-4 space-y-4">
            {/* 1. 置頂：目前進度與活動 + 目前正在執行的 CE 計畫單元 + 最新動態 (DESC 排序) */}
            <Field icon={<Eye className="w-3.5 h-3.5" aria-hidden="true" />} label="目前進度與活動（DESC 最新在上）">
              <div className="rounded-xl bg-[#0e1017] border border-[#242938] p-3 space-y-2.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="text-base font-semibold text-slate-100 leading-relaxed">{room?.activity || w.activity}</div>
                    {room && (
                      <div className="mt-1 flex flex-wrap gap-2 text-[13px] text-slate-400 font-mono">
                        <span>階段：{room.phase}</span>
                        <span>·</span>
                        <span>狀態：{room.status}</span>
                        {ceUnits.length > 0 && (
                          <>
                            <span>·</span>
                            <span className="text-amber-300">
                              CE 單元進度：{completedCritCount}/{ceUnits.length}
                            </span>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                  {ceUnits.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setTab('ce')}
                      className="px-2 py-1 rounded bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-sky-300 text-xs font-semibold shrink-0 cursor-pointer"
                    >
                      查看 CE 計畫明細 →
                    </button>
                  )}
                </div>

                {/* 明確標示目前正在做哪一個 CE 計畫單元 */}
                {activeCrit && (
                  <div className="rounded-lg bg-amber-500/10 border border-amber-400/40 px-2.5 py-2 space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-bold text-amber-300 flex items-center gap-1">
                        <span>⚡ 目前正在執行的 CE 計畫單元（第 {activeCritIdx + 1}/{ceUnits.length} 項）</span>
                      </span>
                      <button
                        type="button"
                        onClick={() => setTab('ce')}
                        className="text-xs text-amber-200 hover:underline cursor-pointer"
                      >
                        切換至計畫分頁
                      </button>
                    </div>
                    <div className="text-sm font-semibold text-slate-100 leading-snug">
                      {activeCrit.id && <span className="font-mono text-amber-300 mr-1.5">[{activeCrit.id}]</span>}
                      <span>{activeCrit.text}</span>
                    </div>
                    {room?.activity && room.status === 'running' && (
                      <div className="text-[13px] text-sky-300 font-mono truncate">
                        🛠️ 實作即時動作：{room.activity}
                      </div>
                    )}
                  </div>
                )}

                {descEvents.length > 0 && (
                  <div className="space-y-1.5 rounded-lg bg-[#090c12] border border-[#1f2535] p-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
                        <span>最新動態（DESC 最新在最上方）</span>
                        <span className="text-xs font-mono text-slate-500">共 {descEvents.length} 筆</span>
                      </div>
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() =>
                            void handleCopy(
                              'recent-events',
                              descEvents
                                .slice(0, 8)
                                .map((ev) => `${formatTime(ev.ts)} ${ev.text}`)
                                .join('\n')
                            )
                          }
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-[#181d2b] hover:bg-[#23293b] border border-[#2a3147] text-xs text-slate-300 hover:text-amber-300 cursor-pointer"
                          title="複製最新動態訊息"
                        >
                          <Copy className="w-2.5 h-2.5" />
                          <span>{copiedKey === 'recent-events' ? '已複製' : '複製動態'}</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => setTab('log')}
                          className="px-1.5 py-0.5 rounded bg-[#181d2b] hover:bg-[#23293b] border border-[#2a3147] text-xs text-sky-300 cursor-pointer"
                        >
                          全部紀錄
                        </button>
                      </div>
                    </div>
                    <div className="space-y-1 max-h-44 overflow-y-auto pr-0.5">
                      {descEvents.slice(0, 8).map((ev, idx) => (
                        <div
                          key={ev.id}
                          className={`group/ev text-[13px] flex items-start gap-2 rounded px-1.5 py-0.5 ${
                            idx === 0 ? 'bg-amber-500/10 text-amber-100 font-medium' : 'text-slate-300'
                          }`}
                        >
                          <span className="font-mono text-slate-500 shrink-0">{formatTime(ev.ts)}</span>
                          <span className="break-words flex-1 select-text">{ev.text}</span>
                          <button
                            type="button"
                            onClick={() => void handleCopy(`ev-${ev.id}`, `${formatTime(ev.ts)} ${ev.text}`)}
                            className="opacity-0 group-hover/ev:opacity-100 px-1 py-0.5 rounded text-xs text-slate-400 hover:text-amber-300 cursor-pointer shrink-0 transition-opacity"
                            title="複製此行訊息"
                          >
                            {copiedKey === `ev-${ev.id}` ? '✓' : '複製'}
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </Field>

            {/* 2. 房間內三位像素人（點擊任一角色展開最新 5 項工作紀錄，與左方像素畫布雙向同步） */}
            {scene && (
              <Field
                icon={<Users className="w-3.5 h-3.5" aria-hidden="true" />}
                label="房間內 Personas 分工（點擊像素人或下方卡片查看最新 5 項工作紀錄）"
              >
                <div className="space-y-2">
                  <div className="rounded-lg bg-[#101624] border border-sky-500/25 px-3 py-1.5 text-[13px] text-slate-300 leading-relaxed">
                    {(room?.engine_version || 'v2') === 'v2' ? (
                      <>
                        ⚡ <strong className="text-sky-300">v2 編排引擎已掛載：</strong>
                        <span className="text-amber-200">LangGraph</span> 循序鎖定單元 ＋{' '}
                        <span className="text-sky-200">Decision-1</span> 128 題閘門 ＋{' '}
                        <span className="text-emerald-300">LLM Wiki</span> 隨執行自動演化。
                      </>
                    ) : (
                      <>
                        🧩 <strong className="text-sky-300">Compound Engineering 全角色已掛載：</strong>
                        里程碑跟隨 <code className="text-amber-200">docs/plans/</code> 演進，知識自動編譯入 <strong className="text-emerald-300">LLM Wiki</strong>。
                      </>
                    )}
                  </div>
                  {scene.personas.map((p) => {
                    const isSelected = activePersonaSlot === p.slotKey;
                    const records = getPersonaRecentRecords(room, descEvents, p.slotKey, 5);
                    return (
                      <div
                        key={p.slotKey}
                        className={`rounded-xl border transition-colors ${
                          isSelected
                            ? 'bg-[#151c2e] border-amber-400/80 shadow-[0_0_0_1px_rgba(251,191,36,0.25)]'
                            : 'bg-[#0e1017] border-[#242938] hover:border-[#38415a]'
                        }`}
                      >
                        <div
                          role="button"
                          tabIndex={0}
                          onClick={() => setActivePersonaSlot(isSelected ? null : p.slotKey)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              setActivePersonaSlot(isSelected ? null : p.slotKey);
                            }
                          }}
                          className="px-3 py-2 flex items-center justify-between gap-2 text-sm cursor-pointer"
                        >
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <span
                                className="relative group/role inline-flex items-center gap-1.5 cursor-help"
                                title={getRoleTooltip(p.slotKey)}
                              >
                                <span
                                  className="px-1.5 py-0.2 rounded text-xs font-bold text-slate-950 shrink-0"
                                  style={{ backgroundColor: p.badgeColor }}
                                >
                                  {p.tierLabel}
                                </span>
                                <span className="font-semibold text-slate-100 truncate underline decoration-dotted decoration-slate-500 underline-offset-2">
                                  {p.roleTitle}
                                </span>
                                {getRoleTooltip(p.slotKey) && (
                                  <span
                                    role="tooltip"
                                    className="pointer-events-none opacity-0 group-hover/role:opacity-100 transition-opacity duration-150 absolute left-0 top-full mt-1.5 z-40 w-72 p-2.5 rounded-lg bg-[#0d1220] border border-sky-400/40 text-xs text-slate-200 leading-relaxed shadow-xl font-normal whitespace-normal"
                                  >
                                    {getRoleTooltip(p.slotKey)}
                                  </span>
                                )}
                              </span>
                              <span
                                className={`text-xs px-1.5 py-0.2 rounded font-semibold ${
                                  p.isWorking
                                    ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                                    : 'bg-slate-700/40 text-slate-400'
                                }`}
                              >
                                {p.isWorking ? '⚡ 工作中' : '☕ 待命區休息'}
                              </span>
                            </div>
                            <div className="text-[13px] text-slate-300 truncate mt-1">💬 {p.bubble}</div>
                          </div>
                          <div className="flex flex-col items-end gap-1 shrink-0">
                            <span className="text-[13px] font-mono text-sky-300">{p.engineLabel}</span>
                            <span className="text-xs text-amber-300/90 font-semibold">
                              {isSelected ? '▲ 收起紀錄' : `📋 最新 ${records.length} 筆紀錄`}
                            </span>
                          </div>
                        </div>

                        {isSelected && (
                          <div className="px-3 pb-2.5 pt-1.5 border-t border-[#242c42] bg-[#0a0d14]/90 rounded-b-xl space-y-1.5">
                            <div className="flex items-center justify-between gap-2">
                              <span className="text-xs font-bold text-amber-300">
                                📋 {p.roleTitle} 最新 5 項工作紀錄（DESC 最新在上）
                              </span>
                              <button
                                type="button"
                                onClick={() =>
                                  void handleCopy(
                                    `panel-persona-${p.slotKey}`,
                                    records
                                      .map((r, i) => `${i + 1}. ${r.ts ? formatTime(r.ts) + ' ' : ''}${r.text}`)
                                      .join('\n')
                                  )
                                }
                                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-[#181d2b] hover:bg-[#252d42] border border-[#2e3752] text-xs text-slate-200 hover:text-amber-300 cursor-pointer"
                              >
                                <Copy className="w-2.5 h-2.5" />
                                <span>{copiedKey === `panel-persona-${p.slotKey}` ? '已複製' : '複製 5 筆'}</span>
                              </button>
                            </div>
                            <ol className="space-y-1">
                              {records.map((rec, idx) => (
                                <li
                                  key={rec.id}
                                  className="text-[13px] leading-snug rounded bg-[#121723] border border-[#1f273b] px-2 py-1 flex items-start gap-1.5"
                                >
                                  <span
                                    className="font-mono text-xs font-bold shrink-0 mt-0.5"
                                    style={{ color: p.badgeColor }}
                                  >
                                    #{idx + 1}
                                  </span>
                                  {rec.ts && (
                                    <span className="font-mono text-xs text-slate-400 shrink-0 mt-0.5">
                                      {formatTime(rec.ts)}
                                    </span>
                                  )}
                                  <span className="text-slate-200 break-words flex-1 select-text">{rec.text}</span>
                                </li>
                              ))}
                            </ol>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </Field>
            )}

            {/* 3. 使用者自行寫入的目標編輯區 */}
            <Field icon={<Target className="w-3.5 h-3.5" aria-hidden="true" />} label="目標（由你自行寫入或跟隨 CE 計畫）">
              <div className="rounded-xl bg-[#0e1017] border border-[#242938] p-3 space-y-2">
                <textarea
                  rows={3}
                  value={goalDraft}
                  disabled={ctrlBusy || isArchivedOrDone}
                  onChange={(e) => setGoalDraft(e.target.value)}
                  placeholder="請在此自行輸入本房間要達成的目標（留空亦可，代理會直接依 docs/plans/ 的 CE 計畫執行）…"
                  className="w-full px-2.5 py-2 rounded-lg bg-[#141824] border border-[#2a3147] focus:border-amber-400 text-sm text-slate-100 placeholder:text-slate-500 leading-relaxed focus:outline-none"
                />
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap gap-1.5">
                    <button
                      type="button"
                      disabled={ctrlBusy || isArchivedOrDone}
                      onClick={() => saveGoal(goalDraft)}
                      className="px-2.5 py-1 rounded bg-amber-400 hover:bg-amber-300 text-slate-950 text-[13px] font-bold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                    >
                      <Save className="w-3 h-3" />
                      <span>儲存目標</span>
                    </button>
                    {goalDraft && (
                      <button
                        type="button"
                        disabled={ctrlBusy || isArchivedOrDone}
                        onClick={() => saveGoal('')}
                        className="px-2.5 py-1 rounded bg-[#23293b] hover:bg-[#2d354d] text-slate-300 text-[13px] cursor-pointer disabled:opacity-50"
                      >
                        清空
                      </button>
                    )}
                    {room && (
                      <button
                        type="button"
                        disabled={ctrlBusy || isArchivedOrDone}
                        onClick={() => setBrainstormOpen(true)}
                        className="px-2.5 py-1 rounded bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/35 text-emerald-300 text-[13px] font-medium flex items-center gap-1 cursor-pointer disabled:opacity-50"
                        title="開啟 Brainstorm 討論視窗，先釐清想法再一鍵生成 CE 計畫"
                      >
                        <Sparkles className="w-3 h-3" />
                        <span>Brainstorm</span>
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => setTab('ce')}
                      className="px-2.5 py-1 rounded bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-sky-300 text-[13px] font-medium cursor-pointer"
                      title="切換至「CE 計畫與進度」分頁以根據目標生成計畫或同步里程碑"
                    >
                      前往生成／管理計畫 →
                    </button>
                  </div>
                  {(w.core ?? 0) > 0 && (
                    <span className="text-[13px] font-mono tabular-nums text-emerald-300">
                      計畫進度 {progressPct(w)}%
                    </span>
                  )}
                </div>
              </div>
            </Field>

            {/* 4. 精簡 2 座位配置（研發實作・深度審查）＋ Decision-1 審查分流開關 */}
            {room && (
              <Field
                icon={<Cpu className="w-3.5 h-3.5" aria-hidden="true" />}
                label="大模型座位與 Decision-1 分流配置"
              >
                <div className="rounded-xl bg-[#0e1017] border border-[#242938] p-3 space-y-3 text-sm">
                  <div className="flex items-center justify-between gap-2 pb-1.5 border-b border-[#1d2230] flex-wrap">
                    <span className="text-[13px] text-slate-400">
                      決策閘門、失敗歸因與計畫體檢由 <strong className="text-sky-300">Decision-1（128 題打包）</strong> 自動執行；大模型只保留「研發實作」與「深度審查」2 個座位。
                    </span>
                    <button
                      type="button"
                      disabled={detectingModels || ctrlBusy}
                      onClick={() => handleDetectAllModels('all')}
                      className="px-2 py-1 rounded bg-[#1d2436] hover:bg-[#283149] text-sky-300 border border-sky-500/30 text-xs font-semibold flex items-center gap-1 shrink-0 cursor-pointer disabled:opacity-50"
                      title="重新向 Claude / Kiro / Antigravity 探測最新可用模型清單"
                    >
                      <RefreshCw className={`w-3 h-3 ${detectingModels ? 'animate-spin' : ''}`} />
                      <span>{detectingModels ? '偵測中…' : '重新偵測可用模型'}</span>
                    </button>
                  </div>

                  {/* Decision-1 快速審查分流開關 */}
                  <div className="flex items-center justify-between gap-2 px-2.5 py-2 rounded-lg bg-[#141926] border border-emerald-500/25 flex-wrap">
                    <div className="text-xs text-slate-300">
                      <span className="font-semibold text-emerald-300">Decision-1 15 題快速審查分流</span>
                      <span className="text-slate-400 ml-1.5">（低風險單元通過測試後直接 commit，高風險才叫深度審查）</span>
                    </div>
                    <button
                      type="button"
                      disabled={ctrlBusy || isArchivedOrDone}
                      onClick={() => void handleToggleD1FastReview(room.d1_fast_review === false)}
                      className={`px-2.5 py-1 rounded text-xs font-bold cursor-pointer disabled:opacity-50 ${
                        room.d1_fast_review !== false
                          ? 'bg-emerald-400 text-slate-950'
                          : 'bg-[#23293b] text-slate-300 border border-[#343d57]'
                      }`}
                    >
                      {room.d1_fast_review !== false ? '已開啟 ON' : '已關閉 OFF'}
                    </button>
                  </div>

                  {room.demo && (
                    <div className="px-2.5 py-1.5 rounded bg-purple-500/10 border border-purple-500/30 text-[13px] text-purple-200">
                      此為示範房間（使用內建假代理腳本），不需耗用真實模型額度。
                    </div>
                  )}

                  {ROLE_DEFINITIONS.filter((d) => d.id === 'impl' || d.id === 'review').map((roleDef) => {
                    const role = roleDef.id;
                    const seatVal = roleMap[role]?.seat || 'auto';
                    const modelVal = roleMap[role]?.model || '';
                    const baseChoices = filterCallableSeatChoices(effectiveModelsView);
                    const callableChoices = [
                      { id: 'auto', label: roleDef.autoSeatLabel },
                      { id: 'litellm', label: 'LiteLLM Proxy（線上掃描模型）' },
                      ...baseChoices.filter((c) => c.id !== 'auto'),
                    ];
                    const otherDevSeat =
                      role === 'impl' ? roleMap.review?.seat || 'auto' : role === 'review' ? roleMap.impl?.seat || 'auto' : '';
                    const opts = buildRoleModelDropdownOptions(
                      role,
                      seatVal,
                      modelVal,
                      effectiveModelsView,
                      catalogModels,
                      baseChoices,
                      otherDevSeat,
                    );
                    const seatInfo = seatVal !== 'auto' && seatVal !== 'litellm' ? effectiveModelsView?.seats?.[seatVal] : undefined;
                    const isDetecting = Boolean(seatInfo?.detecting);

                    return (
                      <div key={role} className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                        <div>
                          <label
                            htmlFor={`select-seat-${role}`}
                            title={roleDef.description}
                            className="relative group/role-label inline-flex items-center gap-1 text-xs font-semibold text-slate-300 mb-1 cursor-help"
                          >
                            <span className="underline decoration-dotted decoration-slate-500 underline-offset-2">
                              {roleDef.seatLabel}
                            </span>
                            <span className="text-[11px] text-sky-400/90" aria-hidden="true">ⓘ</span>
                            <span
                              role="tooltip"
                              className="pointer-events-none opacity-0 group-hover/role-label:opacity-100 transition-opacity duration-150 absolute left-0 top-full mt-1 z-40 w-72 p-2.5 rounded-lg bg-[#0d1220] border border-sky-400/40 text-xs text-slate-200 leading-relaxed shadow-xl font-normal whitespace-normal"
                            >
                              {roleDef.description}
                            </span>
                          </label>
                          <select
                            id={`select-seat-${role}`}
                            disabled={Boolean(room.demo) || isArchivedOrDone}
                            value={seatVal}
                            onChange={(e) => {
                              const nextSeat = e.target.value;
                              const defMod =
                                nextSeat === 'auto'
                                  ? ''
                                  : defaultModelForSeat(nextSeat, effectiveModelsView, catalogModels, role);
                              void changeModel(role, nextSeat, defMod);
                            }}
                            className="w-full px-2.5 py-1.5 rounded-lg bg-[#151926] border border-[#2c344a] focus:border-amber-400 text-slate-100 text-sm disabled:opacity-50"
                          >
                            {callableChoices.map((c) => (
                              <option key={c.id} value={c.id}>
                                {c.label}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div>
                          <label
                            htmlFor={`select-model-${role}`}
                            title={roleDef.description}
                            className="block text-xs font-semibold text-slate-300 mb-1"
                          >
                            {roleDef.modelLabel}
                            {isDetecting && <span className="ml-1 text-sky-400">（偵測中…）</span>}
                          </label>
                          <select
                            id={`select-model-${role}`}
                            disabled={Boolean(room.demo) || isArchivedOrDone}
                            value={seatVal === 'auto' ? '' : modelVal}
                            onChange={(e) => void changeModel(role, seatVal, e.target.value)}
                            className="w-full px-2.5 py-1.5 rounded-lg bg-[#151926] border border-[#2c344a] focus:border-amber-400 text-slate-100 text-sm disabled:opacity-50"
                          >
                            <option value="">
                              {seatVal === 'auto' ? roleDef.autoDefaultModelLabel : '（依工具預設模型）'}
                            </option>
                            {opts.map((m) => (
                              <option key={m.id} value={m.id}>
                                {m.label}
                              </option>
                            ))}
                          </select>
                        </div>
                      </div>
                    );
                  })}
                  <div className="flex items-center justify-between gap-2 pt-1 text-[12px] text-slate-400">
                    <span>
                      LiteLLM 線上模型目錄：{catalogModels.length} 個可用模型（每日自動掃描，價格單位 USD / 1M tokens）
                    </span>
                    <button
                      type="button"
                      disabled={scanningCatalog}
                      onClick={async () => {
                        setScanningCatalog(true);
                        try {
                          const res = await teamApi.scanModelCatalog();
                          const models = (res as { models?: typeof catalogModels }).models;
                          if (Array.isArray(models)) setCatalogModels(models);
                          onToast?.(`已掃描 LiteLLM 線上模型（共 ${models?.length ?? catalogModels.length} 個）`);
                        } finally {
                          setScanningCatalog(false);
                        }
                      }}
                      className="px-2 py-1 rounded bg-[#1d2436] hover:bg-[#283149] text-emerald-300 border border-emerald-500/30 text-xs font-semibold shrink-0 cursor-pointer disabled:opacity-50"
                    >
                      {scanningCatalog ? '掃描中…' : '立即掃描 LiteLLM 模型'}
                    </button>
                  </div>

                  {/* 房間封存、接續開新房與恢復初始狀態控制列 */}
                  <div className="pt-2 border-t border-[#1d2230] space-y-2">
                    <div className="text-xs text-slate-400">
                      房間管理（封存與開新房間時，過去所有開發紀錄與 Commits 皆會自動保存在 <strong className="text-emerald-300">LLM Wiki</strong> 中不流失）：
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      <button
                        type="button"
                        disabled={ctrlBusy}
                        onClick={handleHandoffNewRoom}
                        className="px-2.5 py-1.5 rounded bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 text-emerald-300 text-[13px] font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                        title="關閉並封存目前房間，同時以相同專案設定開啟新房間繼續開發，完整保留過去開發紀錄"
                      >
                        <FolderGit2 className="w-3.5 h-3.5" />
                        <span>封存並開新房間接續開發</span>
                      </button>
                      <button
                        type="button"
                        disabled={ctrlBusy}
                        onClick={removeRoom}
                        className="px-2.5 py-1.5 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/30 text-amber-300 text-[13px] font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                        title="關閉並封存此房間，將開發紀錄寫入 LLM Wiki"
                      >
                        <Archive className="w-3.5 h-3.5" />
                        <span>關閉並封存房間</span>
                      </button>
                      <button
                        type="button"
                        disabled={ctrlBusy}
                        onClick={handleResetRoom}
                        className="px-2.5 py-1.5 rounded bg-rose-500/15 hover:bg-rose-500/25 border border-rose-500/30 text-rose-300 text-[13px] font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                        title="移除 ai-team 分支與 worktree、清空自動填入的目標與舊驗收條件，恢復到介入前狀態"
                      >
                        <RotateCcw className="w-3.5 h-3.5" />
                        <span>恢復至介入前狀態</span>
                      </button>
                    </div>
                  </div>
                </div>
              </Field>
            )}

            {w.nextStep?.reason && (
              <Field icon={<Sparkles className="w-3.5 h-3.5" aria-hidden="true" />} label="助理的下一步">
                <p className="text-base text-slate-200">
                  <span className="text-amber-200">{ACTION_LABEL[w.nextStep.action] ?? w.nextStep.action}</span>
                  <span className="text-slate-400">：{w.nextStep.reason}</span>
                </p>
              </Field>
            )}

            {(w.commits?.length ?? 0) > 0 && (
              <Field icon={<GitCommit className="w-3.5 h-3.5" aria-hidden="true" />} label="里程碑 commit">
                <ul className="space-y-1">
                  {w.commits!.map((c) => (
                    <li key={c.hash} className="text-sm flex gap-2 min-w-0">
                      <span className="font-mono text-sky-300 shrink-0">{c.hash.slice(0, 7)}</span>
                      <span className="text-slate-300 truncate">{c.desc}</span>
                      <span className="ml-auto shrink-0 text-xs text-slate-500">{c.check}</span>
                    </li>
                  ))}
                </ul>
              </Field>
            )}

            {w.chatTitle && (
              <Field icon={<MessageSquare className="w-3.5 h-3.5" aria-hidden="true" />} label="綁定的對話">
                <div className="font-mono text-sm text-slate-300 bg-[#0e1017] border border-[#242938] rounded-md px-2.5 py-1.5 break-all">
                  {w.chatTitle}
                </div>
              </Field>
            )}
          </div>
        )}

        {/* CE 計畫與進度分頁：跟隨 docs/plans/*.md 的專案最終目的與階段性單元 */}
        {tab === 'ce' && (
          <div className="px-4 sm:px-5 py-4 space-y-4">
            <div className="rounded-xl bg-[#0e1017] border border-[#242938] p-3 space-y-2.5 text-sm">
              {/* 統一顯示目前房間唯一目標，避免與計畫檔案內文混淆 */}
              <div className="p-2.5 rounded-lg bg-[#121725] border border-amber-500/25 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-bold text-amber-300 flex items-center gap-1">
                    <Target className="w-3.5 h-3.5" />
                    <span>目前房間目標（生成計畫依據）</span>
                  </span>
                  <button
                    type="button"
                    onClick={() => setTab('overview')}
                    className="text-xs text-sky-300 hover:underline cursor-pointer"
                  >
                    修改目標 →
                  </button>
                </div>
                <p className="text-[13px] text-slate-200 leading-relaxed whitespace-pre-wrap break-words">
                  {(goalDraft || room?.goal || '').trim() || '（尚未設定房間目標，請先至「概況與模型配置」輸入目標）'}
                </p>
              </div>

              <div className="flex items-center justify-between gap-2 flex-wrap">
                <span className="font-semibold text-sky-300 flex items-center gap-1.5">
                  <FileText className="w-3.5 h-3.5" />
                  <span>Compound Engineering 計畫同步 (`docs/plans/`)</span>
                </span>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <button
                    type="button"
                    disabled={ctrlBusy || isArchivedOrDone || !(goalDraft || room?.goal || '').trim()}
                    onClick={() => void generateCePlanFromGoal()}
                    className="px-2.5 py-1 rounded bg-amber-500/25 hover:bg-amber-500/35 border border-amber-400/60 text-amber-100 text-[13px] font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                    title="直接根據上方目前房間目標生成 CE 計畫、寫入 docs/plans/ 並同步階段單元"
                  >
                    <Sparkles className={`w-3 h-3 ${generatingPlan ? 'animate-spin' : ''}`} />
                    <span>{generatingPlan ? '正在生成計畫中（約 10～30 秒）…' : '根據目標生成計畫'}</span>
                  </button>
                  <button
                    type="button"
                    disabled={ctrlBusy}
                    onClick={() => syncCePlan(selectedPlanRel, false)}
                    className="px-2.5 py-1 rounded bg-sky-500/20 hover:bg-sky-500/30 border border-sky-500/30 text-sky-200 text-[13px] font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                  >
                    <RefreshCw className="w-3 h-3" />
                    <span>跟隨計畫同步里程碑</span>
                  </button>
                  <button
                    type="button"
                    disabled={!selectedPlanRel && !(room?.selected_plan_rel || '')}
                    onClick={() => void openPlanPreview(selectedPlanRel || room?.selected_plan_rel || '')}
                    className="px-2.5 py-1 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/35 text-emerald-200 text-[13px] font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                    title="彈出視窗預覽目前選擇的 CE 計畫 Markdown 內容"
                  >
                    <FileText className="w-3 h-3" />
                    <span>預覽計畫 (.md)</span>
                  </button>
                  <button
                    type="button"
                    disabled={ctrlBusy || isArchivedOrDone}
                    onClick={() => void clearCePlanAndProgress()}
                    className="px-2.5 py-1 rounded bg-rose-500/15 hover:bg-rose-500/25 border border-rose-500/35 text-rose-200 text-[13px] font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                    title="清空此房間的 CE 計畫單元、重試紀錄與 progress.md（保留目標與分支）"
                  >
                    <Trash2 className="w-3 h-3" />
                    <span>清空計畫與進度</span>
                  </button>
                </div>
              </div>

              {generatingPlan && (
                <div className="px-3 py-2 rounded-lg bg-amber-500/15 border border-amber-400/50 text-amber-200 text-xs font-semibold flex items-center gap-2">
                  <RefreshCw className="w-3.5 h-3.5 animate-spin shrink-0" />
                  <span>正在呼叫模型依據上方房間目標生成計畫並同步階段單元，請稍候（約 10～30 秒）…</span>
                </div>
              )}

              {(ceInfo?.plans?.length ?? 0) > 0 ? (
                <div className="space-y-2">
                  <label htmlFor="select-ce-plan" className="block text-xs text-slate-400">
                    選擇要對齊的 CE 計畫檔案（支援總綱最終目的與階段子計畫）：
                  </label>
                  <select
                    id="select-ce-plan"
                    value={selectedPlanRel}
                    onChange={(e) => {
                      setSelectedPlanRel(e.target.value);
                      void syncCePlan(e.target.value, false);
                    }}
                    className="w-full px-2.5 py-1.5 rounded-lg bg-[#151926] border border-[#2c344a] text-slate-100 text-sm"
                  >
                    {ceInfo!.plans!.map((p) => (
                      <option key={p.rel_path} value={p.rel_path}>
                        {p.filename} — {p.title}
                      </option>
                    ))}
                  </select>
                  {(() => {
                    const picked =
                      ceInfo?.plans?.find((p) => p.rel_path === selectedPlanRel) || ceInfo?.active_plan;
                    const obj = picked?.objective || ceInfo?.active_plan?.objective || '';
                    if (!obj) return null;
                    return (
                      <div className="p-2.5 rounded-lg bg-[#141926] border border-[#26304a] space-y-1.5">
                        <div className="flex items-center justify-between gap-2 text-xs font-bold text-sky-300 flex-wrap">
                          <span>📄 計畫檔案記載目標（唯讀預覽）</span>
                          <div className="flex items-center gap-2">
                            {picked?.filename && (
                              <span className="font-mono font-normal text-[11px] text-slate-400 truncate max-w-[180px]">
                                來源：{picked.filename}
                              </span>
                            )}
                            <button
                              type="button"
                              disabled={ctrlBusy || isArchivedOrDone}
                              onClick={() => syncCePlan(selectedPlanRel, true)}
                              className="px-2 py-0.5 rounded bg-sky-500/20 hover:bg-sky-500/30 border border-sky-500/35 text-sky-200 text-[11px] font-semibold cursor-pointer disabled:opacity-50"
                            >
                              套用為房間目標
                            </button>
                          </div>
                        </div>
                        <p className="text-[13px] text-slate-300 leading-relaxed">{obj}</p>
                      </div>
                    );
                  })()}
                </div>
              ) : (
                <p className="text-[13px] text-slate-400">
                  目前專案尚未建立 `docs/plans/*.md`；開工後 Tech Lead 會以 `/ce-plan` 建立計畫並自動同步至此。
                </p>
              )}
            </div>

            {/* 目前正在執行的 CE 計畫單元焦點橫幅 */}
            {activeCrit && (
              <div className="rounded-xl bg-amber-500/15 border border-amber-400/50 p-3 space-y-1.5 text-sm shadow-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-bold text-amber-300 flex items-center gap-1.5">
                    <span>⚡ 目前正在執行的計畫單元（第 {activeCritIdx + 1}/{ceUnits.length} 項）</span>
                  </span>
                  <span className="px-2 py-0.5 rounded bg-amber-400 text-slate-950 font-bold text-xs">
                    進行中 · 已完成 {completedCritCount}/{ceUnits.length}
                  </span>
                </div>
                <div className="font-semibold text-slate-100 leading-relaxed">
                  {activeCrit.id && <span className="font-mono text-amber-300 mr-1.5">[{activeCrit.id}]</span>}
                  <span>{activeCrit.text}</span>
                </div>
                {room?.activity && (
                  <div className="text-[13px] font-mono text-sky-300 bg-[#0d121d] border border-sky-500/30 rounded px-2 py-1">
                    🛠️ 實作即時動作：{room.activity}
                  </div>
                )}
              </div>
            )}

            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-semibold text-slate-300">
                  階段實作單元與驗證條件（隨 CE 計畫動態變化）：
                </div>
                {ceUnits.length > 0 && (
                  <span className="text-[13px] font-mono text-emerald-300">
                    進度：{completedCritCount} / {ceUnits.length} 已完成
                  </span>
                )}
              </div>
              {ceUnits.length === 0 ? (
                <p className="text-sm text-slate-500">
                  尚未載入階段單元。可點上方「跟隨計畫同步里程碑」，或直接開工由 `/ce-plan` 自動產生。
                </p>
              ) : (
                <ul className="space-y-2">
                  {ceUnits.map((u, i) => {
                    const done = Boolean(u.done || u.status === 'verified');
                    const isCurrent = i === activeCritIdx && !done;
                    return (
                      <li
                        key={`${u.id || i}-${u.text}`}
                        className={`rounded-lg border p-2.5 flex gap-2 text-sm leading-relaxed transition-colors ${
                          isCurrent
                            ? 'bg-[#171d2f] border-amber-400/80 shadow-[0_0_0_1px_rgba(251,191,36,0.25)]'
                            : done
                              ? 'bg-[#0c1412] border-emerald-500/30'
                              : 'bg-[#0e1017] border-[#242938]'
                        }`}
                      >
                        {done ? (
                          <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                        ) : isCurrent ? (
                          <RefreshCw className="w-4 h-4 text-amber-400 shrink-0 mt-0.5 animate-spin" />
                        ) : (
                          <Circle className="w-4 h-4 text-slate-500 shrink-0 mt-0.5" />
                        )}
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center justify-between gap-2 flex-wrap">
                            <div className="font-semibold text-slate-100">
                              <span className="text-amber-300 font-mono mr-1.5">{u.id}</span>
                              <span>{u.text}</span>
                            </div>
                            {isCurrent && (
                              <span className="px-1.5 py-0.5 rounded bg-amber-400/20 border border-amber-400/50 text-amber-300 text-xs font-bold shrink-0">
                                ⚡ 目前執行中
                              </span>
                            )}
                            {done && (
                              <span className="px-1.5 py-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs font-semibold shrink-0">
                                ✓ 已完成
                              </span>
                            )}
                          </div>
                          {isCurrent && room?.activity && (
                            <div className="text-[13px] font-mono text-sky-300 mt-1">
                              👉 目前動作：{room.activity}
                            </div>
                          )}
                          {u.check && (
                            <div className="text-xs font-mono text-slate-400 mt-0.5 truncate">
                              驗證：{u.check}
                            </div>
                          )}
                          {/* v2 單一單元快捷操作列與 Artifact 檢視 */}
                          {u.id && !isArchivedOrDone && (
                            <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                              <button
                                type="button"
                                disabled={ctrlBusy}
                                onClick={() => void handleEditUnitCheck(u.id!, u.check || '')}
                                className="px-1.5 py-0.5 rounded bg-[#1a2030] hover:bg-[#252d42] border border-[#2d3650] text-[11px] text-slate-300 cursor-pointer disabled:opacity-50"
                              >
                                改驗證指令
                              </button>
                              <button
                                type="button"
                                disabled={ctrlBusy}
                                onClick={() => void handleRerunUnit(u.id!)}
                                className="px-1.5 py-0.5 rounded bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-[11px] text-sky-200 cursor-pointer disabled:opacity-50"
                              >
                                重跑此單元
                              </button>
                              {!done && (
                                <button
                                  type="button"
                                  disabled={ctrlBusy}
                                  onClick={() => void handleWaiveUnit(u.id!)}
                                  className="px-1.5 py-0.5 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/30 text-[11px] text-amber-200 cursor-pointer disabled:opacity-50"
                                >
                                  豁免跳過
                                </button>
                              )}
                              {room?.unit_artifacts?.[u.id] && (
                                <button
                                  type="button"
                                  onClick={() => setExpandedArtifactId((cur) => (cur === u.id ? '' : u.id!))}
                                  className="px-1.5 py-0.5 rounded bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 text-[11px] text-emerald-200 cursor-pointer"
                                >
                                  {expandedArtifactId === u.id ? '收起 Artifact' : '檢視 Artifact'}
                                </button>
                              )}
                            </div>
                          )}
                          {u.id && expandedArtifactId === u.id && room?.unit_artifacts?.[u.id] && (
                            <div className="mt-2 p-2 rounded-lg bg-[#090c12] border border-emerald-500/25 text-xs space-y-1 font-mono text-slate-300">
                              <div className="flex items-center justify-between gap-2 text-emerald-300">
                                <span>Commit: {room.unit_artifacts[u.id].commit_sha?.slice(0, 7) || '—'}</span>
                                <span>
                                  D1 Risk: {room.unit_artifacts[u.id].review?.d1_risk || '—'} ·{' '}
                                  {room.unit_artifacts[u.id].review?.deep_reviewed ? '已深度審查' : 'D1 快速放行'}
                                </span>
                              </div>
                              {room.unit_artifacts[u.id].verify?.tail_output && (
                                <pre className="p-1.5 rounded bg-[#0d111a] text-[11px] text-slate-300 whitespace-pre-wrap break-words max-h-36 overflow-y-auto">
                                  {room.unit_artifacts[u.id].verify?.tail_output}
                                </pre>
                              )}
                            </div>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>

            {/* 每個房間專屬的 progress.md 斷點續傳紀錄卡片 */}
            <div className="rounded-xl bg-[#0e1017] border border-emerald-500/35 p-3 space-y-2.5 text-sm">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="min-w-0">
                  <div className="font-bold text-emerald-300 flex items-center gap-1.5">
                    <FileText className="w-3.5 h-3.5 shrink-0" />
                    <span>房間斷點續傳紀錄 (`progress.md`)</span>
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">
                    每個回合、階段轉換、暫停或中斷時自動寫入最新進展；任何時候中斷重啟或換手皆可無縫接續開發。
                  </div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <button
                    type="button"
                    disabled={ctrlBusy}
                    onClick={refreshRoomProgress}
                    className="px-2 py-1 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/30 text-emerald-200 text-xs font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                  >
                    <RefreshCw className="w-3 h-3" />
                    <span>同步更新 progress.md</span>
                  </button>
                  {progressInfo?.markdown && (
                    <button
                      type="button"
                      onClick={() => void handleCopy('room-progress-md', progressInfo.markdown)}
                      className="px-2 py-1 rounded bg-[#181d2b] hover:bg-[#23293b] border border-[#2a3147] text-slate-200 hover:text-amber-300 text-xs font-semibold flex items-center gap-1 cursor-pointer"
                    >
                      <Copy className="w-3 h-3" />
                      <span>{copiedKey === 'room-progress-md' ? '已複製' : '複製 progress.md'}</span>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setProgressExpanded((v) => !v)}
                    className="px-2 py-1 rounded bg-[#181d2b] hover:bg-[#23293b] border border-[#2a3147] text-sky-300 text-xs font-semibold cursor-pointer"
                  >
                    {progressExpanded ? '收起內容' : '展開檢視'}
                  </button>
                </div>
              </div>
              {progressInfo?.notes_path && (
                <div className="text-xs font-mono text-slate-500 truncate">
                  工作文件路徑：{progressInfo.notes_path}
                </div>
              )}
              {progressExpanded && (
                <pre className="p-2.5 rounded-lg bg-[#090c12] border border-[#1f2535] text-[13px] font-mono text-slate-200 whitespace-pre-wrap break-words max-h-64 overflow-y-auto select-text leading-relaxed">
                  {progressInfo?.markdown || '（尚無 progress.md 紀錄，點選「同步更新 progress.md」即可立即產生）'}
                </pre>
              )}
            </div>
          </div>
        )}

        {/* LLM Wiki 知識庫分頁（取代舊版 LightRAG） */}
        {tab === 'wiki' && (
          <div className="px-4 sm:px-5 py-4 space-y-4">
            <div className="rounded-xl bg-[#0e1017] border border-emerald-500/30 p-3 space-y-2 text-sm">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <span className="font-bold text-emerald-300 flex items-center gap-1.5">
                  <BookOpen className="w-4 h-4" />
                  <span>專案 LLM Wiki 知識百科（雙向連結 Markdown）</span>
                </span>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <button
                    type="button"
                    disabled={ctrlBusy}
                    onClick={() => syncCePlan(selectedPlanRel, false)}
                    className="px-2 py-1 rounded bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 text-xs font-semibold cursor-pointer disabled:opacity-50"
                  >
                    同步 docs/solutions 與 plans
                  </button>
                  <button
                    type="button"
                    disabled={ctrlBusy}
                    onClick={() => void clearRoomWiki()}
                    className="px-2 py-1 rounded bg-rose-500/15 hover:bg-rose-500/25 border border-rose-500/35 text-rose-200 text-xs font-semibold flex items-center gap-1 cursor-pointer disabled:opacity-50"
                    title="清空此專案的 LLM Wiki 知識庫，一切重新累積"
                  >
                    <Trash2 className="w-3 h-3" />
                    <span>清空 LLM Wiki</span>
                  </button>
                </div>
              </div>
              <p className="text-[13px] text-slate-400 leading-relaxed">
                已全面改用 <strong className="text-slate-200">LLM Wiki 架構</strong> 取代舊版 LightRAG：所有房間的開發紀錄、審查教訓、房間封存交接 (`rooms`) 與 CE 解法 (`solutions`) 皆編譯為結構化 Markdown 條目與 `index.md`，新開房間會自動以 Wiki-First 檢索接續。
              </p>
            </div>

            {(wikiView?.pages?.length ?? 0) === 0 ? (
              <p className="text-sm text-slate-500">目前尚無 Wiki 條目，點選上方「同步 docs/solutions 與 plans」即可立即建立。</p>
            ) : (
              <div className="space-y-2">
                {wikiView!.pages!.slice(0, 30).map((p) => (
                  <div key={p.id} className="rounded-lg bg-[#0e1017] border border-[#242938] p-2.5 space-y-1 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[13px] text-emerald-300 font-semibold">[[{p.id}]]</span>
                      <span className="text-xs px-1.5 py-0.5 rounded bg-[#1d2436] text-sky-300">
                        {wikiView?.namespaces?.[p.namespace] || p.namespace}
                      </span>
                    </div>
                    <div className="font-semibold text-slate-100">{p.title}</div>
                    {p.summary && p.summary !== p.title && (
                      <div className="text-[13px] text-slate-400">{p.summary}</div>
                    )}
                    <div className="text-xs text-slate-500 font-mono flex flex-wrap gap-2">
                      {p.updated_at && <span>更新：{p.updated_at.slice(0, 16).replace('T', ' ')}</span>}
                      {p.rooms && p.rooms.length > 0 && <span>來源房間：{p.rooms.join(', ')}</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === 'log' && (
          <div className="px-4 sm:px-5 py-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[13px] text-slate-400">事件時間線（DESC 最新在最上方）</span>
              <div className="flex items-center gap-1.5">
                {descEvents.length > 0 && (
                  <button
                    type="button"
                    onClick={() =>
                      void handleCopy(
                        'all-events',
                        descEvents
                          .slice(0, 50)
                          .map((ev) =>
                            ev.detail
                              ? `${formatTime(ev.ts)} ${ev.text}\n${JSON.stringify(ev.detail, null, 2)}`
                              : `${formatTime(ev.ts)} ${ev.text}`
                          )
                          .join('\n\n')
                      )
                    }
                    className="inline-flex items-center gap-1 px-2 py-1 rounded bg-[#1c2233] hover:bg-[#283149] border border-[#323d59] text-[13px] text-slate-200 hover:text-amber-300 cursor-pointer"
                  >
                    <Copy className="w-3 h-3" />
                    <span>{copiedKey === 'all-events' ? '已複製全部' : '複製全部紀錄'}</span>
                  </button>
                )}
                <button
                  type="button"
                  disabled={loadingEvents}
                  onClick={() => loadRoomEvents()}
                  className="px-2.5 py-1 rounded bg-[#23293b] hover:bg-[#2f3750] text-[13px] text-slate-200 cursor-pointer disabled:opacity-50"
                >
                  {loadingEvents ? '載入中…' : '重新整理紀錄'}
                </button>
              </div>
            </div>
            {descEvents.length > 0 && (
              <ul className="space-y-2 border-b border-[#252a3a] pb-3">
                {descEvents.slice(0, 50).map((ev, idx) => {
                  const evCopyText = ev.detail
                    ? `${formatTime(ev.ts)} ${ev.text}\n${JSON.stringify(ev.detail, null, 2)}`
                    : `${formatTime(ev.ts)} ${ev.text}`;
                  return (
                    <li
                      key={ev.id}
                      className={`text-sm space-y-1 rounded-lg px-2 py-1.5 ${
                        idx === 0 ? 'bg-amber-500/10 border border-amber-400/30' : 'bg-[#0e1017]/60 border border-transparent'
                      }`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex items-start gap-2 min-w-0 flex-1">
                          <span className="font-mono text-[13px] text-slate-500 shrink-0">{formatTime(ev.ts)}</span>
                          <span className="text-slate-200 break-words select-text">{ev.text}</span>
                        </div>
                        <button
                          type="button"
                          onClick={() => void handleCopy(`log-ev-${ev.id}`, evCopyText)}
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-[#181d2b] hover:bg-[#23293b] border border-[#2a3147] text-xs text-slate-300 hover:text-amber-300 cursor-pointer shrink-0"
                          title="複製此筆事件紀錄"
                        >
                          <Copy className="w-2.5 h-2.5" />
                          <span>{copiedKey === `log-ev-${ev.id}` ? '已複製' : '複製'}</span>
                        </button>
                      </div>
                      {ev.detail && (
                        <pre className="text-xs font-mono bg-[#0e1017] border border-[#232838] rounded p-2 text-slate-300 whitespace-pre-wrap break-words overflow-y-auto max-h-32 select-text">
                          {JSON.stringify(ev.detail, null, 2)}
                        </pre>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            <ul className="space-y-2.5">
              {w.logs.length === 0 && descEvents.length === 0 && <li className="text-base text-slate-500">還沒有紀錄</li>}
              {[...w.logs].reverse().map((log) => (
                <li key={log.id} className="flex items-start gap-2.5 text-sm leading-relaxed">
                  <span className="font-mono tabular-nums text-[13px] text-slate-500 shrink-0 mt-0.5">{formatTime(log.timestamp)}</span>
                  <span className="text-slate-300">{log.message}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* 底部對話框 */}
      <form onSubmit={send} className="shrink-0 px-4 sm:px-5 py-3 border-t border-[#252a3a] bg-[#12151e] space-y-1.5">
        {inboxItems.length > 0 && (
          <div className="px-2.5 py-1.5 rounded-md bg-amber-500/15 border border-amber-400/40 text-xs text-amber-200 flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
            <span>
              上方有 <strong>{inboxItems.length} 件待你決定</strong>，請先點選上方決定按鈕讓任務繼續；下方輸入框用於追加目標或補充指示。
            </span>
          </div>
        )}
        <div className="flex items-center justify-between text-xs text-slate-400">
          <span className="flex items-center gap-1">
            <Bot className="w-3 h-3 text-amber-400" />
            <span>
              <strong className="text-amber-300">高層主管</strong>
              接收指示（自動判定並寫入目標）→ 交辦
              <strong className="text-sky-300 ml-0.5">研發工程師</strong>
              ＆同步
              <strong className="text-emerald-300 ml-0.5">審查工程師</strong>
            </span>
          </span>
        </div>
        <div className="flex gap-2">
          <label htmlFor="agent-message" className="sr-only">
            傳話給高層主管與研發工程師
          </label>
          <input
            id="agent-message"
            type="text"
            value={text}
            disabled={isArchivedOrDone}
            onChange={(e) => setText(e.target.value)}
            placeholder={
              isArchivedOrDone
                ? '房間已結束或封存'
                : '輸入給高層主管的新目標或指示（會自動寫入目標並同步三角色）…'
            }
            className="flex-1 min-w-0 bg-[#0e1017] border border-[#2a3044] focus:border-amber-400 rounded-md px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={!text.trim() || busy || ctrlBusy || isArchivedOrDone}
            aria-label="送出"
            className="px-3.5 py-2 rounded-md bg-amber-400 hover:bg-amber-300 disabled:bg-slate-700 disabled:text-slate-400 text-slate-950 text-sm font-bold flex items-center gap-1.5 cursor-pointer shrink-0"
          >
            <Send className="w-3.5 h-3.5" aria-hidden="true" />
            <span className="hidden sm:inline">送出指示</span>
          </button>
        </div>
      </form>
      {room && (
        <GoalBrainstormModal
          open={brainstormOpen}
          room={room}
          goalDraft={goalDraft}
          modelsView={effectiveModelsView ?? null}
          onClose={() => setBrainstormOpen(false)}
          onApplied={async (res) => {
            if (res.ce) setCeInfo(res.ce);
            if (res.plan_rel) setSelectedPlanRel(res.plan_rel);
            if (res.room?.goal !== undefined) setGoalDraft(res.room.goal);
            await onRoomUpdated?.();
          }}
          onToast={(msg) => onToast?.(msg)}
        />
      )}
      {planPreview.open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={() => setPlanPreview((p) => ({ ...p, open: false }))}
        >
          <div
            className="w-full max-w-3xl max-h-[85vh] rounded-xl bg-slate-900 border border-slate-700 shadow-2xl flex flex-col overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-4 py-3 border-b border-slate-700/80 flex items-center justify-between gap-2 bg-slate-800/70">
              <div className="flex items-center gap-2 min-w-0">
                <FileText className="w-4 h-4 text-emerald-400 shrink-0" />
                <span className="text-sm font-semibold text-slate-100 truncate">{planPreview.title}</span>
              </div>
              <button
                type="button"
                onClick={() => setPlanPreview((p) => ({ ...p, open: false }))}
                className="px-2.5 py-1 rounded bg-slate-700/70 hover:bg-slate-600 text-xs text-slate-200 cursor-pointer"
              >
                關閉
              </button>
            </div>
            <div className="p-4 overflow-y-auto text-sm text-slate-200 space-y-1.5 leading-relaxed">
              {planPreview.loading ? (
                <div className="text-slate-400 flex items-center gap-2">
                  <RefreshCw className="w-4 h-4 animate-spin" />
                  <span>正在讀取計畫內容…</span>
                </div>
              ) : (
                planPreview.content.split(/\r?\n/).map((line, i) => {
                  if (/^#{1,3}\s+/.test(line)) {
                    const level = (line.match(/^#+/)?.[0].length || 1);
                    const txt = line.replace(/^#+\s+/, '');
                    const cls = level === 1 ? 'text-lg font-bold text-amber-300 mt-3 mb-1' : level === 2 ? 'text-base font-bold text-sky-300 mt-3 mb-1 border-b border-slate-800 pb-0.5' : 'text-sm font-semibold text-emerald-300 mt-2';
                    return <div key={i} className={cls}>{txt}</div>;
                  }
                  if (/^\s*-\s+\[[ xX]\]\s+/.test(line)) {
                    const checked = /^\s*-\s+\[[xX]\]/.test(line);
                    const txt = line.replace(/^\s*-\s+\[[ xX]\]\s+/, '');
                    return (
                      <div key={i} className="flex items-start gap-2 pl-2">
                        <span className={checked ? 'text-emerald-400' : 'text-amber-300'}>{checked ? '☑' : '☐'}</span>
                        <span className={checked ? 'line-through text-slate-400' : 'text-slate-100'}>{txt}</span>
                      </div>
                    );
                  }
                  if (/^\s*-\s+/.test(line)) {
                    return <div key={i} className="pl-3 text-slate-200">• {line.replace(/^\s*-\s+/, '')}</div>;
                  }
                  if (!line.trim()) {
                    return <div key={i} className="h-1.5" />;
                  }
                  return <div key={i} className="text-slate-300 whitespace-pre-wrap break-words font-mono text-[13px]">{line}</div>;
                })
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[13px] font-medium text-slate-400 mb-1 flex items-center gap-1">
        {icon}
        {label}
      </div>
      {children}
    </div>
  );
}
