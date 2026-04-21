# Adaptive TPI Diagnostics

## Purpose

This document explains the main diagnostics exposed by `adaptive_tpi` in the climate `specific_states`.

These diagnostics are mainly intended for:

- development
- field validation
- learning/debug sessions

## Reading The Runtime State

### Regulation vs learning

Two different things happen at runtime:

1. the thermostat regulates temperature
2. the adaptive model tries to learn from completed cycles

So it is possible to have:

- correct regulation
- but little or no learning progress

This is why diagnostics should be read in groups.

## Core Fields

### Phase

- `bootstrap_phase`
- `phase`

Current bootstrap phases are used to indicate where the runtime is in the learning progression.

Typical interpretation:

- early phases: deadtime and `b` are still being built
- later phases: `a` and gain projection can contribute more

### Startup bootstrap

- `startup_bootstrap_active`
- `startup_bootstrap_stage`
- `startup_bootstrap_attempt`
- `startup_bootstrap_max_attempts`
- `startup_bootstrap_target_temp`
- `startup_bootstrap_lower_target_temp`
- `startup_bootstrap_command_on_percent`
- `startup_bootstrap_completion_reason`

These fields track the forced startup sequence used before the first deadtime identification.

Typical stages are:

- `idle`
- `preheat_to_target`
- `cooldown_below_target`
- `reheat_to_target`
- `completed`
- `abandoned`

Interpretation:

- `startup_bootstrap_active = true` means the scheduler command is currently overridden
- `startup_bootstrap_attempt` shows whether the runtime is on the first or second OFF->ON attempt
- `startup_bootstrap_lower_target_temp` is the temporary OFF threshold, equal to `target - 0.3°C`
- `startup_bootstrap_command_on_percent` is the forced command currently requested by the bootstrap state machine
- `startup_bootstrap_completion_reason` explains how the sequence ended

### Gains

- `k_int`
- `k_ext`

These are the currently projected controller gains actually used by the plugin.

### Deadtime

- `nd_hat`
  Estimated deadtime in number of cycles
- `nd_hat_cycles`
  Explicit alias of `nd_hat`, also in number of cycles
- `deadtime_min`
  Deadtime converted into minutes using the last accepted cycle duration
- `c_nd`
  Confidence in the deadtime estimate, in `[0, 1]`
- `deadtime_candidate_costs`
  Fit score for each deadtime candidate
- `deadtime_b_proxy`
  `b` proxy derived from the best deadtime candidate fit

Important:

- `nd_hat` is expressed in cycles, not minutes
- `deadtime_min` is the user-facing normalized deadtime in minutes
- deadtime in minutes is computed as `nd_hat * cycle_min_at_last_accepted_cycle`

### Estimator state

- `a_hat`
- `a_hat_per_hour`
- `b_hat`
- `b_hat_per_hour`
- `tau_h`
- `tau_min`
- `c_a`
- `c_b`
- `b_converged`

Interpretation:

- `a_hat`: learned heating authority in `°C/cycle`
- `a_hat_per_hour`: same quantity normalized to `°C/hour`
- `b_hat`: learned thermal loss coefficient in `1/cycle`
- `b_hat_per_hour`: same quantity normalized to `1/hour`
- `tau_h`: thermal time constant derived from `b_hat_per_hour`, in hours
- `tau_min`: same thermal time constant in minutes
- `c_a`, `c_b`: confidence in these estimates
- `b_converged`: whether `b` is stable enough to open learning for `a`

### Routing state

- `current_cycle_regime`
- `learning_route_selected`
- `learning_route_block_reason`
- `a_learning_enabled`
- `deadtime_learning_blackout_active`

Interpretation:

- `current_cycle_regime` describes how the completed cycle was classified:
  - `off`
  - `on`
  - `mixed`
- `learning_route_selected` is the branch chosen for that completed cycle:
  - `a`
  - `b`
  - `none`
- `learning_route_block_reason` explains why the chosen route did not lead to an accepted update
- `a_learning_enabled` means the runtime conditions are good enough to let `a` learn when an ON window is valid
- `deadtime_learning_blackout_active = true` means the window is still inside the post-transition deadtime blackout, so explicit `a`/`b` learning is held back

