# Adaptive TPI Diagnostics

## Purpose

This document explains the diagnostics exposed by `adaptive_tpi` in the climate
`specific_states`.

The diagnostics are split into two layers:

- a compact non-debug view intended for normal monitoring
- a larger debug view intended for tuning and internal analysis

## Non-Debug Diagnostics

These keys are the primary user-facing diagnostics.

### Learning phase

- `adaptive_phase`

Possible values:

- `startup`
- `deadtime_learning`
- `drift_learning`
- `control_learning`
- `stabilized`

### Gains

- `gain_indoor`
- `gain_outdoor`

These are the projected gains currently used by the controller.

### Deadtime

- `deadtime_cycles`
- `deadtime_minutes`
- `deadtime_confidence`

Interpretation:

- `deadtime_cycles` is the current deadtime estimate in scheduler cycles
- `deadtime_minutes` is the measured deadtime in minutes attached to the selected `deadtime_cycles` estimate
- when no measured minute value is available yet, `deadtime_minutes` falls back to the normalized value built from `deadtime_cycles` and the last accepted cycle duration
- `deadtime_confidence` is the confidence in that estimate, in `[0, 1]`

### Heating and cooling rates

- `control_rate_per_hour`
- `drift_rate_per_hour`
- `thermal_time_constant_hours`
- `control_rate_confidence`
- `drift_rate_confidence`
- `control_rate_converged`
- `drift_rate_converged`

Interpretation:

- `control_rate_per_hour` is the learned heating authority normalized in `°C/hour`
- `drift_rate_per_hour` is the learned cooling loss rate normalized in `1/hour`
- `thermal_time_constant_hours` is derived from the cooling rate
- `control_rate_converged` indicates whether the heating estimate has reached the confidence target used by Phase C stabilization checks
- `drift_rate_converged` indicates whether cooling estimation is stable enough to open heating learning

### Sample counters

- `control_samples`
- `drift_samples`
- `sample_window_size`
- `control_learning_enabled`

Interpretation:

- `control_samples` counts accepted `a` updates
- `drift_samples` counts accepted `b` updates
- `sample_window_size` exposes the rolling estimator capacity used by both counters
- `control_learning_enabled` indicates whether runtime conditions allow `a` learning when an ON window is valid

### Startup sequence

- `startup_sequence_active`
- `startup_sequence_stage`
- `startup_sequence_attempt`
- `startup_sequence_max_attempts`
- `startup_sequence_completion_reason`

Possible `startup_sequence_stage` values:

- `idle`
- `active_to_target`
- `passive_drift_phase`
- `reactivation_to_target`
- `completed`
- `abandoned`

Interpretation:

- `startup_sequence_active = true` means startup bootstrap is currently overriding the nominal command
- `current_cycle_percent` is the power committed for the currently engaged cycle
- `next_cycle_percent` is the requested power prepared for the next scheduler cycle

### Last result and blocker

- `last_learning_result`
- `last_learning_family`
- `last_runtime_blocker`

Interpretation:

- `last_learning_result` is the latest learning outcome or rejection reason
- `last_learning_family` identifies which branch was considered last:
  - `heating`
  - `cooling`
- `last_runtime_blocker` is the latest runtime freeze reason from the supervisor

## Debug Diagnostics

When debug mode is enabled, the diagnostics include a `debug` mapping with the
technical internal fields.

This mapping keeps the implementation-oriented names used by the algorithm.

### Main technical groups

- supervisor and phase:
  - `bootstrap_phase`
  - `phase`
  - `last_freeze_reason`
- gains and estimators:
  - `k_int`
  - `k_ext`
  - `a_hat`
  - `b_hat`
  - `a_hat_per_hour`
  - `b_hat_per_hour`
  - `tau_h`
  - `tau_min`
  - `c_a`
  - `c_b`
  - `b_converged`
  - `i_a`
  - `i_b`
  - `a_dispersion`
  - `b_dispersion`
- deadtime:
  - `nd_hat`
  - `nd_hat_cycles`
  - `deadtime_min`
- `deadtime_min` follows the same selected-deadtime minute rule as `deadtime_minutes`
  - `c_nd`
  - `deadtime_identification_count`
  - `deadtime_identification_qualities`
  - `deadtime_b_proxy`
  - `deadtime_locked`
  - `deadtime_pending_step`
  - `deadtime_best_candidate`
  - `deadtime_second_best_candidate`
- startup bootstrap:
  - `startup_bootstrap_active`
  - `startup_bootstrap_stage`
  - `startup_bootstrap_attempt`
  - `startup_bootstrap_max_attempts`
  - `startup_bootstrap_target_temp`
  - `startup_bootstrap_lower_target_temp`
  - `startup_bootstrap_command_on_percent`
  - `startup_bootstrap_completion_reason`
- routing:
  - `current_cycle_regime`
  - `learning_route_selected`
  - `learning_route_block_reason`
  - `deadtime_learning_blackout_active`
  - `a_learning_enabled`
  - `a_last_reason`
  - `b_last_reason`
  - `last_learning_attempt_reason`
  - `last_learning_attempt_regime`
- cycle flow:
  - `accepted_cycles_count`
  - `cycle_started_calls_count`
  - `cycle_completed_calls_count`
  - `cycle_min_at_last_accepted_cycle`
  - `hours_without_excitation`
  - `last_cycle_started_at`
  - `last_cycle_completed_at`
  - `last_cycle_classification`
  - `valid_cycles_count`
  - `informative_deadtime_cycles_count`
  - `adaptive_cycles_since_phase_c`
  - `calculated_on_percent`
  - `requested_on_percent`
  - `committed_on_percent`
- cross-check:
  - `b_crosscheck_error`
  - `b_methods_consistent`

## Practical Reading Order

For normal monitoring, read the diagnostics in this order:

1. `adaptive_phase`
2. `startup_sequence_active`
3. `startup_sequence_stage`
4. `deadtime_cycles`
5. `deadtime_confidence`
6. `drift_rate_per_hour`
7. `drift_rate_converged`
8. `control_rate_per_hour`
9. `control_rate_converged`
10. `last_learning_result`
11. `last_runtime_blocker`

## Common Situations

### Startup is still running

Look at:

- `startup_sequence_active`
- `startup_sequence_stage`
- `startup_sequence_attempt`
- `startup_sequence_completion_reason`
- `deadtime_cycles`

### Cooling does not progress

Look at:

- `drift_samples`
- `drift_rate_per_hour`
- `drift_rate_confidence`
- `last_learning_result`

If needed, enable debug mode and inspect:

- `debug["b_last_reason"]`
- `debug["learning_route_selected"]`
- `debug["learning_route_block_reason"]`

### Heating does not start learning

Look at:

- `control_learning_enabled`
- `drift_rate_converged`
- `control_rate_converged`
- `deadtime_confidence`
- `control_samples`
- `last_runtime_blocker`

If needed, enable debug mode and inspect:

- `debug["a_last_reason"]`

### Regulation still looks frozen

Look at:

- `last_runtime_blocker`
- `gain_indoor`
- `gain_outdoor`

If needed, enable debug mode and inspect:

- `debug["last_cycle_classification"]`
- `debug["calculated_on_percent"]`
- `debug["requested_on_percent"]`
- `debug["committed_on_percent"]`

## Persistence Note

The compact user-facing diagnostics describe the current adaptive state.

The debug routing fields remain runtime-oriented and are not intended to be a
stable persistence contract for dashboards across restarts.
