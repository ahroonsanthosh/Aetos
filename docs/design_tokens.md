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
`--text-disabled` is deliberately below the bar and is for **inactive controls only**.

## 4. Type

| Token | Family | Used for |
|---|---|---|
| `--font-display` | Barlow Condensed | Headings, the wordmark, labels (uppercase, letterspaced) |
| `--font-sans` | Barlow | Body text, navigation, buttons |
| `--font-mono` | JetBrains Mono | Every number that changes, identifiers, timestamps |

All three are open-source (SIL OFL) and **ship inside the site** through their `@fontsource`
npm packages, bundled by Vite. Nothing is fetched from a font CDN and nothing costs money.

## 5. Geometry and motion

Rounded cards (`--radius`, `--radius-lg`), pill buttons, one soft shadow. Motion is limited to
hover and state transitions on controls. There are no entrance animations, reveals, film grain
or boot sequences: decoration that carries no information was removed on purpose.

## 6. Dark, with the brand plum

Near-black surfaces warmed toward the eagle's plum, light type, and plum for structure (the
active page, focus, the mark). Status colours are the only other saturated values.
