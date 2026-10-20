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

Two deliberate exceptions, both in `frontend/src/components/landing/`: the hero and launch
sequence use a handful of literal `#rrggbbaa` values inside gradients and blend layers, where an
alpha channel is part of the value and no token would be reused anywhere else. The landing page
is one screen with one composition; the console is a system.

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

**Blue was removed.** It used to carry informational and nominal states (`#4ba3d9`). Against an
eggplant ground it read as a third meaning nobody had assigned, so those states are now purple —
which is the accurate statement: they are structure, not judgement.

## 3. Contrast is a constraint, not a review step

Every text token clears **WCAG AA against the lightest surface it lands on**, which is
`--surface-raised`, not the page ground.

That distinction is not pedantry — it is how two successive versions of this palette shipped
failing. `--text-muted` carries `.label`, the 10px uppercase type used for every column heading
and section title, and column headings sit on panel headers:

| Attempt | Value | On base | On raised | Verdict |
|---|---|---|---|---|
| First | `#5c4f60` | 2.62:1 | 2.45:1 | Unreadable in a lit room |
| Second | `#84728a` | 4.54:1 | **4.26:1** | Passed the ground, failed where it is used |
| Shipped | `#8e7c93` | 5.21:1 | **4.51:1** | Passes |

`--status-critical` failed the same way at `#d9534f` (4.38:1 on a panel header) — the single
colour in the system that most needs to be readable. It is `#da5753`.

Both were caught by computing ratios, not by looking. Looking is unreliable on a near-black
ground, which is exactly where this interface lives.

**When adding or changing a text colour**, compute its ratio against `--surface-base`,
`--surface-panel` *and* `--surface-raised`, and take the worst. Body and small text need 4.5:1.
`--text-disabled` is held to 3.0:1 and is for **inactive controls only** — never for live
content. "NOT ASSESSED" is live content and uses `--text-muted`.

## 4. Type

| Token | Family | Used for |
|---|---|---|
| `--font-display` / `--font-sans` | Space Grotesk | Headings, navigation, wordmark, body |
| `--font-mono` | IBM Plex Mono | All telemetry, identifiers, timestamps, log rows |

Both are **vendored** under `frontend/public/fonts`, not linked from a font CDN: a console for a
defence site should not make a third-party request on load, and it has to render correctly with
no outbound network at all. 66 KB for all four faces, both SIL OFL.

Anything that changes every update cycle carries `.numeric`, which sets the mono family and
`font-variant-numeric: tabular-nums` so digits hold their width and the eye can hold a value
still while it changes.

Labels are letterspaced small caps rather than bold. That keeps weight available to mean
something.

## 5. Geometry and motion

Corners are square or nearly so (`--radius: 3px`). Rounding reads as consumer software, and this
is an instrument.

Motion is informational only — enough to notice a change, never enough to draw attention.
`--transition-fast: 90ms`, `--transition: 160ms`. Both collapse to `0ms` under
`prefers-reduced-motion: reduce`, and **no information in this interface is carried by motion
alone**, so nothing is lost when they do.

The landing page is the deliberate exception and is governed by its own rules; see
`frontend/src/components/landing/`.

## 6. Dark only

Stated as a decision, not a gap. The display is designed for low-light operations rooms, and the
status palette's contrast relationships are calibrated against a near-black ground. Re-deriving
them for a light background is a separate design pass, not a token swap — every ratio in §3 would
have to be recomputed and several hues would have to change to stay distinguishable.
