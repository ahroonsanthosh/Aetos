# Bundled typefaces

Every face ships with the site rather than being linked from a font CDN. A console for a
defence site should not make a third-party request on load, and it has to render correctly with
no outbound network at all. They are installed as npm packages and bundled by Vite at build time;
nothing is fetched at runtime.

| Package | Family | Licence |
|---|---|---|
| `@fontsource/barlow` | Barlow, by Jeremy Tribby | SIL Open Font License 1.1 |
| `@fontsource/barlow-condensed` | Barlow Condensed, by Jeremy Tribby | SIL Open Font License 1.1 |
| `@fontsource/jetbrains-mono` | JetBrains Mono, by JetBrains | SIL Open Font License 1.1 |

The full OFL text is at <https://openfontlicense.org>.
