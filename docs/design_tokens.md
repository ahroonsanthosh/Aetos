# Design tokens

`frontend/src/styles/tokens.css` is the single source of every colour, size, spacing and
duration in the interface. **A component that writes a literal colour is a bug.** This document
is what that rule means and why it is worth keeping.

Two files pointed here for months and this file did not exist. It does now.

---

## 1. The rule

One file holds the values; every component reads them through `var(--token)`. That is not
tidiness. It is what made it possible to move the entire interface — 31 components, 17
stylesheets — onto a new palette by editing one file, and what makes the next such change cost
the same.

The corollary matters as much: **a component that needs a colour the tokens do not have is
telling you the palette is incomplete.** Add the token. Do not write the hex.

```bash
# Should return nothing but tokens.css.
grep -rn "#[0-9a-fA-F]\{6\}" frontend/src --include="*.css" --include="*.tsx" | grep -v tokens.css
```

One exception: `frontend/src/components/landing/AetosHero.css` uses `#000` inside a CSS
`mask`, where only the alpha channel is read and the colour never shows.

## 2. Two colour families, and why they must not mix

This is the load-bearing idea in the palette.

| Family | Tokens | Job |
|---|---|---|
| **Brand** | `--brand-deep`, `--brand-mid`, `--brand-accent`, `--brand-wash` | **Identity and structure.** The mark, the active navigation item, focus rings, panel emphasis, the radial ground. They say *where you are*. |
| **Status** | `--status-warning`, `--status-elevated`, `--status-critical`, `--status-nominal` | **Judgement.** Something was assessed and found to warrant attention. |

An interface that uses colour freely for emphasis destroys the reflex that makes a warning colour
work: an operator who sees colour everywhere stops reading it anywhere. So:

- **Purple may be decorative. Purple may never mean "alarming."**
- **Amber and red may never be decorative.**

If you find yourself reaching for `--status-warning` because a heading needs to stand out, the
answer is `--brand-accent` or type weight, and the fact that you reached for amber is worth
noticing.

**Blue was removed.** It used to carry informational and nominal states. It read as a third
meaning nobody had assigned, so those states are plum, the brand colour: they are structure,
not judgement.

## 3. Contrast is a constraint, not a review step

Every content colour (the three text tokens, the brand accent, every status colour and the
default track colour) clears **WCAG AA, 4.5:1, against every surface** it can land on: void,
base, panel, raised and inset. `frontend/tests/unit/tokens.test.ts` reads `tokens.css` and
measures all fifty pairs, so a tweak that breaks one fails the build instead of shipping.

The status colours are much darker than they were on the old dark theme (critical is
`#ab2219`, warning `#8a5100`), because a light ground needs dark marks to carry the same
weight. `--text-disabled` is deliberately below the bar and is for **inactive controls only**,
never live content.

## 4. Type

| Token | Family | Used for |
|---|---|---|
| `--font-sans` / `--font-display` | Inter Tight | Navigation, body, the wordmark, most of every heading |
| `--font-serif` | Instrument Serif, italic | One accent word in a heading (`.serif`), small asides |
| `--font-mono` | IBM Plex Mono | All telemetry, identifiers, timestamps, log rows |

All three are open-source (SIL OFL) and **ship inside the site**: Inter Tight and Instrument
Serif through their `@fontsource` npm packages, bundled by Vite; Plex Mono under
`frontend/public/fonts`. Nothing is fetched from a font CDN, so the console renders with no
outbound network and nothing costs money.

Headings follow the reference site's mix: a small caps eyebrow, then a large title whose first
word is the italic serif (`PageHeader`). Anything that changes every update cycle carries
`.numeric`, so digits hold their width while they change.

## 5. Geometry and motion

Soft and rounded: cards at `--radius` (18px) and `--radius-lg` (28px), every button and chip a
pill (`--radius-pill`, the shared `.pill` class), and depth from one soft shadow
(`--shadow-soft`) rather than hairline boxes. A faint animated film grain sits over the whole
interface (`body::after`).

Motion is slow and eased (`--ease-out`, 200 to 1000 ms): pages rise and unblur into place,
heading words reveal in sequence, the full-screen menu opens like a curtain from its button, and
the word under the pointer swaps to the serif. Everything collapses to nothing under
`prefers-reduced-motion: reduce`, and **no information is carried by motion alone**.

## 6. Light, on purpose

The interface moved from a dark-only theme to a warm light one, after the calm editorial feel of
unseen.co. That was the "separate design pass" this section used to say a light theme would
need: every ratio was recomputed (and is now tested), and the status hues were darkened until
each stays readable and distinguishable on every surface.

The one dark region left is deliberate: the digital twin is a holographic 3D scene and sits in a
dark rounded window inside the light console, the way the reference site frames its 3D world.
