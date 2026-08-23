# Bench procedure — from an unopened box to a measured coverage figure

Follow this in order. Each step ends with something you can check, and each one only makes sense
once the previous one has passed. If a step fails, the fault is almost always in that step or the
one before it — resist skipping ahead, because a wiring fault and a configuration fault produce
identical symptoms three steps later.

Set aside about ninety minutes for steps 1–7. Step 8 needs the drone and open space, and it is
the only one that produces a number nobody can give you.

---

## What you need

| | |
|---|---|
| The radar module | 24 GHz FMCW, 5 V, 3.3 V TTL UART |
| USB-to-TTL adapter | CP2102-based, **with a 3.3 V / 5 V logic-level switch** |
| Four jumper leads | Shortest you have. Length is not neutral — see step 7 |
| A computer | With the repository checked out and `pip install -e ".[dev,serial]"` done |
| A tape measure | For step 5. Twenty metres is plenty |
| The airframe | Step 8 only |

---

## Step 1 — Wire it, with the logic level set first

**Set the adapter's logic-level switch to 3.3 V before connecting anything.** The radar's UART
is 3.3 V. Driving its RX at 5 V is the one mistake in this procedure that can damage the module
rather than merely fail to work.

| Radar | Adapter | Note |
|---|---|---|
| `VCC` | **5V** | Not the 3.3 V pin. The module needs 5 V to run — only its *logic* is 3.3 V |
| `GND` | `GND` | Without a common ground nothing else matters |
| `TX` | `RX` | **Crossed.** Its transmit goes to the adapter's receive |
| `RX` | `TX` | Crossed the other way |

The two crossed lines are the most common error, and they fail silently: the port opens, and
nothing ever arrives.

**Check:** plug the adapter into USB. On Linux, `dmesg | tail` should mention a `cp210x`
converter. Nothing yet means a driver or cable problem, not a radar problem.

---

## Step 2 — Find the port

```bash
python -m sensors.adapters.ld2451.bench --list
```

Look for the one describing itself as a CP2102 or CP210x. Typically `/dev/ttyUSB0` on Linux,
`/dev/cu.usbserial-XXXX` on macOS, `COM3` or similar on Windows.

**On Linux, if the port is listed but opening it is denied**, you are not in the `dialout`
group:

```bash
sudo usermod -aG dialout "$USER"     # then log out and back in — a new shell is not enough
```

**Check:** you have a port name. Write it down; every following step uses it.

---

## Step 3 — Hear it talk

```bash
python -m sensors.adapters.ld2451.bench --port /dev/ttyUSB0
```

This is the first real test, and it deliberately uses **only** the wire codec and the serial
port — no site, no tracker, no database. If it works, the hardware and the protocol are fine and
everything after this is software you can debug at a desk.

Expect the baud probe to report `ok`, then lines like:

```
    25 m    +0 deg  ->  12 km/h  snr  70  [alarm]
```

Wave your hand across the front of the module, or walk past it. Something should appear.

### If nothing decodes

The tool prints the causes in order of likelihood. In practice:

1. **No power.** `VCC` on the adapter's 3.3 V pin instead of 5 V. Extremely common.
2. **TX/RX not crossed.**
3. **Logic switch still on 5 V.**
4. **No common ground.**
5. **A baud rate neither candidate covers.** Try `--baud 256000`, then others.

If it reports *bytes but no frames*, the wiring is right and the rate is wrong — that is the
signature, and it is why the tool distinguishes "silent" from "N bytes, no frames".

**Check:** targets appear when something moves in front of it, and stop when nothing does.

---

## Step 4 — Take a recording, now, while it is working

```bash
python -m sensors.adapters.ld2451.bench --port /dev/ttyUSB0 --record capture-clean.bin --duration 60
```

Walk around in front of it for the minute.

Do this before anything else goes wrong. A recording of the raw byte stream replays into the
decoder forever afterwards as a test fixture that never needs the hardware again, and a *clean*
one is the baseline you compare against the first time something looks odd. It costs a minute
now and is unobtainable later.

Note the summary's link integrity figures. On a good bench they should read 0.00% discarded and
0.00% rejected.

**Check:** you have `capture-clean.bin`, and you have written down the reject rate.

---

## Step 5 — Check the range figures against a tape measure

Stand at a measured distance — 5 m, 10 m, 20 m — directly in front of the module and move
slightly so it sees you.

The reported distance is in **whole metres**, so expect ±1 m of quantisation before any real
error. What you are checking is that 10 m reads about 10 and not about 3 or about 30. A gross
scale error would mean the field layout has been misread, and everything downstream would be
confidently wrong.

**Check:** reported range agrees with the tape to within a metre or two at each distance.

---

## Step 6 — Calibrate the azimuth sign

**This is the step people skip, and it is the one that silently mirrors your entire picture.**

The published protocol says the reported angle is the byte minus `0x80`. It does not say whether
a positive value means clockwise from boresight or anticlockwise. Get it wrong and a target on
your left is drawn on your right, at exactly the right range, with no other symptom whatsoever.

1. Note which way the module's front faces. Call that boresight.
2. Stand clearly off to **its right** as it faces you — about 15° off, a few metres out.
3. Move so it detects you, and read the angle sign in the bench output.

