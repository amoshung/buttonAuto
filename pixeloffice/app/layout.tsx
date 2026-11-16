import type { Metadata } from 'next';
import { Plus_Jakarta_Sans, Silkscreen, JetBrains_Mono } from 'next/font/google';
import './globals.css';

const plusJakarta = Plus_Jakarta_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-sans',
  display: 'swap',
});

const silkscreen = Silkscreen({
  subsets: ['latin'],
  weight: ['400', '700'],
  variable: '--font-pixel',
  display: 'swap',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  // dashboard.py 送出頁面時把 __TOKEN__ 換成這次啟動的存取碼（POST 時放在 X-Token 標頭）
  other: { 'aea-token': '__TOKEN__' },
  title: 'AI 工程助理｜像素辦公室',
  description:
    '即時監看 IDE、瀏覽器與開發工具工作現況的互動式像素辦公室，支援即時異常求助與人機協作介入。',
  openGraph: {
    title: 'PixelDesk 像素辦公室 — 跨工具即時工作監看與協作控制台',
    description:
      '即時監看 IDE、瀏覽器與開發工具工作現況的互動式像素辦公室，支援即時異常求助與人機協作介入。',
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'PixelDesk 像素辦公室',
    description:
      '即時監看 IDE、瀏覽器與開發工具工作現況的互動式像素辦公室。',
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="zh-Hant"
      className={`${plusJakarta.variable} ${silkscreen.variable} ${jetbrainsMono.variable} dark`}
    >
      <body
        className="bg-[#12141c] text-slate-100 font-sans antialiased selection:bg-amber-500/30 selection:text-amber-200"
        suppressHydrationWarning
      >
        {children}
      </body>
    </html>
  );
}
