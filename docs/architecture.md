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

Coarse deadtime estimator.

Responsibilities:

- keep a temporally contiguous cycle history
- score candidate deadtimes
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

## Learning Sequence

### 1. Cycle start

When VT starts a real cycle, the plugin captures:

- target temperature
- indoor temperature
- outdoor temperature
- applied power
- hvac mode

This snapshot becomes the pending cycle context.

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

## Known Limits

Current known limits of the prototype:

- thresholds are still conservative and may need tuning on field data
- deadtime confidence may rise slowly on sparse or low-contrast traces
- `a` intentionally starts later than `b`
- the plugin is still in an experimental stage and not finalized for production use
