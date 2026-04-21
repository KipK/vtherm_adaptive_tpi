# Adaptive TPI Architecture

## Work In Progress

This document describes the current implementation state of `vtherm_adaptive_tpi`.

It is not a frozen specification. The project is still evolving, and some details may change as the learning logic is refined on real telemetry.

## High-Level Flow

The plugin is attached to Versatile Thermostat through `vtherm_api` and reacts to two kinds of events:

1. regular regulation refreshes
2. scheduler cycle boundaries

At a high level:

1. `calculate()` computes the current `on_percent`
2. the VT cycle scheduler commits a real cycle
3. the plugin records the cycle start context
4. at cycle end, the plugin decides whether the cycle is valid for learning
5. if valid, it updates:
   - deadtime search
   - regime-routed OFF learning for `b`
   - regime-routed ON learning for `a`
6. projected gains `k_int` and `k_ext` are refreshed conservatively

## Main Modules

### `handler.py`

Integration-facing glue with Home Assistant and Versatile Thermostat.

Responsibilities:

- initialize the adaptive algorithm
- bind scheduler callbacks
- save and restore persistent state
- publish diagnostics in the climate `specific_states`

### `algo.py`

Runtime orchestration layer.

Responsibilities:

- compute `on_percent`
- capture committed cycle data
- validate learning conditions
- route cycles toward deadtime, `b`, or `a`
- update public runtime state
- refresh projected gains

### `adaptive_tpi/deadtime.py`

Coarse deadtime estimator using a time-to-first-rise method.

Responsibilities:

- keep a temporally contiguous cycle history
- detect rising power edges (OFF→ON transitions) and measure the delay
  until the first visible temperature rise (`RISE_EPSILON` cumulative or
  `RISE_EPSILON_STEP` per cycle)
- aggregate identifications via weighted median over the last `N_HIST`
  events and lock when spread (in cycles) and quality conditions are met
- expose:
  - `nd_hat`
  - `c_nd`
  - best and second-best candidates
  - a deadtime-side proxy for `b`

Important note:

- deadtime search uses all real cycles kept in aligned history
- some cycles are valid for history but not informative for scoring

### `adaptive_tpi/learning_window.py`

Builds short bounded learning windows from real cycle history.

Responsibilities:

- reconstruct recent OFF windows for `b`
- reconstruct recent ON windows for `a`
- anchor windows on the current completed cycle
- enforce short bounded windows
- reject windows when:
  - the signal is too weak
  - the regime sign is inconsistent
  - a recent setpoint change contradicts the regime
  - the window still intersects the post-transition deadtime blackout

The learning blackout currently depends on deadtime:

- blackout of `ceil(nd_hat)` cycles after a regime transition
- with a minimum safety blackout of `1` cycle when deadtime is not yet known

The setpoint-jump guard is regime-oriented:

- ON windows tolerate upward setpoint jumps that reinforce heating
- OFF windows tolerate downward setpoint jumps that reinforce the current no-heat regime
- contradictory jumps still invalidate the window

### `adaptive_tpi/estimator.py`

Decoupled estimators for `b` and `a`.

Responsibilities:

- learn `b` from OFF or quasi-OFF windows
- learn `a` from ON windows
- keep bounded estimates and confidence values
- expose sample counts and last rejection reasons

Current design choice:

- the estimator uses a bounded rolling robust estimator
- this is intentionally simpler than a more aggressive online LMS/RLS approach

### `adaptive_tpi/controller.py`

Gain projection and command computation.

Responsibilities:

- derive structural gain targets from `a_hat`, `b_hat`, and `nd_hat`
- project gains slowly with bounded rate limits
- compute the nominal `on_percent`

### `adaptive_tpi/startup_bootstrap.py`

Startup command override used before the first deadtime identification.

Responsibilities:

- force a clean startup sequence around setpoint
- keep the sequence bounded to at most two OFF->ON identification attempts
- expose the detailed startup-bootstrap diagnostics consumed by the runtime state

## Learning Sequence

### 1. Cycle start

