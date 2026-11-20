'use client';

import React, { useEffect, useRef, useState } from 'react';
import {
  CheckCircle2,
  Copy,
  Download,
  ExternalLink,
  PlayCircle,
  RefreshCw,
  Server,
  Upload,
  Wrench,
  X,
  AlertTriangle,
} from 'lucide-react';
import { teamApi } from '@/lib/api';

export interface PreflightCliItem {
  name: string;
  label: string;
  exe: string;
  protocol: string;
  installed: boolean;
  path: string;
  install_command: string;
  docs_url: string;
  note: string;
}

export interface PreflightReport {
  ok: boolean;
  needs_setup: boolean;
  installed_cli_count: number;
  clis: PreflightCliItem[];
  git: { installed: boolean; path: string };
  model: {
    ready: boolean;
    file: string;
    example_file?: string;
    default_base_url?: string;
    base_url: string;
    glm_model: string;
    d1_model: string;
    has_key: boolean;
    key_preview: string;
    has_d1_key: boolean;
    d1_key_preview?: string;
    sovereign_base_url?: string;
    has_sovereign_key?: boolean;
    sovereign_key_preview?: string;
    embedding_model?: string;
    embedding_dim?: number;
    rerank_model?: string;
    wiki_base_url?: string;
    has_wiki_key?: boolean;
    wiki_key_preview?: string;
  };
}

interface SetupWizardModalProps {
  open: boolean;
  report: PreflightReport | null;
  onClose: () => void;
  onRefresh: () => Promise<void>;
  onStartDemo: () => Promise<void>;
  onToast: (msg: string) => void;
}

const ROOM_PREF_STORAGE_KEYS = [
  'pixel_office_room_vista_v1',
  'pixel_office_room_theme_v1',
  'pixel_office_room_time_v1',
];

