# ADR 0005 — Global Nearest Neighbour association with Mahalanobis gating

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

Spec §7 scenario H requires that when two objects' paths cross, the system must not swap
their identities. Scenario D and I require stable track IDs under multiple simultaneous
objects and under saturation.

The obvious implementation — for each detection, attach it to the closest track — is greedy
nearest neighbour. It does not merely perform poorly at crossings; it fails there
**by construction**. At the moment of crossing, each detection is genuinely closest to the
wrong track, and greedy assignment takes the bait. Scenario H would be unpassable.

The specification does not say which association algorithm to use.

## Decision

Association is **Global Nearest Neighbour (GNN)**:

1. For each (track, detection) pair, compute the **Mahalanobis distance** between the
   detection and the track's predicted measurement, using the innovation covariance from
   the Kalman filter.
2. **Gate**: reject any pair whose Mahalanobis distance exceeds a chi-squared threshold at a
   configured confidence. Gated-out pairs cannot be assigned at any cost.
3. Build a cost matrix over surviving pairs and solve the assignment **globally** with the
   Hungarian algorithm (`scipy.optimize.linear_sum_assignment`), minimising total cost
   across all pairs simultaneously rather than choosing greedily one at a time.
4. Unassigned detections become candidate new tracks. Unassigned tracks coast.

## Rationale

Mahalanobis distance rather than Euclidean because it weights each axis by how uncertain the
filter actually is in that axis. A track that has been coasting has a large predicted
covariance and should accept detections further away; a well-observed track should not.
Euclidean distance cannot express that, and it is exactly the information that disambiguates
a crossing.

Global assignment rather than greedy because the crossing case is defined by two locally
optimal choices being jointly wrong. Minimising total cost across the whole matrix picks the
jointly consistent interpretation. Combined with velocity state in the filter — the two
objects are moving in different directions even where their positions coincide — this is
what preserves identity through the crossing.

## Consequences

- Association is O(n³) in the Hungarian solve. At the V0 ceiling of 20 tracks this is
  microseconds. It is not a concern until track counts are orders of magnitude higher.
- GNN still assumes each detection belongs to at most one track and each track receives at
  most one detection per cycle. Under dense clutter or genuinely merged returns it can
  still make the wrong call.
- The upgrade path when that becomes binding is JPDA (probabilistic association across all
  gated candidates) or MHT (deferred decision across multiple hypotheses). Both are strictly
  more capable and strictly more expensive. Neither is justified at V0. The association
  module is kept behind an interface so either can be swapped in without touching the track
  manager.
- Scenario H is the acceptance test for this decision, not a nice-to-have. If it regresses,
  this ADR is what failed.
