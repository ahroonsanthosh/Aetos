import type { Metadata, Viewport } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Aetos Command',
  description:
    'Holographic digital twin command interface for the Aetos airspace security platform. Prototype running on simulated data.',
};

export const viewport: Viewport = {
  themeColor: '#04070b',
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
