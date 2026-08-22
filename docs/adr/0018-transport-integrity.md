# ADR 0018 — Transport integrity on links that have none

**Status:** Accepted
**Date:** 2026-08-22

## Context

A great deal of affordable sensor hardware speaks a framed binary protocol over a raw serial
line with **no checksum of any kind** — a magic header, a length field, a payload, a magic
tail, and nothing else. The first live driver written for Aetos targets exactly such a device.

This is a different and worse problem than a noisy link that fails loudly. Consider what
corruption does to such a stream.

**Corruption that changes the frame's length or alignment is detected**, because the tail will
not be where the length field says it is. The decoder discards and resynchronises. A frame is
lost, which is benign: the sensor appears to have missed one update, and the tracker already
handles that.

**Corruption that changes a payload byte in place is not detected at all.** Header, length and
tail still validate. A single bit flip turns a target at 40 m (`0x28`) into a target at 168 m
(`0xA8`), and the frame is structurally perfect. Nothing in the protocol can tell the difference,
and nothing downstream has any basis to doubt it.

On a counter-UAS picture that is the worst available failure: a phantom target, at a plausible
range, indistinguishable from a real detection, arriving through a source the operator has been
told to trust. It is not hypothetical — 3.3 V TTL through a cheap USB bridge, over an
unshielded jumper lead, next to a motor, is a link that will corrupt bytes.

The instinct is to add a checksum. That instinct is wrong, and it is worth being explicit about
why: **integrity cannot be retrofitted onto a one-way stream by the receiver.** There is no
back-channel to request retransmission, no spare field to carry a CRC, and no way to make the
transmitter compute something it does not compute. Whatever is done has to be done with the
information already in the frame.

## Decision

Five parts. The first three are what the decoder does; the last two are what makes it honest.

### 1. Adapters declare the integrity their transport provides

`verified` (a checksum or equivalent exists at or below this layer — TCP, TLS, a protocol with a
CRC), `structural` (framing and value plausibility only), or `none` (no framing at all).

A `structural` link is admissible. Treating one as though it were `verified` is not. The
declaration is what makes the difference visible rather than assumed, and it is part of
`SensorCapabilities` (ADR 0017).

### 2. Deterministic structural validation, then a configured plausibility envelope

Structural validation rejects everything whose length or alignment is inconsistent: header
exact, payload length exactly consistent with the declared target count, tail present at the
computed offset. This catches every length-changing corruption with certainty.

Value corruption is then caught only by the **unused code space in each field**. A direction byte
with two legal values rejects 254 of 256 possibilities; an angle byte on a sensor with a ±20°
aperture rejects everything outside a narrow band around centre; a distance byte rejects
everything past the configured maximum range. The plausibility envelope is not a heuristic
filter bolted on afterwards — on a checksumless protocol it *is* the integrity check, and it is
the only one available.

The envelope comes from configuration, never from constants in the decoder, for a reason that
falls out of §3: it is the operator's lever on link integrity.

### 3. The residual undetected-corruption probability is computed, not estimated

Because the envelope is the integrity check, the probability that corruption survives it is a
property of the frame layout and the configured bounds — so the decoder calculates it rather
than anyone guessing.

Under a single-byte corruption model, for each byte position `i` in the frame:

```
p_undetected(i)  =  (legal_values(i) − 1) / 255
residual         =  mean over all byte positions of p_undetected(i)
```

Worked for the first live driver's frame layout — a 4-byte header, 2-byte length, count, alarm,
five bytes per target, 4-byte tail — carrying three targets, with a ±25° aperture, 100 m maximum
range and a 200 km/h speed ceiling:

| Field | Legal values | `p_undetected` |
|---|---|---|
| header, length, count, tail (11 bytes) | 1 each | 0.000 |
| alarm | 2 | 0.004 |
| angle | 51 | 0.196 |
| distance | 101 | 0.392 |
| direction | 2 | 0.004 |
| speed | 201 | 0.784 |
| SNR | 101 | 0.392 |

Residual over the 27-byte frame: **0.197**. About four in five single-byte corruptions are
caught; one in five is not.

**This figure is verified, not asserted.** `tests/unit/test_ld2451_protocol.py` enumerates every
single-byte corruption of a three-target frame — every byte position against all 255 alternative
values, 6,885 cases — feeds each to the decoder, and compares the fraction accepted against the
computed prediction. The two agree to four decimal places. A declared integrity figure that did
not describe the decoder would be worse than none, and this is the test that stops the two
drifting apart.

The figure the adapter actually declares is the **worst case over frame size**, not the
three-target case above: the fully-checked delimiter bytes are a fixed cost per frame, so their
protective share shrinks as targets are added. For the shipped configuration — ten targets
maximum, 80 km/h ceiling — that is **0.209**.

Two things follow, and both are useful.

**Configuration is an integrity control.** Narrowing the speed ceiling from 200 km/h to a value
the site actually expects — 80 km/h — takes the residual from 0.197 to **0.144**. Declaring a
tighter envelope is not merely a filter; it measurably improves the trustworthiness of the link.
This is worth telling an integrator, and it is not something a hand-waved "we validate the
values" would ever have surfaced.

**The number is conservative for the realistic failure mode.** Single-byte replacement is the
model, but the common UART failure is a framing error — a missed start bit — which shifts the
entire remaining stream and is caught structurally with near-certainty. The declared residual is
therefore a worst case for the errors that actually happen, which is the correct direction for a
safety-relevant figure to be wrong in.

