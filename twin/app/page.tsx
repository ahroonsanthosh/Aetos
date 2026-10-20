'use client';

/**
 * The application is a single command view.
 *
 * WebGL and the interpolation buffer both need the browser, so the whole interface is
 * client-rendered. There is no server-rendered version of a hologram worth having, and
 * pretending otherwise would mean fighting hydration for no benefit.
 */

import dynamic from 'next/dynamic';

const CommandInterface = dynamic(
  () => import('@/ui/CommandInterface').then((m) => m.CommandInterface),
  {
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
        INITIALISING AETOS COMMAND
      </main>
    ),
  },
);

export default function Page() {
  return <CommandInterface />;
}
