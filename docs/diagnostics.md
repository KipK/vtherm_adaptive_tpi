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
- `cooling_learning`
- `heating_learning`
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
- `deadtime_minutes` is the same value normalized with the last accepted cycle duration
- `deadtime_confidence` is the confidence in that estimate, in `[0, 1]`

### Heating and cooling rates

- `heating_rate_per_hour`
- `cooling_rate_per_hour`
- `thermal_time_constant_hours`
- `thermal_time_constant_minutes`
- `heating_rate_confidence`
- `cooling_rate_confidence`
- `cooling_rate_converged`

Interpretation:

- `heating_rate_per_hour` is the learned heating authority normalized in `°C/hour`
- `cooling_rate_per_hour` is the learned cooling loss rate normalized in `1/hour`
- `thermal_time_constant_hours` and `thermal_time_constant_minutes` are derived from the cooling rate
- `cooling_rate_converged` indicates whether cooling estimation is stable enough to open heating learning

### Sample counters

- `heating_samples`
- `cooling_samples`
- `heating_learning_enabled`

Interpretation:

- `heating_samples` counts accepted `a` updates
- `cooling_samples` counts accepted `b` updates
- `heating_learning_enabled` indicates whether runtime conditions allow `a` learning when an ON window is valid

### Startup sequence

- `startup_sequence_active`
- `startup_sequence_stage`
- `startup_sequence_attempt`
- `startup_sequence_max_attempts`
- `startup_sequence_target_temperature`
- `startup_sequence_cooling_temperature`
- `startup_sequence_requested_power`
- `startup_sequence_completion_reason`

Possible `startup_sequence_stage` values:

- `idle`
- `heating_to_target`
- `cooling_below_target`
- `reheating_to_target`
- `completed`
- `abandoned`

Interpretation:

- `startup_sequence_active = true` means startup bootstrap is currently overriding the nominal command
- `startup_sequence_cooling_temperature` is the temporary cooldown threshold, equal to `target - 0.3°C`
- `startup_sequence_requested_power` is the power currently requested by the startup sequence

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
  - `on_percent`
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
6. `cooling_rate_per_hour`
7. `cooling_rate_converged`
8. `heating_rate_per_hour`
9. `last_learning_result`
10. `last_runtime_blocker`

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

- `cooling_samples`
- `cooling_rate_per_hour`
- `cooling_rate_confidence`
- `last_learning_result`

If needed, enable debug mode and inspect:

- `debug["b_last_reason"]`
- `debug["learning_route_selected"]`
- `debug["learning_route_block_reason"]`

### Heating does not start learning

Look at:

- `heating_learning_enabled`
- `cooling_rate_converged`
- `deadtime_confidence`
- `heating_samples`
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
- `debug["on_percent"]`

## Persistence Note

The compact user-facing diagnostics describe the current adaptive state.

The debug routing fields remain runtime-oriented and are not intended to be a
stable persistence contract for dashboards across restarts.