export default function SetupWizardModal({
  open,
  report,
  onClose,
  onRefresh,
  onStartDemo,
  onToast,
}: SetupWizardModalProps) {
  const [baseUrl, setBaseUrl] = useState('http://172.22.119.193:4000');
  const [apiKey, setApiKey] = useState('');
  const [d1Key, setD1Key] = useState('');
  const [glmModel, setGlmModel] = useState('glm-5.3');
  const [d1Model, setD1Model] = useState('Decision-1');
  const [sovereignUrl, setSovereignUrl] = useState('');
  const [sovereignKey, setSovereignKey] = useState('');
  const [embeddingModel, setEmbeddingModel] = useState('qwen3-embedding');
  const [rerankModel, setRerankModel] = useState('bge-reranker-v2-m3');
  const [wikiUrl, setWikiUrl] = useState('');
  const [wikiKey, setWikiKey] = useState('');
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [startingDemo, setStartingDemo] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string; models?: string[] } | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!report?.model) return;
    setBaseUrl(report.model.base_url || report.model.default_base_url || 'http://172.22.119.193:4000');
    setGlmModel(report.model.glm_model || 'glm-5.3');
    setD1Model(report.model.d1_model || 'Decision-1');
    setSovereignUrl(report.model.sovereign_base_url || '');
    setEmbeddingModel(report.model.embedding_model || 'qwen3-embedding');
    setRerankModel(report.model.rerank_model || 'bge-reranker-v2-m3');
    setWikiUrl(report.model.wiki_base_url || '');
  }, [report]);

  if (!open) return null;

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    const res = await teamApi.testPreflight({
      base_url: baseUrl.trim(),
      key: apiKey.trim(),
      glm_model: glmModel.trim(),
    });
    setTesting(false);
    if (res) {
      setTestResult({
        ok: Boolean(res.ok),
        message: String(res.message || (res.ok ? '連線成功' : '連線失敗')),
        models: Array.isArray(res.models) ? (res.models as string[]) : [],
      });
    } else {
      setTestResult({ ok: false, message: '無法呼叫後端測試端點' });
    }
  };

  const handleSave = async () => {
    if (!baseUrl.trim()) {
      setTestResult({ ok: false, message: '請先填入模型 API 的 IP / 連接埠或網址（Base URL）' });
      return;
    }
    setSaving(true);
    const res = await teamApi.savePreflight({
      base_url: baseUrl.trim(),
      key: apiKey.trim() || undefined,
      decision1_key: d1Key.trim() || undefined,
      glm_model: glmModel.trim() || 'glm-5.3',
      d1_model: d1Model.trim() || 'Decision-1',
      sovereign_base_url: sovereignUrl.trim(),
      sovereign_key: sovereignKey.trim() || undefined,
      embedding_model: embeddingModel.trim() || 'qwen3-embedding',
      rerank_model: rerankModel.trim() || 'bge-reranker-v2-m3',
      wiki_base_url: wikiUrl.trim(),
      wiki_key: wikiKey.trim() || undefined,
    });
    setSaving(false);
    if (res?.ok) {
      setApiKey('');
      setD1Key('');
      setSovereignKey('');
      setWikiKey('');
      onToast('已將所有連線與金鑰寫入 litellm.local.json 並觸發模型掃描！');
      await onRefresh();
    } else {
      setTestResult({ ok: false, message: String(res?.error || res?.text || '儲存失敗') });
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    await onRefresh();
    setRefreshing(false);
    onToast('已重新偵測本機 CLI 與模型設定');
  };

  const handleCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      onToast('已複製安裝指令到剪貼簿');
    } catch {
      onToast('複製失敗，請手動選取指令');
    }
  };

  const handleExportProfile = async () => {
    const backend = await teamApi.exportProfile();
    const roomPrefs: Record<string, unknown> = {};
    try {
      for (const k of ROOM_PREF_STORAGE_KEYS) {
        const raw = window.localStorage.getItem(k);
        if (raw) roomPrefs[k] = JSON.parse(raw);
      }
    } catch {
      // ignore
    }
    const bundle = {
      ...(backend || { schema: 'buttonauto-profile-v1' }),
      pixel_office_ui: roomPrefs,
    };
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `pixel-office-profile-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    onToast('已匯出安全設定檔（已自動排除所有金鑰）');
  };

  const handleImportFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as Record<string, unknown>;
      if (parsed.pixel_office_ui && typeof parsed.pixel_office_ui === 'object') {
        const ui = parsed.pixel_office_ui as Record<string, unknown>;
        for (const k of ROOM_PREF_STORAGE_KEYS) {
          if (ui[k] !== undefined) {
            window.localStorage.setItem(k, JSON.stringify(ui[k]));
          }
        }
      }
      const res = await teamApi.importProfile(parsed);
      if (res?.ok) {
        onToast('已匯入設定與房間佈置（重新整理房間即可套用視覺偏好）');
        await onRefresh();
      } else {
        onToast(`匯入失敗：${String(res?.error || '格式錯誤')}`);
      }
    } catch {
      onToast('讀取設定檔失敗：請確認為有效的 .json 檔案');
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const clis = report?.clis ?? [];
  const modelReady = Boolean(report?.model?.ready);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-xs p-3 sm:p-5"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-2xl bg-[#131722] border border-[#2b3348] shadow-2xl text-slate-100"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#232a3d]">
          <div className="flex items-center gap-2.5">
            <Wrench className="w-5 h-5 text-sky-400" />
            <div>
              <h2 className="text-base font-bold">環境健檢與首次啟動設定精靈</h2>
              <p className="text-xs text-slate-400">
                自動檢查本機 CLI 與模型 API（IP / 連接埠 / 金鑰），開箱即可上手
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-[#1f2536] cursor-pointer"
            aria-label="關閉精靈"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-5">
          {/* Section 1: Local CLI check */}
          <section className="rounded-xl bg-[#0e121b] border border-[#232a3d] p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold">1. 本機 AI CLI 偵測</span>
                <span
                  className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${
                    (report?.installed_cli_count ?? 0) > 0
                      ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                      : 'bg-amber-500/15 text-amber-300 border border-amber-500/30'
                  }`}
                >
                  已偵測 {report?.installed_cli_count ?? 0} / {clis.length || 3}
                </span>
              </div>
              <button
                type="button"
                onClick={handleRefresh}
                disabled={refreshing}
                className="flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-lg bg-[#1a2030] hover:bg-[#242c42] text-slate-200 border border-[#2d364d] cursor-pointer"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
                重新偵測
              </button>
            </div>

            <div className="space-y-2">
              {clis.map((cli) => (
                <div
                  key={cli.name}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-[#151a27] border border-[#232a3d] px-3 py-2.5"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      {cli.installed ? (
                        <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
                      ) : (
                        <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
                      )}
                      <span className="text-xs font-semibold text-slate-100">{cli.label}</span>
                      <span className="text-[10px] font-mono text-slate-400">({cli.exe})</span>
                    </div>
                    <p className="text-[11px] text-slate-400 mt-0.5 truncate">
                      {cli.installed ? `路徑：${cli.path}` : cli.note}
                    </p>
                  </div>
                  {!cli.installed && (
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => handleCopy(cli.install_command)}
                        className="flex items-center gap-1 px-2 py-1 text-[11px] rounded bg-sky-500/15 text-sky-300 border border-sky-500/30 hover:bg-sky-500/25 cursor-pointer"
                        title={cli.install_command}
                      >
                        <Copy className="w-3 h-3" />
                        複製安裝指令
                      </button>
                      {cli.docs_url && (
                        <a
                          href={cli.docs_url}
                          target="_blank"
                          rel="noreferrer"
                          className="p-1 text-slate-400 hover:text-sky-300"
                          title="開啟官方文件"
                        >
                          <ExternalLink className="w-3.5 h-3.5" />
                        </a>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>

          {/* Section 2: Model API / IP & Port */}
          <section className="rounded-xl bg-[#0e121b] border border-[#232a3d] p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Server className="w-4 h-4 text-sky-400" />
                <span className="text-sm font-semibold">2. 模型 API 端點（IP / 連接埠 / 金鑰）</span>
                <span
                  className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${
                    modelReady
                      ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                      : 'bg-amber-500/15 text-amber-300 border border-amber-500/30'
                  }`}
                >
                  {modelReady ? '已設定' : '尚未設定'}
                </span>
              </div>
              <div className="flex items-center gap-1.5 text-[11px]">
                <button
                  type="button"
                  onClick={() => {
                    setBaseUrl('http://127.0.0.1:11434/v1');
                    setGlmModel('qwen2.5-coder:32b');
                  }}
                  className="px-2 py-0.5 rounded bg-[#1a2030] hover:bg-[#242c42] text-slate-300 border border-[#2d364d] cursor-pointer"
                >
                  本機 Ollama
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setBaseUrl('http://172.22.119.193:4000');
                    setGlmModel('glm-5.3');
                    setD1Model('Decision-1');
                  }}
                  className="px-2 py-0.5 rounded bg-[#1a2030] hover:bg-[#242c42] text-slate-300 border border-[#2d364d] cursor-pointer"
                >
                  Y6P2 LiteLLM（預設）
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="block sm:col-span-2">
                <span className="text-[11px] text-slate-400">LiteLLM Proxy Base URL（含 IP 與連接埠，將每日自動掃描 /v1/models 與 /model_group/info）</span>
                <input
                  type="text"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="http://172.22.119.193:4000"
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">
                  LiteLLM 主 API Key（{report?.model?.has_key ? `已存 ${report.model.key_preview}，留空沿用` : '必填'}）
                </span>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-..."
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">
                  Decision-1 API Key（{report?.model?.has_d1_key ? `已存 ${report.model.d1_key_preview || '***'}，留空沿用` : '留空則共用主 Key'}）
                </span>
                <input
                  type="password"
                  value={d1Key}
                  onChange={(e) => setD1Key(e.target.value)}
                  placeholder="留空共用主 Key"
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">主對話／編寫預設模型</span>
                <input
                  type="text"
                  value={glmModel}
                  onChange={(e) => setGlmModel(e.target.value)}
                  placeholder="glm-5.3"
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">決策模型名稱</span>
                <input
                  type="text"
                  value={d1Model}
                  onChange={(e) => setD1Model(e.target.value)}
                  placeholder="Decision-1"
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">主權 AI 向量／重排 URL（選填）</span>
                <input
                  type="text"
                  value={sovereignUrl}
                  onChange={(e) => setSovereignUrl(e.target.value)}
                  placeholder="https://..."
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">
                  主權 AI Key（{report?.model?.has_sovereign_key ? `已存 ${report.model.sovereign_key_preview}，留空沿用` : '選填'}）
                </span>
                <input
                  type="password"
                  value={sovereignKey}
                  onChange={(e) => setSovereignKey(e.target.value)}
                  placeholder="選填"
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">Wiki API URL（選填）</span>
                <input
                  type="text"
                  value={wikiUrl}
                  onChange={(e) => setWikiUrl(e.target.value)}
                  placeholder="http://..."
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>

              <label className="block">
                <span className="text-[11px] text-slate-400">
                  Wiki API Key（{report?.model?.has_wiki_key ? `已存 ${report.model.wiki_key_preview}，留空沿用` : '選填'}）
                </span>
                <input
                  type="password"
                  value={wikiKey}
                  onChange={(e) => setWikiKey(e.target.value)}
                  placeholder="選填"
                  className="mt-1 w-full rounded-lg bg-[#151a27] border border-[#2a334a] px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-sky-400"
                />
              </label>
            </div>

            {testResult && (
              <div
                className={`rounded-lg px-3 py-2 text-xs border ${
                  testResult.ok
                    ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-200'
                    : 'bg-rose-500/10 border-rose-500/30 text-rose-200'
                }`}
              >
                <div>{testResult.message}</div>
                {testResult.models && testResult.models.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {testResult.models.slice(0, 8).map((m) => (
                      <button
                        key={m}
                        type="button"
                        onClick={() => setGlmModel(m)}
                        className="px-1.5 py-0.5 rounded bg-emerald-500/15 hover:bg-emerald-500/30 text-[10px] font-mono cursor-pointer"
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                type="button"
                onClick={handleTest}
                disabled={testing}
                className="px-3 py-1.5 text-xs font-medium rounded-lg bg-[#1c2336] hover:bg-[#27314b] text-slate-100 border border-[#313d5c] cursor-pointer"
              >
                {testing ? '連線測試中…' : '測試連線'}
              </button>
              <button
                type="button"
                onClick={handleSave}
                disabled={saving}
                className="px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-sky-500 hover:bg-sky-400 text-slate-950 cursor-pointer"
              >
                {saving ? '儲存中…' : '儲存並立即套用'}
              </button>
            </div>
          </section>

          {/* Section 3: Demo Mode & Safe Profile Export/Import */}
          <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-[#0e121b] border border-[#232a3d] p-4">
            <button
              type="button"
              disabled={startingDemo}
              onClick={async () => {
                setStartingDemo(true);
                await onStartDemo();
                setStartingDemo(false);
                onClose();
              }}
              className="flex items-center gap-1.5 px-3.5 py-2 text-xs font-semibold rounded-lg bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 border border-emerald-500/40 cursor-pointer"
            >
              <PlayCircle className="w-4 h-4" />
              {startingDemo ? '建立 Demo 房間中…' : '還沒接好 API？一鍵開啟 Demo 展示房間'}
            </button>

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleExportProfile}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg bg-[#1a2030] hover:bg-[#242c42] text-slate-200 border border-[#2d364d] cursor-pointer"
                title="匯出房間窗景偏好、SSH 主機與成本上限（自動排除金鑰）"
              >
                <Download className="w-3.5 h-3.5" />
                匯出設定
              </button>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg bg-[#1a2030] hover:bg-[#242c42] text-slate-200 border border-[#2d364d] cursor-pointer"
              >
                <Upload className="w-3.5 h-3.5" />
                匯入設定
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={handleImportFile}
              />
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
