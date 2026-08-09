# ADR 0011 — The interface renders only what the backend actually knows

- **Status:** Accepted
- **Date:** 2026-08-09

## Context

The Aetos frontend specification describes a complete operator interface: classification
confidence distributions, threat assessments, an alert feed, sensor runtime health, a system
health dashboard.

The backend does not yet produce most of that. Classification, risk, alerts and the
system-health watchdog are not implemented. `ThreatView` exists as a wire shape and reports
zero for every track; the alert feed is empty because nothing generates alerts.

There is an obvious temptation here, and it is worth naming: a demonstration interface looks
far more impressive with a classification bar reading `UAV 94% / BIRD 3% / UNKNOWN 3%` than
with a panel saying classification is not implemented. The specification itself asks for that
bar.

## Decision

**The interface renders only what the backend actually knows, and says so plainly where it
knows nothing.**

Concretely:

1. **No fabricated measurements.** A field the backend did not supply renders as `—`. Never
   interpolated, never defaulted to a plausible number.
2. **No fabricated inference.** Classification shows the single label and confidence the
   backend supplies. It does not synthesise a multi-class distribution, because no such
   distribution is computed. When the classification module produces one, the component
   renders it.
3. **Unbuilt subsystems are labelled unbuilt.** The system health dashboard reports
   `NOT_BUILT` for fusion, classification, risk, alerts and the watchdog, sourced from
   `GET /system/health`. It does not omit them, which would make "healthy" and "forgotten"
   indistinguishable, and it does not show them green.
4. **Empty means empty.** An empty alert feed reads as "no alerts — alert generation is not
   implemented", not as a quiet blank that could be mistaken for "all clear".
5. **Simulated versus real is a backend fact.** The mode indicator reads
   `GET /system/mode`, which computes `simulated` from whether any real adapter is actually
   feeding the pipeline, rather than trusting the configured mode string. A frontend
   constant would let synthetic data be presented as real by editing one line.

## Rationale

This is a system whose entire output is a judgement an operator is asked to trust. An
interface that fabricates plausible values to look complete is not a cosmetic problem — it
trains an operator to believe a display that is sometimes lying, and there is no way for them
to tell which parts. The failure is worst precisely when the system is most degraded, which
is when it matters most.

The specification's own rules already say this ("never fabricate a sensor measurement",
"synthetic data must never be presentable as real"). This ADR extends the same rule from
measurements to inference and to component status, because the reasoning is identical and the
gap is where it would otherwise be exploited.

A panel that honestly says "not implemented" is also better product feedback than one that
looks finished: it makes the remaining work visible instead of hiding it behind a mock.

## Consequences

- The interface will look less complete than the specification's screenshots imply, until the
  assessment modules are built. That is an accurate depiction of the system's current state.
- Every component that renders backend data needs an explicit empty state and an explicit
  not-implemented state. This is more work than a component that assumes data is present, and
  it is the work that makes the interface trustworthy.
- Components are written against the *finished* backend contract, so when classification and
  risk start producing real output the panels populate without redesign — the not-implemented
  state simply stops being reached.

## Alternatives considered

- **Mock the missing data for demonstration purposes.** More impressive immediately.
  Rejected: it produces a system that shows confident-looking assessments derived from
  nothing, which is the exact failure the whole architecture is built to avoid.
- **Hide unbuilt panels entirely.** Cleaner-looking. Rejected: a hidden subsystem cannot be
  distinguished from a forgotten one, and the navigation is meant to show the shape of the
  finished system.