Then set `azimuth_sign` in `config/sensor_registry/ld2451_uart.json`:

| Standing to the module's right, the reported angle is | Set `azimuth_sign` to |
|---|---|
| **positive** | `1` |
| **negative** | `-1` |

Repeat on the left to confirm the sign flips. If it does not flip, you were not far enough off
axis — the module has a narrow beam and near-boresight readings hover around zero.

**Check:** you have set `azimuth_sign` deliberately, having seen the evidence, rather than left
it at its default.

---

## Step 7 — Measure the link, and improve it

Run the bench for five minutes and read the *Link integrity* block.

This protocol has **no checksum** (ADR 0018). Corruption that changes a frame's length is caught;
corruption that changes a value in place is caught only by the plausibility envelope, and about
21% of single-byte corruptions get through it. That figure is computed, not guessed, and it is
verified against the decoder by test.

So the reject rate is not cosmetic. It is the only observable estimate of the corruption you
*cannot* see, and the bench prints that estimate directly.

| Reject rate | What it means |
|---|---|
| 0.00% | Clean. Record it as your baseline |
| under 0.5% | Fine |
| 0.5–2% | Worth improving before you trust it |
| over 2% | Fix it. The adapter will report DEGRADED |

**What actually helps, in order of effect:**

1. **Shorter leads.** The single biggest factor. 10 cm beats 30 cm by more than anything else on
   this list.
2. **A solid common ground**, and only one.
3. **Distance from motors, switching power supplies and USB 3 ports.** All three are aggressive
   broadband sources.
4. **The lower baud rate.** 115200 rather than 256000 roughly halves the bit error rate on a
   marginal line, and you have update-rate headroom to spend.
5. **Shielded or twisted leads**, if you have them.

**Check:** reject rate under 0.5%, and the figure written down.

---

## Step 8 — Point Aetos at it

Now, and not before. Edit `config/sensor_registry/ld2451_uart.json`:

```jsonc
"driver_config": {
  "port": "/dev/ttyUSB0",     // yours, from step 2
  "azimuth_sign": 1,          // yours, from step 6
  ...
}
```

And edit `config/sites/bench.json` — **set `reference` to where you are actually standing.** The
sensor is placed at the site reference, so a reference in the wrong county puts your detections
in the wrong county. Set `boresight_deg` to the compass bearing the module faces.

Then check the configuration before running it:

```bash
python -m sensors.adapters.conformance --site bench --sensor radar_bench
```

This will not touch the hardware — it builds the adapter and checks the declaration. Expect
`conformant at TRACKABLE` with **one** gap: `D-3`, unvalidated coverage. That gap is step 9, and
it is the only thing software cannot close.

Then run the system:

```bash
python -m backend.main --site bench --mode live --no-scenario --no-auth
```

Open http://localhost:8000. The mode banner must read **LIVE**, and the sensors page must show
`radar_bench` as `REAL` and `RUNNING`. Walk in front of the module and watch a track form.

**Expect the altitude to be shown as an assumption, not a number.** The module has no elevation
channel, so every detection is placed in the sensor's own horizontal plane and marked `assumed`,
with about 15 m of vertical uncertainty at 100 m (ADR 0019). If the interface shows you a
confident altitude figure, that is FM-017 and worth reporting.

**Check:** LIVE banner, a real sensor running, and a track that follows you.

---

## Step 9 — Measure what it can actually see

Everything up to here has tested that the software reads the device correctly. This step tests
something nobody else can tell you, and it is the reason `coverage_validated` is `false`.

**The rated 100 m is quoted against a car.** A small multirotor has a radar cross-section one to
two orders of magnitude smaller. Whether this module detects your airframe at all, at any range,
is not established by buying it.

Until it is measured, silence from this sensor must not be read as clear sky (FM-016).

### The measurement

1. Mark ground positions at 10, 20, 30, 40, 60, 80 and 100 m from the module, on boresight.
2. Fly the airframe to a hover at each mark, at a realistic operating height — 5 m, then 15 m.
3. At each point, record for 60 seconds with `--record` and note **how many of those seconds
   produced a detection**. That fraction is your probability of detection at that range.
4. Repeat off-boresight at ±15° for at least two ranges, to find where the beam falls away.

### What to do with the result

Write the range at which detection probability falls below about 0.5 into
`coverage.max_range_m`, replacing the datasheet figure, and set `coverage_validated` to `true`.
That is now a measured claim rather than a manufacturer's.

**If the answer is that it barely sees the drone at all**, that is a real and valuable result,
obtained for the price of an afternoon rather than a deployment. It means this module is a
vehicle sensor and the airborne threat needs different hardware — which is exactly the kind of
thing the whole capability-declaration mechanism exists to surface early. Record the measured
figure honestly and let the conformance report say what it says.

---

## Afterwards

- Keep every recording. They are test fixtures.
- Re-run step 7 whenever anything about the physical setup changes. The reject rate is the
  earliest warning that a link has begun to lie.
- The rotating mount is Phase 7, and there is a constraint worth knowing before buying a motor:
  a *tentative* track is retired after two missed cycles, so a sweep with a revisit interval
  slower than about a second can hold tracks handed to it but will never originate one. See
  `docs/sensor_interface_specification.md` §5.
