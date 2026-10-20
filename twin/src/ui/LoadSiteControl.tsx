'use client';

/**
 * Load a site file directly into the running command view — no editor round trip needed
 * when the file already exists (exported earlier, checked into a repo, handed over by a
 * colleague). The editor's "Preview in Command" handoff is for the moment of building a
 * site; this is for every moment after, including opening someone else's export cold.
 */

import { useRef, useState } from 'react';

import { parseSiteFileText, SiteFileError } from '@/site/schema';
import { useAetos } from '@/state/store';
import styles from './loadsite.module.css';

export function LoadSiteControl() {
  const setSite = useAetos((s) => s.setSite);
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);

  const onFile = async (file: File) => {
    setError(null);
    try {
      const text = await file.text();
      setSite(parseSiteFileText(text));
    } catch (err) {
      setError(err instanceof SiteFileError ? err.message : 'Could not read that file.');
    }
  };

  return (
    <div className={styles.control}>
      <button type="button" className={styles.button} onClick={() => inputRef.current?.click()}>
        Load site…
      </button>
      <a className={styles.button} href="/editor">
        Site editor
      </a>
      <input
        ref={inputRef}
        type="file"
        accept="application/json,.json"
        className={styles.hiddenInput}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void onFile(file);
          e.target.value = '';
        }}
      />
      {error && <span className={styles.error}>{error}</span>}
    </div>
  );
}