### Informative flags

- `i_a`
- `i_b`

These are not values of `a` or `b`.

They indicate whether the latest update path was informative enough to attempt learning on that branch.

In practice:

- `i_b = 1` means a `b` update was accepted on that cycle
- `i_a = 1` means an `a` update was accepted on that cycle

### Counters

- `a_samples_count`
- `b_samples_count`
- `accepted_cycles_count`
- `cycle_started_calls_count`
- `cycle_completed_calls_count`

These are essential to know whether the problem is:

- before cycle completion
- before cycle acceptance
- or inside the estimator itself

## Last Reason Fields

### Learning attempt

- `last_learning_attempt_reason`
- `last_learning_attempt_regime`

These describe the latest learning path considered by the algorithm.

Examples:

- `cycle_interrupted`
- `deadtime_not_locked`
- `off_window_no_candidate`
- `off_window_waiting_more_signal`
- `on_window_deadtime_blackout`
- `mixed_cycle_regime`

### Branch-specific reasons

- `a_last_reason`
- `b_last_reason`

These are usually the best place to look when one branch does not progress.

Examples for `b`:

- `off_window_no_candidate`
- `off_window_setpoint_changed`
- `off_window_waiting_more_signal`
- `off_window_deadtime_blackout`
- `b_delta_out_too_small`
- `b_setpoint_error_too_small`
- `b_window_not_quasi_off`

## Cross-check Between Two `b` Estimates

The runtime currently has two ways to get information about `b`:

1. explicit OFF-window estimator
2. deadtime-side proxy from the best deadtime candidate fit

Related diagnostics:

- `deadtime_b_proxy`
- `b_crosscheck_error`
- `b_methods_consistent`

Interpretation:

- low `b_crosscheck_error` is good
- `b_methods_consistent = true` means both methods tell a similar story
- large disagreement suggests caution even if one method alone looks stable

## Practical Debug Patterns

### Case 1: cycles do not progress

Look at:

- `cycle_started_calls_count`
- `cycle_completed_calls_count`

If starts increase but completes do not, the issue is before the learning logic.

### Case 2: cycles complete but learning does not

Look at:

- `accepted_cycles_count`
- `last_learning_attempt_reason`
- `a_last_reason`
- `b_last_reason`

This usually means the learning guards or the window builder are rejecting the cycle.

### Case 2b: deadtime still does not appear at startup

Look at:

- `startup_bootstrap_active`
- `startup_bootstrap_stage`
- `startup_bootstrap_attempt`
- `startup_bootstrap_completion_reason`
- `deadtime_identification_count`

If the bootstrap reaches `abandoned` after attempt `2` and `deadtime_identification_count`
is still `0`, the plugin has already exhausted the dedicated startup sequence and has
returned to normal regulation.

### Case 3: `b` does not move in OFF phase

Look at:

- `b_last_reason`
- `b_samples_count`
- `deadtime_b_proxy`
- `b_crosscheck_error`

Common explanations:

- no valid OFF candidate yet
- a contradictory setpoint jump invalidated the window
- the cycle is still inside the deadtime blackout after a recent regime transition
- not enough signal
- setpoint error too small

### Case 4: `a` does not start

Look at:

- `deadtime_locked`
- `c_nd`
- `b_converged`
- `a_learning_enabled`
- `a_last_reason`

This is often normal during bootstrap.

### Case 5: learning is blocked even though cycles complete

Look at:

- `current_cycle_regime`
- `learning_route_selected`
- `learning_route_block_reason`
- `deadtime_learning_blackout_active`

Common explanations:

- the completed cycle is `mixed`, so no explicit `a`/`b` route is opened
- the route is correct, but the window is still inside the deadtime blackout
- the setpoint moved against the current regime and invalidated the window

## Persistence Note

The routing diagnostics are runtime-only by design:

- `current_cycle_regime`
- `learning_route_selected`
- `learning_route_block_reason`
- `deadtime_learning_blackout_active`

They describe the latest decision path and are not intended to survive a restart.
Persistent fields remain focused on adaptive state continuity rather than the last branch-selection event.