When VT starts a real cycle, the plugin captures:

- target temperature
- indoor temperature
- outdoor temperature
- applied power
- hvac mode

This snapshot becomes the pending cycle context.

Before deadtime exists, the runtime may temporarily bypass the nominal P+feedforward
command and use the startup bootstrap sequence instead:

- if `current_temp >= target_temp`, command `0%` until `target_temp - 0.3°C`
- if `current_temp < target_temp`, command `100%` until `target_temp`
- once the room reached `target_temp`, command `0%` until `target_temp - 0.3°C`
- from `target_temp - 0.3°C`, command `100%` until `target_temp`
- every bootstrap threshold crossing forces an immediate scheduler restart so the current cycle can end without waiting for its nominal boundary
- if no deadtime identification was produced, repeat one more OFF->ON cycle
- after the second failed attempt, return to nominal regulation
- the OFF cooldown cycles created by this sequence may also feed the first `b` updates, even when they start near setpoint

### 2. Cycle end

At the end of the cycle:

- realized power is recorded
- interrupted cycles are rejected from learning
- accepted cycles are added to the deadtime model history

### 3. Deadtime update

The deadtime model evaluates the candidate set and updates:

- `nd_hat`
- `c_nd`
- `deadtime_locked`
- candidate costs

It also exposes a temporary `b` proxy from the best candidate fit.

### 4. Window extraction

The algorithm first classifies the completed cycle into a coarse regime:

- `off`
- `on`
- `mixed`

It then builds one anchored window for that same completed cycle:

- one OFF window for `b`
- or one ON window for `a`

The runtime no longer searches the full history for whichever regime happens to be available first.
The current completed cycle decides the learning route.

### 5. Estimation

Routing logic is:

- `b` may learn from OFF windows routed from an OFF completed cycle
- `a` waits for:
  - credible deadtime
  - converged `b`
- both `a` and `b` are blocked while the candidate window still lies in the deadtime blackout after a regime transition
- mixed cycles do not feed `a` or `b`

The deadtime-side `b` proxy is also used as a light bootstrap seed for the explicit `b` estimator when no OFF sample has been accepted yet.

### 6. Gain projection

Once estimates are available, gains are projected conservatively:

- bounded by phase-dependent rate limits
- confidence-weighted
- clamped to safe ranges

## Diagnostics Philosophy

The climate diagnostics are intended to answer three practical questions:

1. is the scheduler actually delivering complete cycles?
2. is the cycle accepted for learning?
3. if not, where is it blocked?

Useful diagnostic groups:

- cycle flow:
  - `cycle_started_calls_count`
  - `cycle_completed_calls_count`
  - `last_cycle_started_at`
  - `last_cycle_completed_at`
- deadtime:
  - `nd_hat`
  - `c_nd`
  - `deadtime_candidate_costs`
  - `deadtime_b_proxy`
- startup bootstrap:
  - `startup_bootstrap_active`
  - `startup_bootstrap_stage`
  - `startup_bootstrap_attempt`
  - `startup_bootstrap_completion_reason`
- estimator:
  - `a_hat`
  - `b_hat`
  - `c_a`
  - `c_b`
  - `a_samples_count`
  - `b_samples_count`
  - `a_last_reason`
  - `b_last_reason`
- routing:
  - `current_cycle_regime`
  - `learning_route_selected`
  - `learning_route_block_reason`
  - `a_learning_enabled`
  - `deadtime_learning_blackout_active`
- cross-check:
  - `deadtime_b_proxy`
  - `b_crosscheck_error`
  - `b_methods_consistent`

## Bootstrap Phases

The algorithm progresses through a sequence of phases controlled by `supervisor.py`.
Each phase determines which learning operations are permitted and how aggressively gains may move.

### Phase progression

```
STARTUP → A → B → C → D
```

Phases advance forward only. A reset (`reset_learning`) returns to STARTUP.
A warm-start after a long absence may step back to A or B (see Warm Start section below).

---

### STARTUP

Entry: on first initialization or after a full reset.

