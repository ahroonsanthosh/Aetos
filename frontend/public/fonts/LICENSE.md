# Bundled typefaces

Every face ships with the site rather than being linked from a font CDN. A console for a defence site should
not make a third-party request on load, and it has to render correctly with no outbound network
at all.

| File | Family | Licence |
|---|---|---|
| `ibm-plex-mono-{400,500}.woff2` | IBM Plex Mono, by Mike Abbink / Bold Monday for IBM | SIL Open Font License 1.1 |
| (npm) `@fontsource-variable/inter-tight` | Inter Tight, by Rasmus Andersson | SIL Open Font License 1.1 |
| (npm) `@fontsource/instrument-serif` | Instrument Serif, by Instrument | SIL Open Font License 1.1 |

The two npm packages are bundled by Vite at build time; nothing is fetched at runtime.

Latin subsets only. The full OFL text is at <https://openfontlicense.org>.
