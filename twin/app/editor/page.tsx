'use client';

/**
 * The site editor route, separate from `/` — building a site and running the command view
 * against one are different tasks with different tools, and keeping them on different
 * routes means neither's state has to account for the other running at the same time.
 */

import dynamic from 'next/dynamic';

const SiteEditor = dynamic(() => import('@/ui/editor/SiteEditor').then((m) => m.SiteEditor), {
  ssr: false,
  loading: () => (
    <main
      style={{
        display: 'grid',
        placeItems: 'center',
        height: '100%',
        color: '#2b6b78',
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        letterSpacing: '0.24em',
      }}
    >
      LOADING SITE EDITOR
    </main>
  ),
});

export default function EditorPage() {
  return <SiteEditor />;
}