- No cycle accepted yet.
- Gains are held at `default_kint` / `default_kext`.
- No deadtime search, no estimation.

Exit: immediately on the first accepted valid cycle → advance to A.

---

### Phase A

Entry: first valid cycle received.

Purpose: accumulate enough observations to begin deadtime search.

- Gains are frozen (rate limit = 0).
- Deadtime search runs and accumulates history.
- `b` estimation is blocked.
- `a` estimation is blocked.

Exit conditions (all required):
- `valid_cycles_count ≥ 5`
- `informative_deadtime_cycles_count ≥ 3`

Stuck detection: if ≥ 10 valid cycles have passed and `c_nd` stays below 0.2, `last_freeze_reason` is set to `"insufficient_excitation_bootstrap"`.

---

### Phase B

Entry: enough cycles for deadtime search.

Purpose: identify the deadtime and converge `b`.

- Gains move slowly: `delta_kint_max = 0.01`, `delta_kext_max = 0.002`.
- Deadtime search continues.
- `b` estimation is allowed (OFF windows only).
- `a` estimation is still blocked.

Exit conditions (all required):
- `deadtime_locked = True`
- `c_nd ≥ 0.6`
- `b_converged = True`

---

### Phase C

Entry: deadtime locked and `b` converged.

Purpose: active learning — both `a` and `b` update, gains move toward structural targets.

- Gains move faster: `delta_kint_max = 0.03`, `delta_kext_max = 0.005`.
- `b` estimation continues (OFF windows).
- `a` estimation is enabled (ON windows, requires deadtime locked and `b` converged).
- `adaptive_cycles_since_phase_c` counter is reset to 0 on entry.

Exit conditions (all required, checked after each estimator update):
- `c_a ≥ 0.6` and `c_b ≥ 0.5`
- `adaptive_cycles_since_phase_c ≥ 20`
- `a` and `b` have each moved less than 10 % over the last 11 accepted cycles

---

### Phase D

Entry: `a` and `b` have converged in Phase C.

Purpose: steady-state long-term operation.

- Gains move slowly again: `delta_kint_max = 0.01`, `delta_kext_max = 0.002`.
- Both `a` and `b` continue to adapt slowly.
- This is the nominal operating regime.

No automatic exit. The phase stays D indefinitely unless a warm-start revalidation occurs.

---

### Gain rate limits summary

| Phase   | `delta_kint_max` | `delta_kext_max` |
|---------|------------------|------------------|
| STARTUP | —  (fixed)       | —  (fixed)       |
| A       | 0.0              | 0.0              |
| B       | 0.01             | 0.002            |
| C       | 0.03             | 0.005            |
| D       | 0.01             | 0.002            |

---

### Warm start and phase revalidation

When persistent state is loaded after a gap:

- **Gap > 30 days**: confidences are halved (`decay_confidences(0.5)`). If `c_nd` falls below 0.6, `deadtime_locked` is cleared and the phase is stepped back to B.
- **Gap > 90 days**: confidences are fully reset and the phase is stepped back to A.
- **`cycle_min` changed**: deadtime model is discarded, confidences reset, phase stepped back to A (`"cycle_min_changed_revalidation"`).

---

### `deadtime_locked` and what clears it

`deadtime_locked` is recomputed every cycle. It is `False` when any of the following is true:

- fewer than 10 accepted cycles in the deadtime model (`"deadtime_insufficient_cycles"`)
- best-candidate dominance ratio < 2.0 over the second-best (`"deadtime_insufficient_separation"`)
- best candidate won fewer than 7 of the last 10 cycles (`"deadtime_inconsistent_winner"`)
- confidence decay after > 30 days drops `c_nd` below 0.6
- full confidence reset (gap > 90 days, or `cycle_min` change)

The `last_freeze_reason` diagnostic always names the active blocker.

## Known Limits

Current known limits of the prototype:

- thresholds are still conservative and may need tuning on field data
- deadtime confidence may rise slowly on sparse or low-contrast traces
- `a` intentionally starts later than `b`
- the plugin is still in an experimental stage and not finalized for production use
