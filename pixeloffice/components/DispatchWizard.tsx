'use client';

import React, { useEffect, useRef, useState } from 'react';
import {
  FileText,
  FolderSearch,
  Globe,
  KeyRound,
  Monitor,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { teamApi } from '@/lib/api';
import type { SshHostInfo, SshProbeResult, TeamModelsView, TeamProject, TeamScanResult } from '@/lib/types';
import {
  ROLE_DEFINITIONS,
  type JobRoleKey,
  buildRoleModelDropdownOptions,
  defaultModelForSeat,
  filterCallableSeatChoices,
} from './SessionPanel';

interface DispatchWizardProps {
  open: boolean;
  onClose: () => void;
  onCreated: (roomId?: string) => Promise<void> | void;
  onToast: (msg: string) => void;
}

const DEFAULT_ROLE_CONFIG: Record<JobRoleKey, { seat: string; model: string }> = {
  supervisor: { seat: 'auto', model: '' },
  impl: { seat: 'auto', model: '' },
  review: { seat: 'auto', model: '' },
};

export default function DispatchWizard({ open, onClose, onCreated, onToast }: DispatchWizardProps) {
  const [projects, setProjects] = useState<TeamProject[]>([]);
  const [modelsView, setModelsView] = useState<TeamModelsView | null>(null);
  const [catalogModels, setCatalogModels] = useState<
    Array<{ id: string; label: string; input_usd_per_1m: number | null; output_usd_per_1m: number | null }>
  >([]);
  const [scanningCatalog, setScanningCatalog] = useState(false);

  // 執行環境與 SSH 遠端主機／密碼驗證狀態
  const [execMode, setExecMode] = useState<'local' | 'ssh'>('local');
  const [sshHosts, setSshHosts] = useState<SshHostInfo[]>([]);
  const [sshHost, setSshHost] = useState('');
  const [newSshTarget, setNewSshTarget] = useState('');
  const [newSshLabel, setNewSshLabel] = useState('');
  const [probingSsh, setProbingSsh] = useState(false);
  const [sshProbeInfo, setSshProbeInfo] = useState<SshProbeResult | null>(null);
  const [showPasswordBox, setShowPasswordBox] = useState(false);
  const [sshPassword, setSshPassword] = useState('');
  const [installSshKey, setInstallSshKey] = useState(true);

  // 資料夾邊界 A / B 與掃描狀態
  const [repo, setRepo] = useState('');
  const [referenceRoot, setReferenceRoot] = useState('');
  const [refSubdirs, setRefSubdirs] = useState<string[]>([]);
  const [showRefEditor, setShowRefEditor] = useState(false);
  const [extraTargetInput, setExtraTargetInput] = useState('');
  const [scan, setScan] = useState<TeamScanResult | null>(null);
  const [targetSubdir, setTargetSubdir] = useState('');
  const [targetSubdirs, setTargetSubdirs] = useState<string[]>([]);
  const [selectedDocRel, setSelectedDocRel] = useState('');
  const [goal, setGoal] = useState('');

  // 座位與 v2 引擎配置（實作／深度審查 + Decision-1 審查分流 + 任務模式）
  const [roleConfig, setRoleConfig] = useState<Record<JobRoleKey, { seat: string; model: string }>>(DEFAULT_ROLE_CONFIG);
  const [taskMode, setTaskMode] = useState<'standard' | 'triage'>('standard');
  const [d1FastReview, setD1FastReview] = useState(true);
  const [useWorktree, setUseWorktree] = useState(false);
  const [autonomy, setAutonomy] = useState('balanced');
  const [delegate, setDelegate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [browserTarget, setBrowserTarget] = useState<'repo' | 'ref'>('repo');
  const [dirBrowser, setDirBrowser] = useState<{
    path: string;
    parent: string;
    dirs: Array<{ name: string; path: string }>;
    drives: string[];
  } | null>(null);

  const prevFocusRef = useRef<HTMLElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const passwordInputRef = useRef<HTMLInputElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const activeSshHost = execMode === 'ssh' ? sshHost.trim() : '';

  const isAuthErrorText = (errText?: string, authFlag?: boolean) => {
    if (authFlag) return true;
    const low = (errText || '').toLowerCase();
    return (
      low.includes('permission denied') ||
      low.includes('password') ||
      low.includes('passphrase') ||
      low.includes('publickey') ||
      low.includes('authentication failed')
    );
  };

  const runHostProbe = async (targetHost: string, force = false, password = '', installKey = false) => {
    const clean = targetHost.trim();
    setProbingSsh(true);
    try {
      const probeRes = (await teamApi.probeSsh(clean, force, password, installKey)) as unknown as SshProbeResult;
      if (probeRes) {
        setSshProbeInfo(probeRes);
        if (!probeRes.ok && isAuthErrorText(probeRes.error || probeRes.text, probeRes.auth_required)) {
          setShowPasswordBox(true);
          setTimeout(() => passwordInputRef.current?.focus(), 50);
        } else if (probeRes.ok) {
          setShowPasswordBox(false);
          setSshPassword('');
          if (probeRes.key_installed) {
            onToast(probeRes.key_message || `已將本機 SSH 公鑰安裝至 ${clean}，日後免再輸入密碼`);
          }
        }
      }
      const mView = await teamApi.models(clean);
      if (mView) {
        setModelsView(mView);
      }
      if (clean) {
        const hRes = await teamApi.sshHosts();
        if (hRes?.hosts) setSshHosts(hRes.hosts);
      }
    } finally {
      setProbingSsh(false);
    }
  };

  const loadRefSubdirs = async (refPath: string, hostOverride?: string) => {
    const clean = refPath.trim();
    if (!clean) {
      setRefSubdirs([]);
      return;
    }
    const host = hostOverride !== undefined ? hostOverride : activeSshHost;
    const d = await teamApi.browseDirs(clean, host);
    if (d?.dirs) {
      setRefSubdirs(d.dirs.map((x) => x.name));
    }
  };

  const autoFillParentAsRef = async () => {
    const cur = (repo || '').trim().replace(/[\\/]+$/, '');
    const idx = Math.max(cur.lastIndexOf('\\'), cur.lastIndexOf('/'));
    if (idx <= 2) {
      onToast('請先輸入或選擇目標專案資料夾路徑，再將上層設為唯讀母資料夾 A');
      return;
    }
    const parent = cur.slice(0, idx);
    const sub = cur.slice(idx + 1);
    setReferenceRoot(parent);
    setShowRefEditor(true);
    setTargetSubdir(sub);
    setTargetSubdirs((prev) => (prev.length > 0 ? prev : sub ? [sub] : []));
    await loadRefSubdirs(parent);
    onToast(`已將上層 ${parent} 設為唯讀參考母資料夾 A，並鎖定目標子資料夾 B：${sub}/`);
  };

  const doScan = async (targetPath?: string, overrideSshHost?: string) => {
    const p = String(targetPath ?? repo ?? '').trim();
    if (!p) return;
    const host = overrideSshHost !== undefined ? overrideSshHost : activeSshHost;
    const res = await teamApi.scan(p, host);
    if (res) {
      setScan(res);
      const availSubs = res.subdirs ?? [];
      setTargetSubdir((prev) => (availSubs.includes(prev) ? prev : prev));
      if (res.docs && res.docs.length > 0) {
        setSelectedDocRel(res.docs[0].relpath);
      } else {
        setSelectedDocRel('');
      }
      if (!targetPath) onToast(`已掃描 ${res.name || p}`);
    } else if (!targetPath) {
      onToast(host ? `遠端 ${host} 找不到該專案資料夾或無法掃描（若需密碼請先完成上方 SSH 密碼驗證）` : '找不到該專案資料夾或無法掃描');
    }
  };

  useEffect(() => {
    if (open) {
      prevFocusRef.current = document.activeElement as HTMLElement | null;
      const focusTimer = setTimeout(() => inputRef.current?.focus(), 0);
      teamApi.projects().then((res) => {
        const list = (res?.projects ?? [])
          .map((p) => ({ name: p.name || p.repo || p.path || '', repo: p.repo || p.path || '', path: p.path || p.repo || '' }))
          .filter((p) => Boolean(p.repo));
        setProjects(list);
      });
      teamApi.sshHosts().then((res) => {
        const list = res?.hosts ?? [];
        setSshHosts(list);
        if (!sshHost && list.length > 0) {
          setSshHost(list[0].target);
        }
      });
      teamApi.models(activeSshHost).then(setModelsView);
      teamApi
        .getModelCatalog()
        .then((res) => {
          if (res?.models) setCatalogModels(res.models);
        })
        .catch(() => {});
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onCloseRef.current();
        }
      };
      window.addEventListener('keydown', onKey);
      return () => {
        clearTimeout(focusTimer);
        window.removeEventListener('keydown', onKey);
      };
    } else if (prevFocusRef.current) {
      prevFocusRef.current.focus();
      prevFocusRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const callableChoices = filterCallableSeatChoices(modelsView);

  // 當切換環境或遠端偵測結果更新時，若某角色原本選了不可調用的 CLI，自動退回 auto 預設
  useEffect(() => {
    const allowedIds = new Set([...callableChoices.map((c) => c.id), 'litellm']);
    setRoleConfig((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const rKey of Object.keys(next) as JobRoleKey[]) {
        const curSeat = next[rKey].seat;
        if (curSeat !== 'auto' && !allowedIds.has(curSeat)) {
          next[rKey] = { seat: 'auto', model: '' };
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [callableChoices]);

  if (!open) return null;

  const updateRoleSeatOrModel = (role: JobRoleKey, rawSeat: string, rawModel: string) => {
    let nextSeat = rawSeat;
    let nextModel = rawModel;
    if (rawModel.startsWith('@')) {
      const colonIdx = rawModel.indexOf(':');
      if (colonIdx > 1) {
        nextSeat = rawModel.slice(1, colonIdx);
        nextModel = rawModel.slice(colonIdx + 1);
      }
    } else if (nextSeat !== 'auto' && !nextModel) {
      nextModel = defaultModelForSeat(nextSeat, modelsView, catalogModels, role);
    }

    setRoleConfig((prev) => {
      const next = {
        ...prev,
        [role]: { seat: nextSeat, model: nextSeat === 'auto' ? '' : nextModel },
      };
      // 實作與審查必須使用不同 CLI 座位（若皆為具體 CLI 座位）
      if ((role === 'impl' || role === 'review') && nextSeat !== 'auto' && nextSeat !== 'litellm') {
        const otherRole: JobRoleKey = role === 'impl' ? 'review' : 'impl';
        if (next[otherRole].seat === nextSeat) {
          next[otherRole] = { seat: 'auto', model: '' };
        }
      }
      return next;
    });
  };

  const detectWizardModels = async () => {
    if (execMode === 'ssh') {
      if (!activeSshHost) {
        onToast('請先選擇或新增 SSH 遠端主機');
        return;
      }
      await runHostProbe(activeSshHost, true);
      onToast(`已完成遠端 ${activeSshHost} CLI 握手偵測`);
      return;
    }
    const res = await teamApi.detectModels('all');
    onToast(res.text || '已開始偵測所有 CLI 可用模型');
    setTimeout(() => teamApi.models('').then(setModelsView), 1000);
    setTimeout(() => teamApi.models('').then(setModelsView), 2500);
  };

  const handleAddSshHost = async () => {
    const target = newSshTarget.trim();
    if (!target) return;
    const res = await teamApi.addSshHost(target, newSshLabel.trim());
    if (!res.ok) {
      onToast(res.text || '新增 SSH 主機失敗');
      return;
    }
    const hostsList = (res.hosts as SshHostInfo[] | undefined) ?? (await teamApi.sshHosts())?.hosts ?? [];
    setSshHosts(hostsList);
    setSshHost(target);
    setNewSshTarget('');
    setNewSshLabel('');
    onToast(res.text || `已新增 SSH 主機 ${target}，正在偵測遠端 CLI…`);
    await runHostProbe(target, true);
  };

  const handleRemoveSshHost = async (target: string) => {
    const res = await teamApi.removeSshHost(target);
    if (!res.ok) {
      onToast(res.text || '移除 SSH 主機失敗');
      return;
    }
    const hostsList = (res.hosts as SshHostInfo[] | undefined) ?? (await teamApi.sshHosts())?.hosts ?? [];
    setSshHosts(hostsList);
    const nextTarget = hostsList[0]?.target ?? '';
    setSshHost(nextTarget);
    setSshProbeInfo(null);
    setShowPasswordBox(false);
    if (nextTarget) {
      await runHostProbe(nextTarget, false);
    } else {
      setModelsView({ ssh_host: '', available_seats: [], seats: {} });
    }
    onToast(res.text || `已移除 ${target}`);
  };

  const handlePasswordProbe = async () => {
    if (!activeSshHost) return;
    if (!sshPassword) {
      onToast('請輸入 SSH 登入密碼');
      passwordInputRef.current?.focus();
      return;
    }
    await runHostProbe(activeSshHost, true, sshPassword, installSshKey);
  };

  const combinedSubdirChoices = Array.from(
    new Set([...(scan?.subdirs ?? []), ...refSubdirs, ...targetSubdirs, ...(targetSubdir ? [targetSubdir] : [])]),
  );

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const repoClean = String(repo ?? '').trim();
    const refClean = String(referenceRoot ?? '').trim();
    const goalClean = String(goal ?? '').trim();
    const subClean = String(targetSubdir ?? '').trim();
    const effectiveSubdirs =
      targetSubdirs.length > 0 ? targetSubdirs : subClean ? [subClean] : [];
    if (!repoClean || !goalClean) return;
    if (execMode === 'ssh' && !activeSshHost) {
      onToast('請先選擇或新增 SSH 遠端主機');
      return;
    }
    setBusy(true);
    try {
      const supCfg = roleConfig.supervisor || { seat: 'auto', model: '' };
      const implCfg = roleConfig.impl || { seat: 'auto', model: '' };
      const revCfg = roleConfig.review || { seat: 'auto', model: '' };
      const rolePrefsPayload: Record<string, { seat: string; model: string }> = {};
      for (const rDef of ROLE_DEFINITIONS) {
        const cfg = roleConfig[rDef.id];
        if (cfg && cfg.seat !== 'auto') {
          rolePrefsPayload[rDef.id] = { seat: cfg.seat, model: cfg.model };
        }
      }

      const res = await teamApi.createRoom({
        repo: repoClean,
        ssh_host: activeSshHost || undefined,
        reference_root: refClean || undefined,
        target_subdir: effectiveSubdirs[0] || subClean || undefined,
        target_subdirs: effectiveSubdirs.length > 0 ? effectiveSubdirs : undefined,
        goal: goalClean,
        source: 'goal',
        autonomy,
        impl_cli: implCfg.seat || 'auto',
        impl_model: implCfg.seat === 'auto' ? '' : implCfg.model,
        review_cli: revCfg.seat || 'auto',
        review_model: revCfg.seat === 'auto' ? '' : revCfg.model,
        supervisor_cli: supCfg.seat || 'auto',
        supervisor_model: supCfg.seat === 'auto' ? '' : supCfg.model,
        role_prefs: Object.keys(rolePrefsPayload).length > 0 ? rolePrefsPayload : undefined,
        task_mode: taskMode,
        d1_fast_review: d1FastReview,
        use_worktree: useWorktree,
        engine_version: 'v2',
      });
      if (res.ok) {
        if (res.room?.id) {
          await teamApi.setRoomDelegate(res.room.id, delegate);
        }
        onToast('已建立房間並開始派工');
        setGoal('');
        await onCreated(res.room?.id);
        onClose();
      } else {
        onToast(res.text || '建立房間失敗');
      }
    } finally {
      setBusy(false);
    }
  };

  const needsPasswordBanner =
    execMode === 'ssh' &&
    activeSshHost &&
    (showPasswordBox ||
      Boolean(sshProbeInfo && !sshProbeInfo.ok && isAuthErrorText(sshProbeInfo.error || sshProbeInfo.text, sshProbeInfo.auth_required)));

  return (
    <div
      className="fixed inset-0 z-40 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="派工精靈"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <form
        onSubmit={submit}
        className="w-full max-w-2xl rounded-2xl bg-[#141720] border border-[#282e42] shadow-2xl overflow-hidden"
      >
        <div className="px-5 py-3.5 border-b border-[#252a3a] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-emerald-400" aria-hidden="true" />
            <h2 className="text-sm font-bold text-slate-100">新增工作房間（派工精靈）</h2>
            <span className="px-2 py-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 text-[10px] font-semibold text-emerald-300">
              Decision-1 全能力派工
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-100 hover:bg-[#23293b] cursor-pointer"
            aria-label="關閉派工精靈"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4 text-xs max-h-[78vh] overflow-y-auto">
          {/* 1. 執行環境切換：本機開發 vs SSH 遠端開發（含密碼驗證與公鑰安裝） */}
          <div className="space-y-2 p-3 rounded-xl bg-[#0e1017] border border-[#252a3a]">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold text-slate-200">執行環境與目標機器</span>
              <div className="flex items-center gap-1 bg-[#141824] p-0.5 rounded-lg border border-[#282e42]">
                <button
                  type="button"
                  onClick={() => {
                    setExecMode('local');
                    setDirBrowser(null);
                    setScan(null);
                    setSshProbeInfo(null);
                    setShowPasswordBox(false);
                    void teamApi.models('').then(setModelsView);
                  }}
                  className={`px-2.5 py-1 rounded-md text-[11px] font-semibold flex items-center gap-1 cursor-pointer transition-colors ${
                    execMode === 'local'
                      ? 'bg-emerald-400 text-slate-950 font-bold'
                      : 'text-slate-400 hover:text-slate-100'
                  }`}
                >
                  <Monitor className="w-3 h-3" />
                  <span>本機開發</span>
                </button>
                <button
                  type="button"
                  onClick={async () => {
                    setExecMode('ssh');
                    setDirBrowser(null);
                    setScan(null);
                    const target = sshHost || sshHosts[0]?.target || '';
                    if (target) {
                      setSshHost(target);
                      await runHostProbe(target, false);
                    } else {
                      setModelsView({ ssh_host: '', available_seats: [], seats: {} });
                    }
                  }}
                  className={`px-2.5 py-1 rounded-md text-[11px] font-semibold flex items-center gap-1 cursor-pointer transition-colors ${
                    execMode === 'ssh'
                      ? 'bg-indigo-400 text-slate-950 font-bold'
                      : 'text-slate-400 hover:text-slate-100'
                  }`}
                >
                  <Globe className="w-3 h-3" />
                  <span>SSH 遠端開發</span>
                </button>
              </div>
            </div>

            {execMode === 'ssh' && (
              <div className="space-y-2 pt-1.5 border-t border-[#1e2436]">
                <div className="flex items-center gap-1.5">
                  <select
                    aria-label="SSH 遠端主機"
                    value={sshHost}
                    onChange={async (e) => {
                      const next = e.target.value;
                      setSshHost(next);
                      setDirBrowser(null);
                      setScan(null);
                      setShowPasswordBox(false);
                      if (next) {
                        await runHostProbe(next, false);
                      } else {
                        setSshProbeInfo(null);
                        setModelsView({ ssh_host: '', available_seats: [], seats: {} });
                      }
                    }}
                    className="flex-1 px-2.5 py-1.5 rounded-lg bg-[#141720] border border-[#2c344a] text-slate-100 font-mono text-xs"
                  >
                    <option value="">（請選擇 ~/.ssh/config 主機或新增自訂 SSH 主機）</option>
                    {sshHosts.map((h) => (
                      <option key={h.target} value={h.target}>
                        {h.label !== h.target ? `${h.label} (${h.target})` : h.target}
                        {h.source === 'ssh_config' ? ' [~/.ssh/config]' : ' [自訂]'}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    disabled={!activeSshHost || probingSsh}
                    onClick={() => void runHostProbe(activeSshHost, true)}
                    className="px-2.5 py-1.5 rounded-lg bg-[#1d2436] hover:bg-[#283149] border border-sky-500/30 text-sky-300 text-[11px] font-semibold flex items-center gap-1 cursor-pointer shrink-0 disabled:opacity-50"
                    title="透過 SSH 執行嚴格握手偵測遠端可調用的 claude / agy / kiro-cli"
                  >
                    <RefreshCw className={`w-3 h-3 ${probingSsh ? 'animate-spin' : ''}`} />
                    <span>{probingSsh ? '偵測中…' : '偵測遠端 CLI'}</span>
                  </button>
                  <button
                    type="button"
                    disabled={!activeSshHost || probingSsh}
                    onClick={() => {
                      setShowPasswordBox((v) => !v);
                      setTimeout(() => passwordInputRef.current?.focus(), 50);
                    }}
                    className={`px-2 py-1.5 rounded-lg border text-[11px] font-semibold flex items-center gap-1 cursor-pointer shrink-0 disabled:opacity-50 ${
                      needsPasswordBanner
                        ? 'bg-amber-400 text-slate-950 border-amber-300 font-bold'
                        : 'bg-[#1d2436] hover:bg-[#283149] border-amber-500/30 text-amber-300'
                    }`}
                    title="輸入或更新遠端 SSH 主機登入密碼"
                  >
                    <KeyRound className="w-3 h-3" />
                    <span>輸入密碼</span>
                  </button>
                  {activeSshHost && sshHosts.find((h) => h.target === activeSshHost)?.source === 'custom' && (
                    <button
                      type="button"
                      onClick={() => void handleRemoveSshHost(activeSshHost)}
                      className="p-1.5 rounded-lg bg-rose-500/15 hover:bg-rose-500/25 border border-rose-500/30 text-rose-300 cursor-pointer shrink-0"
                      title={`移除自訂 SSH 主機 ${activeSshHost}`}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>

                {/* 新增自訂 user@host[:port] */}
                <div className="flex items-center gap-1.5">
                  <input
                    type="text"
                    value={newSshTarget}
                    onChange={(e) => setNewSshTarget(e.target.value)}
                    placeholder="新增 SSH 主機（如 ubuntu@192.168.1.50:22 或 ~/.ssh/config 別名）"
                    className="flex-1 px-2.5 py-1.5 rounded-lg bg-[#141720] border border-[#252a3a] text-slate-100 placeholder:text-slate-500 font-mono text-[11px]"
                  />
                  <input
                    type="text"
                    value={newSshLabel}
                    onChange={(e) => setNewSshLabel(e.target.value)}
                    placeholder="別名備註（選填）"
                    className="w-28 px-2 py-1.5 rounded-lg bg-[#141720] border border-[#252a3a] text-slate-100 placeholder:text-slate-500 text-[11px]"
                  />
                  <button
                    type="button"
                    disabled={!newSshTarget.trim() || probingSsh}
                    onClick={() => void handleAddSshHost()}
                    className="px-2.5 py-1.5 rounded-lg bg-indigo-500/20 hover:bg-indigo-500/30 border border-indigo-400/40 text-indigo-200 text-[11px] font-semibold cursor-pointer shrink-0 disabled:opacity-50"
                  >
                    ＋ 新增主機
                  </button>
                </div>

                {/* 當遠端主機需要密碼（Permission denied）或使用者點選「輸入密碼」時彈出的密碼驗證框 */}
                {needsPasswordBanner && (
                  <div className="p-3 rounded-xl bg-amber-500/10 border border-amber-400/50 space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[11px] font-bold text-amber-200 flex items-center gap-1.5">
                        <KeyRound className="w-3.5 h-3.5 text-amber-300" />
                        <span>
                          🔑 遠端主機 <code className="font-mono text-amber-100">{activeSshHost}</code> 需要 SSH 登入密碼驗證
                        </span>
                      </span>
                      <span className="text-[10px] text-amber-300/80">密碼僅暫存於本機記憶體，絕不寫入磁碟</span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <input
                        ref={passwordInputRef}
                        type="password"
                        aria-label="SSH 登入密碼"
                        value={sshPassword}
                        onChange={(e) => setSshPassword(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.preventDefault();
                            void handlePasswordProbe();
                          }
                        }}
                        placeholder={`請輸入 ${activeSshHost} 的 SSH 密碼…`}
                        className="flex-1 px-2.5 py-1.5 rounded-lg bg-[#141720] border border-amber-400/50 focus:border-amber-300 text-slate-100 placeholder:text-slate-500 font-mono text-xs"
                      />
                      <button
                        type="button"
                        disabled={probingSsh || !sshPassword}
                        onClick={() => void handlePasswordProbe()}
                        className="px-3 py-1.5 rounded-lg bg-amber-400 hover:bg-amber-300 text-slate-950 font-bold text-[11px] cursor-pointer shrink-0 disabled:opacity-50"
                      >
                        {probingSsh ? '驗證偵測中…' : '🔓 驗證密碼並偵測 CLI'}
                      </button>
                    </div>
                    <label className="flex items-center gap-1.5 text-[11px] text-amber-100/90 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={installSshKey}
                        onChange={(e) => setInstallSshKey(e.target.checked)}
                        className="rounded border-amber-400/50"
                      />
                      <span>驗證成功後自動安裝本機 SSH 公鑰至遠端 `~/.ssh/authorized_keys`（日後免再輸入密碼）</span>
                    </label>
                  </div>
                )}

                {/* 遠端 CLI 偵測結果摘要 */}
                {probingSsh ? (
                  <div className="px-2.5 py-1.5 rounded-lg bg-sky-500/10 border border-sky-500/30 text-sky-200 text-[11px]">
                    ⏳ 正在透過 SSH 對 <code className="font-mono">{activeSshHost}</code> 執行 CLI 嚴格握手偵測（claude / agy / kiro-cli）…
                  </div>
                ) : sshProbeInfo && sshProbeInfo.ssh_host === activeSshHost ? (
                  sshProbeInfo.ok ? (
                    <div className="px-2.5 py-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-200 text-[11px] space-y-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-bold">✓ 已連線 {sshProbeInfo.hostname || activeSshHost}</span>
                        {sshProbeInfo.platform && <span className="text-slate-400">({sshProbeInfo.platform})</span>}
                        {sshProbeInfo.has_password && (
                          <span className="px-1.5 py-0.2 rounded bg-amber-500/20 text-amber-200 text-[10px]">
                            🔑 已載入密碼授權
                          </span>
                        )}
                      </div>
                      <div className="text-slate-300">
                        遠端可調用 CLI：
                        {callableChoices.filter((c) => c.id !== 'auto').length > 0 ? (
                          callableChoices
                            .filter((c) => c.id !== 'auto')
                            .map((c) => (
                              <span
                                key={c.id}
                                className="inline-block ml-1 px-1.5 py-0.5 rounded bg-emerald-500/20 border border-emerald-400/40 text-emerald-200 font-mono text-[10px]"
                              >
                                {c.label}
                              </span>
                            ))
                        ) : (
                          <span className="text-rose-300 font-semibold ml-1">
                            ⚠️ 未偵測到任何可調用的 CLI（不可調用的 CLI 已自動隱藏）
                          </span>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className="px-2.5 py-1.5 rounded-lg bg-rose-500/15 border border-rose-500/30 text-rose-200 text-[11px] flex items-center justify-between gap-2">
                      <span>❌ SSH 探測失敗：{sshProbeInfo.error || sshProbeInfo.text || '無法連線至遠端主機'}</span>
                      {!showPasswordBox && (
                        <button
                          type="button"
                          onClick={() => {
                            setShowPasswordBox(true);
                            setTimeout(() => passwordInputRef.current?.focus(), 50);
                          }}
                          className="px-2 py-0.5 rounded bg-amber-400 text-slate-950 font-bold text-[10px] shrink-0 cursor-pointer"
                        >
                          🔑 輸入密碼重試
                        </button>
                      )}
                    </div>
                  )
                ) : null}
              </div>
            )}
          </div>

          {/* 2. 專案資料夾與 A / B 唯讀參考邊界設定（整合右側欄能力） */}
          <div className="space-y-2 p-3 rounded-xl bg-[#0e1017] border border-[#252a3a]">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <label className="font-semibold text-slate-200">
                {execMode === 'ssh' ? `遠端專案資料夾路徑（${activeSshHost || 'SSH 主機'}）` : '專案資料夾路徑與 A / B 邊界設定'}
              </label>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => void autoFillParentAsRef()}
                  className="px-2 py-0.5 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/30 text-amber-200 text-[11px] font-semibold cursor-pointer"
                  title="自動將目前資料夾的上層母資料夾設為唯讀參考區 A，並限定只可修改目前子資料夾 B"
                >
                  ⬆️ 自動將上層設為唯讀母資料夾 A
                </button>
                <button
                  type="button"
                  onClick={() => setShowRefEditor((v) => !v)}
                  className="px-2 py-0.5 rounded bg-[#1d2436] hover:bg-[#283149] border border-sky-500/30 text-sky-300 text-[11px] font-semibold cursor-pointer"
                >
                  {showRefEditor ? '收起 A/B 進階邊界' : '🔒 設定唯讀母資料夾 A／多目標 B'}
                </button>
              </div>
            </div>

            <div className="flex gap-2">
              <input
                ref={inputRef}
                type="text"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder={execMode === 'ssh' ? '例如：/home/ubuntu/my-app 或 ~/projects/app' : '例如：D:\\_AI_Projs\\my-app'}
                className="flex-1 px-3 py-2 rounded-lg bg-[#141720] border border-[#2c344a] text-slate-100 placeholder:text-slate-500 font-mono"
              />
              <button
                type="button"
                onClick={async () => {
                  if (execMode === 'ssh' && !activeSshHost) {
                    onToast('請先選擇或新增 SSH 遠端主機');
                    return;
                  }
                  setBrowserTarget('repo');
                  const d = await teamApi.browseDirs(String(repo ?? '').trim(), activeSshHost);
                  if (d) setDirBrowser(d);
                }}
                className="px-3 py-2 rounded-lg bg-[#1d2436] hover:bg-[#283149] border border-sky-500/30 text-sky-300 flex items-center gap-1 cursor-pointer shrink-0"
              >
                📂 瀏覽選取
              </button>
              <button
                type="button"
                onClick={() => void doScan()}
                className="px-3 py-2 rounded-lg bg-[#23293b] hover:bg-[#2e354d] text-slate-200 flex items-center gap-1.5 cursor-pointer shrink-0"
              >
                <FolderSearch className="w-3.5 h-3.5" />
                掃描
              </button>
            </div>

            {/* 進階：唯讀參考母資料夾 A 與手動新增複數目標 B */}
            {(showRefEditor || referenceRoot) && (
              <div className="p-2.5 rounded-lg bg-[#141824] border border-amber-500/35 space-y-2">
                <div className="space-y-1">
                  <label className="block text-[11px] font-semibold text-amber-300">
                    🔒 唯讀參考母資料夾 A（設定後，除目標 B 以外的兄弟資料夾皆為唯讀參考，防止改錯專案）
                  </label>
                  <div className="flex gap-1.5">
                    <input
                      type="text"
                      value={referenceRoot}
                      onChange={(e) => {
                        setReferenceRoot(e.target.value);
                        void loadRefSubdirs(e.target.value);
                      }}
                      placeholder="例如：D:\@BOF456 Proj\MMI_Kernel_Origin（留空代表不限制上層母資料夾）"
                      className="flex-1 px-2.5 py-1.5 rounded bg-[#0e1017] border border-[#2a3147] text-slate-100 font-mono text-[11px]"
                    />
                    <button
                      type="button"
                      onClick={async () => {
                        setBrowserTarget('ref');
                        const d = await teamApi.browseDirs(referenceRoot || repo, activeSshHost);
                        if (d) setDirBrowser(d);
                      }}
                      className="px-2.5 py-1 rounded bg-[#23293b] hover:bg-[#2e354d] text-slate-200 text-[11px] font-semibold cursor-pointer shrink-0"
                    >
                      📂 瀏覽選取 A
                    </button>
                    {referenceRoot && (
                      <button
                        type="button"
                        onClick={() => {
                          setReferenceRoot('');
                          setRefSubdirs([]);
                        }}
                        className="px-2 py-1 rounded bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 text-[11px] cursor-pointer shrink-0"
                      >
                        清除
                      </button>
                    )}
                  </div>
                </div>

                {/* 手動新增複數個目標子資料夾 B */}
                <div className="space-y-1">
                  <label className="block text-[11px] font-semibold text-emerald-300">
                    ➕ 手動指定複數個目標子資料夾 B（例如輸入 frontend, backend 同時開放前後端讀寫）：
                  </label>
                  <div className="flex gap-1.5">
                    <input
                      type="text"
                      value={extraTargetInput}
                      onChange={(e) => setExtraTargetInput(e.target.value)}
                      placeholder="輸入目標子資料夾名稱（如 frontend, backend）後按新增"
                      className="flex-1 px-2.5 py-1.5 rounded bg-[#0e1017] border border-[#2a3147] text-slate-100 font-mono text-[11px]"
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
                        setTargetSubdirs((prev) => {
                          const base = prev.length > 0 ? prev : targetSubdir ? [targetSubdir] : [];
                          const merged = Array.from(new Set([...base, ...parts]));
                          setTargetSubdir(merged[0] ?? '');
                          return merged;
                        });
                        setExtraTargetInput('');
                      }}
                      className="px-2.5 py-1 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-200 text-[11px] font-semibold cursor-pointer shrink-0"
                    >
                      ＋ 新增目標 B
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* 內嵌資料夾瀏覽器 */}
            {dirBrowser && (
              <div className="p-2.5 rounded-lg bg-[#141824] border border-sky-500/40 space-y-1.5">
                <div className="flex items-center justify-between text-[11px]">
                  <span className="font-semibold text-sky-300 truncate">
                    📂 {activeSshHost ? `[SSH: ${activeSshHost}] ` : ''}
                    瀏覽選取{browserTarget === 'ref' ? '「🔒 唯讀參考母資料夾 A」' : '「🎯 專案／目標資料夾 B」'}：{dirBrowser.path}
                  </span>
                  <button
                    type="button"
                    onClick={() => setDirBrowser(null)}
                    className="text-slate-400 hover:text-white px-1 cursor-pointer"
                  >
                    ✕
                  </button>
                </div>
                <div className="flex flex-wrap gap-1">
                  {dirBrowser.drives.map((drv) => (
                    <button
                      key={drv}
                      type="button"
                      onClick={async () => {
                        const d = await teamApi.browseDirs(drv, activeSshHost);
                        if (d) setDirBrowser(d);
                      }}
                      className="px-1.5 py-0.5 rounded bg-[#1e2538] hover:bg-[#2a334d] text-[10px] font-mono text-slate-300 cursor-pointer"
                    >
                      {drv}
                    </button>
                  ))}
                  {dirBrowser.parent && (
                    <button
                      type="button"
                      onClick={async () => {
                        const d = await teamApi.browseDirs(dirBrowser.parent, activeSshHost);
                        if (d) setDirBrowser(d);
                      }}
                      className="px-2 py-0.5 rounded bg-[#23293b] hover:bg-[#2e354d] text-[10px] text-amber-300 font-semibold cursor-pointer"
                    >
                      ⬆️ 上一層
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      if (browserTarget === 'ref') {
                        setReferenceRoot(dirBrowser.path);
                        void loadRefSubdirs(dirBrowser.path);
                      } else {
                        setRepo(dirBrowser.path);
                        void doScan(dirBrowser.path);
                      }
                      setDirBrowser(null);
                    }}
                    className="px-2 py-0.5 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-300 text-[10px] font-bold cursor-pointer"
                  >
                    ✓ 選取此資料夾並掃描子資料夾
                  </button>
                </div>
                <div className="max-h-32 overflow-y-auto grid grid-cols-1 sm:grid-cols-2 gap-1 pt-1">
                  {dirBrowser.dirs.map((d) => (
                    <div key={d.path} className="flex items-center justify-between gap-1 px-2 py-1 rounded bg-[#0e1017] border border-[#22283a] text-[10px]">
                      <button
                        type="button"
                        onClick={async () => {
                          const next = await teamApi.browseDirs(d.path, activeSshHost);
                          if (next) setDirBrowser(next);
                        }}
                        className="text-left text-slate-200 hover:text-sky-300 truncate flex-1 cursor-pointer"
                      >
                        📁 {d.name}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          if (browserTarget === 'ref') {
                            setReferenceRoot(d.path);
                            void loadRefSubdirs(d.path);
                          } else {
                            setRepo(d.path);
                            void doScan(d.path);
                          }
                          setDirBrowser(null);
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

            {execMode === 'local' && projects.length > 0 && (
              <div className="flex flex-wrap gap-1.5 pt-0.5">
                {projects.slice(0, 6).map((p) => {
                  const pPath = p.repo || p.path || '';
                  return (
                    <button
                      key={pPath}
                      type="button"
                      onClick={() => {
                        setRepo(pPath);
                        void doScan(pPath);
                      }}
                      className={`px-2 py-0.5 rounded border text-[11px] cursor-pointer ${
                        repo === pPath
                          ? 'bg-emerald-500/20 border-emerald-500/40 text-emerald-300'
                          : 'bg-[#1a1e2b] border-[#282e42] text-slate-400 hover:text-slate-200'
                      }`}
                    >
                      {p.name || pPath}
                    </button>
                  );
                })}
              </div>
            )}

            {scan && (
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400 pt-0.5">
                {scan.branch && (
                  <span className="px-1.5 py-0.5 rounded bg-[#181d2b] border border-[#2a3147] text-sky-300 font-mono">
                    🌿 分支：{scan.branch}
                  </span>
                )}
                <span>偵測到檢查指令：{(scan.checks ?? []).join('、') || '（無）'}</span>
                {scan.problem && <span className="text-amber-300">⚠️ {scan.problem}</span>}
              </div>
            )}

            {combinedSubdirChoices.length > 0 && (
              <div className="mt-1.5 p-2.5 rounded-lg bg-[#141824] border border-[#252a3a] space-y-2">
                <label className="block text-[11px] font-semibold text-emerald-300">
                  🎯 目標工作子資料夾 B（支援單選或複選多個目標資料夾，例如同時勾選前後端；其餘子資料夾自動設為唯讀參考）
                </label>
                <select
                  aria-label="目標工作子資料夾 B"
                  value={targetSubdir}
                  onChange={(e) => {
                    const val = e.target.value;
                    setTargetSubdir(val);
                    setTargetSubdirs(val ? [val] : []);
                  }}
                  className="w-full px-2.5 py-1.5 rounded bg-[#0e1017] border border-[#282e42] text-slate-100 text-xs"
                >
                  <option value="">（整個母資料夾 A 皆可修改，不限定子資料夾）</option>
                  {combinedSubdirChoices.map((sub) => (
                    <option key={sub} value={sub}>
                      🎯 僅允許修改：{sub}/（其他兄弟資料夾唯讀）
                    </option>
                  ))}
                </select>
                <div className="space-y-1">
                  <div className="text-[10px] text-slate-400">
                    或點擊下方複選多個目標資料夾 B（例如同時將前端與後端設為可讀寫目標）：
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {combinedSubdirChoices.map((sub) => {
                      const activeList = targetSubdirs.length > 0 ? targetSubdirs : targetSubdir ? [targetSubdir] : [];
                      const checked = activeList.includes(sub);
                      return (
                        <button
                          key={sub}
                          type="button"
                          onClick={() => {
                            const next = checked
                              ? activeList.filter((item) => item !== sub)
                              : [...activeList, sub];
                            setTargetSubdirs(next);
                            setTargetSubdir(next[0] ?? '');
                          }}
                          className={`px-2 py-0.5 rounded border text-[11px] font-mono cursor-pointer transition-colors ${
                            checked
                              ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-200 font-semibold'
                              : 'bg-[#0e1017] border-[#252a3a] text-slate-400 hover:text-slate-200'
                          }`}
                        >
                          {checked ? '☑ 🎯 ' : '☐ '}
                          {sub}/
                        </button>
                      );
                    })}
                  </div>
                </div>
                {(targetSubdir || targetSubdirs.length > 0 || referenceRoot) && (() => {
                  const activeTargets = targetSubdirs.length > 0 ? targetSubdirs : targetSubdir ? [targetSubdir] : [];
                  return (
                    <div className="text-[11px] leading-relaxed text-slate-300 space-y-0.5 pt-0.5">
                      {activeTargets.length > 0 && (
                        <div className="flex flex-wrap items-center gap-1">
                          <span className="text-emerald-300 font-semibold">
                            🎯 可讀寫目標（共 {activeTargets.length} 個）：
                          </span>
                          {activeTargets.map((t) => (
                            <code key={t} className="px-1 py-0.5 rounded bg-emerald-500/15 text-emerald-200">
                              {t}/
                            </code>
                          ))}
                        </div>
                      )}
                      <div>
                        <span className="text-amber-300 font-semibold">🔒 唯讀參考來源：</span>
                        <span className="text-slate-400 ml-1">
                          {referenceRoot ? `${referenceRoot} 下除目標 B 外之檔案` : combinedSubdirChoices.filter((s) => !activeTargets.includes(s)).join('、') || '母資料夾其餘檔案'}
                        </span>
                      </div>
                    </div>
                  );
                })()}
              </div>
            )}
          </div>

          {/* 3. 任務目標與 CE 計畫 (docs/plans/*.md) / progress.md 對齊與結構化輔助（整合右側欄能力） */}
          <div className="space-y-2 p-3 rounded-xl bg-[#0e1017] border border-[#252a3a]">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <label className="font-semibold text-slate-200">任務目標與 CE 計畫對齊</label>
              <div className="flex flex-wrap items-center gap-1.5">
                {scan?.progress?.task && (
                  <button
                    type="button"
                    onClick={() => {
                      const t = scan.progress?.task || '';
                      setGoal((prev) => (prev.trim() ? `${prev.trim()}\n接續 progress.md 任務：${t}` : `接續 progress.md 任務：${t}`));
                      onToast('已帶入 progress.md 斷點任務');
                    }}
                    className="px-2 py-0.5 rounded bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 text-emerald-300 text-[11px] font-semibold cursor-pointer"
                    title={`帶入 ${scan.progress.file || 'progress.md'} 的目前任務：${scan.progress.task}`}
                  >
                    📄 套用 progress.md 斷點任務
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    const tpl =
                      '【最終目的】：\n【目標修改範圍】：\n【驗收與測試條件】：\n1. ';
                    setGoal((prev) => (prev.trim() ? `${prev.trim()}\n\n${tpl}` : tpl));
                  }}
                  className="px-2 py-0.5 rounded bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-sky-300 text-[11px] font-semibold flex items-center gap-1 cursor-pointer"
                  title="插入結構化任務與驗收條件範本，讓 PO 與 Tech Lead 更精準拆解 CE 計畫"
                >
                  <Sparkles className="w-3 h-3" />
                  <span>插入結構化驗收範本</span>
                </button>
              </div>
            </div>

            {scan?.docs && scan.docs.length > 0 && (
              <div className="flex items-center gap-1.5 p-2 rounded-lg bg-[#141824] border border-sky-500/30">
                <FileText className="w-3.5 h-3.5 text-sky-300 shrink-0" />
                <select
                  aria-label="選擇既有 CE 計畫或規格檔"
                  value={selectedDocRel}
                  onChange={(e) => setSelectedDocRel(e.target.value)}
                  className="flex-1 px-2 py-1 rounded bg-[#0e1017] border border-[#2c344a] text-slate-100 text-[11px]"
                >
                  {scan.docs.map((d) => (
                    <option key={d.relpath} value={d.relpath}>
                      {d.relpath} — {d.title}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => {
                    const doc = scan.docs?.find((d) => d.relpath === selectedDocRel) ?? scan.docs?.[0];
                    if (!doc) return;
                    const line = `請依據專案計畫文件 ${doc.relpath}（${doc.title}）執行實作與測試驗收。`;
                    setGoal((prev) => (prev.trim() ? `${prev.trim()}\n${line}` : line));
                    onToast(`已對齊 CE 計畫：${doc.relpath}`);
                  }}
                  className="px-2.5 py-1 rounded bg-sky-500/20 hover:bg-sky-500/30 border border-sky-500/40 text-sky-200 text-[11px] font-semibold shrink-0 cursor-pointer"
                >
                  套用 CE 計畫目標
                </button>
              </div>
            )}

            <textarea
              rows={3}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="描述這個房間要完成的功能、修復或重構（亦可點上方按鈕帶入 CE 計畫或結構化驗收範本）…"
              className="w-full px-3 py-2 rounded-lg bg-[#141720] border border-[#2c344a] text-slate-100 placeholder:text-slate-500"
            />
          </div>

          {/* 3.5 v2 任務模式與 Decision-1 128 題快速分流開關 */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 p-3 rounded-xl bg-[#0e1017] border border-sky-500/25">
            <div className="space-y-1.5">
              <label className="block font-semibold text-sky-300 text-xs">任務模式（LangGraph v2 狀態機）</label>
              <div className="flex items-center gap-1 bg-[#141824] p-1 rounded-lg border border-[#282e42]" role="radiogroup" aria-label="任務模式">
                <button
                  type="button"
                  role="radio"
                  aria-checked={taskMode === 'standard'}
                  onClick={() => setTaskMode('standard')}
                  className={`flex-1 px-2 py-1.5 rounded-md text-[11px] font-semibold cursor-pointer transition-colors ${
                    taskMode === 'standard' ? 'bg-sky-400 text-slate-950 font-bold' : 'text-slate-400 hover:text-slate-100'
                  }`}
                >
                  ⚡ 標準開發 / 除錯 / 重構
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={taskMode === 'triage'}
                  onClick={() => setTaskMode('triage')}
                  className={`flex-1 px-2 py-1.5 rounded-md text-[11px] font-semibold cursor-pointer transition-colors ${
                    taskMode === 'triage' ? 'bg-amber-400 text-slate-950 font-bold' : 'text-slate-400 hover:text-slate-100'
                  }`}
                  title="先以 Decision-1 並發批次分診（單次最多 128 題）篩出有缺口的模組，再生成計畫"
                >
                  🔎 大規模盤點 / 遷移（D1 分診）
                </button>
              </div>
            </div>
            <div className="space-y-1.5">
              <label className="block font-semibold text-emerald-300 text-xs">Decision-1 快速審查分流（15 題打包）</label>
              <div className="flex items-center gap-1 bg-[#141824] p-1 rounded-lg border border-[#282e42]" role="radiogroup" aria-label="Decision-1 快速審查分流">
                <button
                  type="button"
                  role="radio"
                  aria-checked={d1FastReview}
                  onClick={() => setD1FastReview(true)}
                  className={`flex-1 px-2 py-1.5 rounded-md text-[11px] font-semibold cursor-pointer transition-colors ${
                    d1FastReview ? 'bg-emerald-400 text-slate-950 font-bold' : 'text-slate-400 hover:text-slate-100'
                  }`}
                  title="單一單元過測試後由 D1 一次問 15 題；低風險高信心直接放行 commit，高風險才喚起大模型深度審查"
                >
                  🚀 開啟快速分流（建議）
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={!d1FastReview}
                  onClick={() => setD1FastReview(false)}
                  className={`flex-1 px-2 py-1.5 rounded-md text-[11px] font-semibold cursor-pointer transition-colors ${
                    !d1FastReview ? 'bg-rose-400 text-slate-950 font-bold' : 'text-slate-400 hover:text-slate-100'
                  }`}
                >
                  🔍 全部走大模型審查
                </button>
              </div>
            </div>
          </div>

          {/* 4. 精簡 2 座位配置（研發實作・深度審查；快速決策／閘門由 Decision-1 自動執行） */}
          <div className="space-y-2.5 p-3 rounded-xl bg-[#0e1017] border border-[#252a3a]">
            <div className="flex items-center justify-between gap-2 flex-wrap pb-1.5 border-b border-[#1e2436]">
              <div>
                <span className="text-xs font-bold text-slate-200">
                  大模型座位配置（研發實作・深度審查；決策閘門與分診由 Decision-1 自動執行）
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  disabled={scanningCatalog}
                  onClick={async () => {
                    setScanningCatalog(true);
                    try {
                      const res = await teamApi.scanModelCatalog();
                      const models = (res as { models?: typeof catalogModels }).models;
                      if (Array.isArray(models)) setCatalogModels(models);
                      onToast(`已掃描 LiteLLM 線上模型（共 ${models?.length ?? catalogModels.length} 個）`);
                    } finally {
                      setScanningCatalog(false);
                    }
                  }}
                  className="px-2 py-1 rounded bg-[#1d2436] hover:bg-[#283149] text-emerald-300 border border-emerald-500/30 text-[10px] font-semibold cursor-pointer disabled:opacity-50"
                >
                  {scanningCatalog ? '掃描中…' : `掃描 LiteLLM (${catalogModels.length})`}
                </button>
                <button
                  type="button"
                  onClick={detectWizardModels}
                  className="px-2 py-1 rounded bg-[#1d2436] hover:bg-[#283149] text-sky-300 border border-sky-500/30 text-[10px] font-semibold flex items-center gap-1 cursor-pointer"
                >
                  <RefreshCw className={`w-3 h-3 ${probingSsh ? 'animate-spin' : ''}`} />
                  <span>{execMode === 'ssh' ? '重新偵測遠端模型' : '重新偵測可用模型'}</span>
                </button>
              </div>
            </div>

            <div className="space-y-2.5">
              {ROLE_DEFINITIONS.filter((d) => d.id === 'impl' || d.id === 'review').map((roleDef) => {
                const role = roleDef.id;
                const curSeat = roleConfig[role]?.seat || 'auto';
                const curModel = roleConfig[role]?.model || '';
                const seatOptions = [
                  { id: 'auto', label: roleDef.autoSeatLabel },
                  { id: 'litellm', label: 'LiteLLM Proxy（線上模型）' },
                  ...callableChoices.filter((c) => c.id !== 'auto'),
                ];
                const otherDevSeat =
                  role === 'impl'
                    ? roleConfig.review?.seat || 'auto'
                    : role === 'review'
                      ? roleConfig.impl?.seat || 'auto'
                      : '';
                const modelOptions = buildRoleModelDropdownOptions(
                  role,
                  curSeat,
                  curModel,
                  modelsView,
                  catalogModels,
                  callableChoices,
                  otherDevSeat,
                );
                const seatAriaLabel = roleDef.seatLabel;
                const modelAriaLabel = roleDef.modelLabel;

                return (
                  <div key={role} className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                    <div className="space-y-1">
                      <label
                        title={roleDef.description}
                        className="relative group/wiz-role inline-flex items-center gap-1 font-medium text-slate-300 cursor-help"
                      >
                        <span className="underline decoration-dotted decoration-slate-500 underline-offset-2">
                          {roleDef.seatLabel}
                        </span>
                        <span className="text-[11px] text-sky-400/90" aria-hidden="true">ⓘ</span>
                        <span
                          role="tooltip"
                          className="pointer-events-none opacity-0 group-hover/wiz-role:opacity-100 transition-opacity duration-150 absolute left-0 top-full mt-1 z-40 w-72 p-2.5 rounded-lg bg-[#0d1220] border border-sky-400/40 text-xs text-slate-200 leading-relaxed shadow-xl font-normal whitespace-normal"
                        >
                          {roleDef.description}
                        </span>
                      </label>
                      <select
                        aria-label={seatAriaLabel}
                        value={curSeat}
                        onChange={(e) => {
                          const nextSeat = e.target.value;
                          const defMod =
                            nextSeat === 'auto'
                              ? ''
                              : defaultModelForSeat(nextSeat, modelsView, catalogModels, role);
                          updateRoleSeatOrModel(role, nextSeat, defMod);
                        }}
                        className="w-full px-2.5 py-1.5 rounded-lg bg-[#141720] border border-[#2c344a] text-slate-100"
                      >
                        {seatOptions.map((o) => (
                          <option key={o.id} value={o.id}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="space-y-1">
                      <label title={roleDef.description} className="block font-medium text-slate-300">
                        {roleDef.modelLabel}
                      </label>
                      <select
                        aria-label={modelAriaLabel}
                        value={curSeat === 'auto' ? '' : curModel}
                        onChange={(e) => updateRoleSeatOrModel(role, curSeat, e.target.value)}
                        className="w-full px-2.5 py-1.5 rounded-lg bg-[#141720] border border-[#2c344a] text-slate-100"
                      >
                        <option value="">
                          {curSeat === 'auto' ? roleDef.autoDefaultModelLabel : '（依工具預設模型）'}
                        </option>
                        {modelOptions.map((m) => (
                          <option key={m.id} value={m.id}>
                            {m.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* 5. 房間決策模式（🤖 AI 高階主管代管 vs 👤 關鍵決策交給我）與自主程度（整合右側欄能力） */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 p-3 rounded-xl bg-[#0e1017] border border-[#252a3a]">
            <div className="space-y-1.5">
              <label className="block font-semibold text-slate-200">房間決策模式</label>
              <div className="flex items-center gap-1 bg-[#141824] p-1 rounded-lg border border-[#282e42]" role="radiogroup" aria-label="房間決策模式">
                <button
                  type="button"
                  role="radio"
                  aria-checked={delegate}
                  onClick={() => setDelegate(true)}
                  className={`flex-1 px-2 py-1.5 rounded-md text-[11px] font-semibold cursor-pointer transition-colors ${
                    delegate ? 'bg-emerald-400 text-slate-950 font-bold' : 'text-slate-400 hover:text-slate-100'
                  }`}
                  title="高階主管（Decision-1 + GLM-5.3）自動審查規格與低風險權限，僅在關鍵風險或超時才交給人工"
                >
                  🤖 AI 高階主管代管
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={!delegate}
                  onClick={() => setDelegate(false)}
                  className={`flex-1 px-2 py-1.5 rounded-md text-[11px] font-semibold cursor-pointer transition-colors ${
                    !delegate ? 'bg-sky-400 text-slate-950 font-bold' : 'text-slate-400 hover:text-slate-100'
                  }`}
                  title="規格確認、權限與問題一律送收件匣由你決定"
                >
                  👤 關鍵決策交給我
                </button>
              </div>
            </div>

            <div className="space-y-1.5">
              <label className="block font-semibold text-slate-200">自主程度</label>
              <select
                aria-label="自主程度"
                value={autonomy}
                onChange={(e) => setAutonomy(e.target.value)}
                className="w-full px-2.5 py-2 rounded-lg bg-[#141720] border border-[#2c344a] text-slate-100"
              >
                <option value="balanced">平衡（預設）</option>
                <option value="careful">謹慎（每關確認）</option>
                <option value="bold">大膽（高自主）</option>
              </select>
            </div>
          </div>
        </div>

        <div className="px-5 py-3.5 border-t border-[#252a3a] flex justify-end gap-2 bg-[#10131b]">
          <button
            type="button"
            onClick={onClose}
            className="px-3.5 py-2 rounded-lg text-xs text-slate-300 hover:bg-[#23293b] cursor-pointer"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={busy || !String(repo ?? '').trim() || !String(goal ?? '').trim()}
            className="px-4 py-2 rounded-lg text-xs font-semibold bg-emerald-400 hover:bg-emerald-300 text-slate-950 disabled:opacity-40 flex items-center gap-1.5 cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" />
            建立並派工
          </button>
        </div>
      </form>
    </div>
  );
}
