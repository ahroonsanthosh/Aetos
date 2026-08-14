# Radar bring-up

What to do the day the unit arrives, and — more importantly — what must be **verified** before
anything it reports is believed.

The adapter is built to be wrong in ways that do not announce themselves. A wrong speed scale
produces plausible targets at the wrong speed. An inverted direction flag produces tracks that
close when they are opening. A wrong angle bias produces a picture that looks like a
mis-aimed sensor rather than a decode error. **None of these raise, none of them fail a test,
and every one of them survives a demonstration that looks like it worked.** That is what this
procedure is for.

## Before you start

Everything in `sensors/adapters/radar/ld2451_protocol.py` under `WireFormat` was written from
documentation, not from a capture. Treat every constant as a hypothesis.

The framing logic around it is a different matter: it is tested against synthesised bytes and
is correct independently of those values. So the expected failure mode of a wrong constant is
not a crash — it is silence, or plausible nonsense.

## 1. Confirm the link before confirming the protocol

Read raw bytes with something that is not this codebase — `cat`, a serial terminal, anything —
and confirm bytes arrive at the configured baud. Two failures look identical from inside the
adapter and are worth separating first:

- **Nothing arrives.** Wrong port, wrong cable, unit not powered.
- **Bytes arrive but are garbage.** Wrong baud rate. At the wrong rate the bytes are real and
  meaningless, and the decoder will discard all of them as unframed noise.

Capture a minute of raw bytes to a file before going further. Every step below is cheaper
against a capture than against a live unit, and a capture can be replayed after a code change.

## 2. Find the frame boundary

In the capture, look for a repeating byte sequence at a regular stride. That is the header.

Compare it with `WireFormat.header`. If they disagree, the configured value is wrong and
**every frame will be discarded as garbage** — the adapter will report an empty sky while the
unit is transmitting normally. `FrameStats.bytes_discarded` climbing with
`frames_decoded` at zero is the signature, and it is asserted as a test
(`test_frames_of_one_format_are_not_decoded_by_another`) precisely so the symptom is known in
advance.

Confirm in the same pass: footer, the width and endianness of the length field, and the size
of one target record.

## 3. Verify the fields, one controlled observation at a time

Each of these needs a target whose true value you already know. Do them separately — a single
test that moves in several dimensions at once cannot tell you which constant is wrong.

| Field | How to verify | Failure if wrong |
|---|---|---|
| `distance` scale | Place a target at a measured 10 m, then 30 m, then 60 m | Range wrong by a constant factor; tracks form at the wrong place but move plausibly |
| `angle_offset` and sign | Target at boresight, then well left, then well right | Bearings mirrored or offset; the whole picture rotates or reflects |
| `speed_scale_mps` | Walk toward the unit at a known pace, or drive past at a known speed | Speeds wrong by a factor; risk scoring skews with it |
| `approaching_flag` | **Walk toward the unit, then away** | **Tracks close when they open.** The most consequential and least visible error here |
| `signal_strength` scale | Observe the same target near and far | Confidence mapping meaningless; weak returns rated as strong |

The direction flag deserves its own paragraph. There is no downstream check that catches an
inverted sign — every value stays in range and every track looks reasonable — and the
consequence is a system that reports a departing object as an incoming one. Verify it with a
person walking, in both directions, and watch `radial_velocity_mps` change sign.

## 4. Measure the uncertainty rather than copying it

`sensor_config.yaml` ships with starting values under `radar.uncertainty`. They are
placeholders.

Park a target at a fixed, measured position and record several hundred returns. The scatter of
those returns *is* the uncertainty:

- Standard deviation along the line of sight → `range_sigma_m`
- Standard deviation across it, at a known range → `cross_range_sigma_mrad`
  (divide the cross-range spread in metres by the range in metres, times 1000)
- `cross_range_sigma_m` is the floor that applies at short range, where the angular term goes
  to zero

Datasheet figures are almost always optimistic, and an optimistic covariance is worse for the
filter than an honestly pessimistic one: it makes the tracker confident in a direction the
sensor is not, which produces association errors that are very hard to diagnose later.

`vertical_sigma_m` cannot be measured, because the unit has no elevation channel. It is not a
measurement error but a statement of ignorance: a target is somewhere within the vertical beam
and nothing narrows it. Keep it of the same order as `max_range_m`.

## 5. Find the real detection envelope

`radar.coverage` also ships with placeholders. Establish, by observation:

- **`max_range_m`** — where detection actually becomes unreliable for the targets that matter,
  not what the datasheet claims for a car.
- **`min_speed_mps`** — the Doppler floor. Walk slowly toward the unit and find the speed below
  which it stops reporting. This is the single most important honesty parameter for this
  sensor: below it a target is invisible however close it is, and a hovering object produces
  nothing at all. Recording it is what lets that silence be reported as a limitation rather
  than mistaken for an empty sky (FM-010).
- **`horizontal_fov_deg`** — where a target at a known bearing stops being seen.

A radar of this class was designed for vehicle detection. A small, slow, low-reflectivity
target is at the edge of what it was built for, and the honest envelope may be considerably
smaller than the datasheet's. Better to know that now and configure it than to discover it
during an exercise.

## 6. Only then, trust it

Until steps 1-5 are done, the adapter produces plausible output. Plausible output that has not
been checked against a known truth is the most expensive kind of wrong, because it is the kind
people act on.

## Configuration touched by this procedure

All of it is in `sensors/config/sensor_config.yaml` under `radar:` — `serial`, `coverage`,
`uncertainty` and `signal` — except the wire-format constants, which are in
`sensors/adapters/radar/ld2451_protocol.py`. Those are a `WireFormat` value rather than
literals scattered through the parser, so correcting one is a data change and every existing
test still applies to the corrected format.
