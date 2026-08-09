# `components/response`

## Responsibility

The Response Systems page: architecture and interface only.

## Interface boundary

**This directory contains no capability and must never contain one.**

Every card is permanently `NOT CONNECTED`. There is no activation control, no command, and no
code path from here to any external system. The page exists so that adding response
integration later is an extension of the architecture rather than a redesign of it.

The frontend never talks to the repository's root-level `response/` directory. Everything
routes through `backend/decision/` and stops there.

Response execution is a genuine future capability for this project, not a permanently disabled
feature — but it requires its own authorisation, safety and audit design pass before any code
is written. See [`response/README.md`](../../../../response/README.md) at the repository root,
which also notes that Aetos currently has no authentication at all (FM-001), and that this
alone disqualifies the layer from being built today.
