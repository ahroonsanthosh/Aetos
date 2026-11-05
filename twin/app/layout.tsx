import type { Metadata, Viewport } from 'next';
import '@fontsource/barlow/400.css';
import '@fontsource/barlow/500.css';
import '@fontsource/barlow-condensed/500.css';
import '@fontsource/barlow-condensed/600.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import './globals.css';

export const metadata: Metadata = {
  title: 'Aetos Command',
  description:
    'Holographic digital twin command interface for the Aetos airspace security platform. Prototype running on simulated data.',
};

export const viewport: Viewport = {
  themeColor: '#090609',
  // The interface is a fixed command display; it must not rubber-band or zoom.
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-GB">
      <body>{children}</body>
    </html>
  );
}
