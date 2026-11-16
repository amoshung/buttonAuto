import type { NextConfig } from 'next';

// 輸出成純靜態檔（out/），再由 scripts/copy-out.mjs 複製到 ../web/pixel，給 AI 工程助理的 dashboard.py 提供。
// 執行時不需要 Node；資料 API（/api/watcher/*）由 Python 端實作。
const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: 'export',
  trailingSlash: false,
  images: { unoptimized: true },
  eslint: { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: false },
};

export default nextConfig;