### 4. The observable rejection rate is an estimator of the unobservable corruption rate

This is the part that turns an undetectable failure into a detectable one.

Undetected payload corruption cannot be observed directly — by definition. But it has the same
physical cause as structural corruption: a failing cable, a wrong baud rate, electrical
interference, a buffer overrun. Structural corruption *is* observable, and with a known residual
the two are quantitatively linked:

```
estimated undetected rate  ≈  observed reject rate × residual / (1 − residual)
```

So the decoder counts every rejection, by reason, and the adapter surfaces those counts in
`health()`. A rising rejection rate is the earliest available signal that the link has begun to
lie, and it arrives *before* anyone has reason to doubt a specific detection.

Every rejection is counted. None is silent. A decoder that quietly discards malformed frames
reports a healthy sensor on a failing cable, which is FM-010 arriving through the transport
layer.

### 5. Degrade on evidence, and prefer silence to fabrication

Past a configured rejection rate the adapter reports `DEGRADED` with the measured rate as its
reason; past a second threshold it reports `OFFLINE` and emits nothing.

Reporting OFFLINE rather than continuing at reduced confidence is deliberate. **A source that is
probably corrupting data is worse than a source that is quiet.** A quiet sensor produces coasting
tracks and a visible gap in coverage, both of which the system already handles honestly. A
lying sensor produces confident phantom targets, which it does not.

### 6. Confirmation logic stays in the tracker

An obvious-looking mitigation is to require a target to appear in N consecutive frames before
emitting it. This is rejected: `TrackManager` already does exactly that, with
`confirm_after_detections` and the tentative-track retirement rule, sized against the
association gate and the site's clutter model.

Duplicating it in the adapter would suppress single-frame detections before the tracker ever
sees them, which is not the adapter's decision to make — it would silently raise the effective
detection threshold, degrade the sensor's real sensitivity, and hide the change from the
conformance suite. The adapter reports what it observed; deciding what a sequence of
observations means belongs downstream of ingestion.

For the same reason the decoder does **not** filter temporally implausible returns. A target
genuinely can appear anywhere on the first frame it is seen, and rejecting returns that are
kinematically unreachable from the previous frame is association — the tracker's job, done with
the tracker's covariance information, which the decoder does not have.

## Rationale

The honest framing of this problem is that it cannot be solved, only bounded and measured. There
is no arrangement of receiver-side logic that makes a checksumless link reliable.

What can be done is to make the residual risk **explicit, computed, and observable** rather than
implicit, unquantified and invisible — and then to let the system draw a conclusion from
evidence rather than from optimism. That is the same shape as the rest of Aetos: uncertainty is
propagated honestly rather than assumed away, and the operator is shown what is actually known.

Points 4 and 5 are what make this more than documentation. A residual probability written in a
document is a caveat; a residual probability used to convert a measured rejection rate into a
degraded sensor state is a mechanism.

## Consequences

- The decoder for such a protocol is a stateful accounting object, not a stateless parse
  function. It owns rejection counts by reason and byte-level accounting, and that is part of its
  interface rather than a debug facility.
- The plausibility envelope must be configuration, and its documentation must explain that
  tightening it improves integrity. An integrator who leaves it at maximum permissiveness gets a
  measurably worse link than one who declares real bounds.
- Sites deploying such hardware should be told the physical mitigations, because they dominate:
  short shielded leads, a solid common ground, distance from motors and switching supplies, and
  the **lowest** baud rate that meets the update-rate floor. Halving the baud rate roughly halves
  the bit error rate on a marginal line, and D1 in the interface specification says how much
  headroom there is to spend.
- Aetos can now state, per sensor and with a number, how much it trusts the wire. That is a
  capability no prose integrity claim provides.

## Alternatives considered

- **Treat the link as reliable and move on.** What the hardware's own reference code does.
  Rejected: it produces confident phantom targets with no mechanism for anyone to notice.
- **Require a checksum, and refuse hardware without one.** Clean. Rejected: it excludes most of
  the affordable sensor market and directly contradicts the point of ADR 0017. The market
  reality is checksumless serial protocols, and a specification that refuses them is a
  specification for hardware nobody is bringing.
- **Wrap the link in a checksummed carrier — a microcontroller between the sensor and the host
  that adds a CRC.** Genuinely effective, and the right answer for a production build. Rejected
  *here* because it moves the trust boundary rather than removing it: the uncorroborated hop
  still exists between the sensor and the microcontroller, and the decoder would need every
  mitigation above regardless. Recorded as a hardening option, not a substitute.
- **N-of-M confirmation inside the adapter.** Rejected under §6 — it is the tracker's job, and
  doing it twice degrades sensitivity invisibly.
- **Rejecting frames whose summary flag contradicts their per-target directions.** This looked
  like a free cross-check — two encodings of one fact — and was implemented as a rejection
  before being changed. The flag's semantics are documented only in passing and are unconfirmed
  against a physical unit, so if it in fact means "a target is present", or latches across
  frames, rejection would silently discard frames full of individually plausible targets.
  Losing real detections is the one failure this system must not have, and it is strictly worse
  than admitting a corrupted flag nothing downstream reads. It is now counted as an integrity
  signal instead, where a rising count is visible and can be settled against the hardware.
- **Statistical anomaly detection over the target stream.** Rejected: it requires a model of
  normal that a sensor with unvalidated coverage does not have, and its false-positive mode is
  suppressing real detections, which is the one failure this system must not have.
