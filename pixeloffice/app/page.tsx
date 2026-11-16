'use client';

import { useEffect, useState } from 'react';
import PixelOfficeApp from '@/components/PixelOfficeApp';
import { officeApi } from '@/lib/api';
import type { OfficeSnapshot } from '@/lib/types';

// 靜態輸出：初始資料改在瀏覽器向 AI 工程助理取得，之後由 SSE 即時更新
export default function HomePage() {
  const [initial, setInitial] = useState<OfficeSnapshot | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const snap = await officeApi.state();
      if (!alive) return;
      if (snap) setInitial(snap);
      else setFailed(true);
    };
    void load();
    return () => {
      alive = false;
    };
  }, []);

  if (!initial) {
    return (
      <div className="h-screen w-screen flex items-center justify-center bg-[#12141c] text-sm text-slate-400">
        {failed ? '連不上 AI 工程助理：請從助理主視窗的「🌐」按鈕重新打開。' : '正在進入辦公室…'}
      </div>
    );
  }
  return <PixelOfficeApp initialSnapshot={initial} />;
}
