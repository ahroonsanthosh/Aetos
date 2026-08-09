# Design tokens

Every colour, size and spacing value in the interface comes from `src/styles/tokens.css`.
A component that writes a literal colour is a bug.

## The palette is almost entirely achromatic, on purpose

Surfaces run from near-black to panel grey; type runs white through to disabled grey. The only
saturated colours in the system are status colours.

**Colour is a status signal, never decoration.** If amber or red appears on screen it means
something, and an operator learns to trust that. An interface that uses colour freely for
emphasis destroys exactly that reflex — which is the only thing a warning colour is for.

## Surfaces

| Token | Use |
|---|---|
| `--surface-void` | Behind everything; the map's ground |
| `--surface-base` | Application background |
| `--surface-panel` | Panels and cards |
| `--surface-raised` | Panel headers, hover states |
| `--surface-inset` | Wells, log rows, inputs |

Structure comes from hairlines (`--line-subtle`, `--line`, `--line-strong`), not from shadows
or rounding. Corners are square or nearly so; rounding reads as consumer software.

## Status colours

| Token | Meaning |
|---|---|
| `--status-nominal` | Healthy, connected |
| `--status-info` | Normal sensor data, informational |
| `--status-warning` | Attention required |
| `--status-elevated` | Escalating |
| `--status-critical` | Severe |
| `--status-inert` | **Not built, not connected** — deliberately colourless |

`--status-inert` matters. An unbuilt module must not be green, and it must not be red either —
it is not broken, it does not exist. Giving it a desaturated grey of its own means the
dashboard can say so without implying either a fault or a capability.

## Type

Two families: `--font-sans` for prose, `--font-mono` for anything numeric. Numbers that change
every update cycle use `.numeric`, which is tabular — digits keep their width so the eye can
hold a value still while it changes.

The scale is small and dense (10px–22px). This is an instrument, not a landing page.

Labels are letterspaced small caps rather than bold. That is the convention of instrumentation
panels, and it keeps font weight available to mean something.

## Spacing and geometry

4px base scale, `--space-1` through `--space-8`. Layout dimensions (`--topbar-height`,
`--sidebar-width`, `--details-width`) are tokens too, so the shell's proportions are adjustable
in one place.

## Motion

`--transition-fast` (90ms) and `--transition` (160ms). Enough to let the eye follow a change,
never enough to draw attention. There is no decorative animation anywhere.

`prefers-reduced-motion` disables all of it — motion here is informational, so a user who has
asked for less of it loses nothing.

## Dark only, deliberately

A light theme is not a missing feature. The display is designed for low-light operations
rooms, and the status palette's contrast relationships are calibrated against a near-black
ground. Re-deriving them for a light background is a design pass, not a token swap.
