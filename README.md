# vtherm_adaptive_tpi

Adaptive TPI plugin for [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat), built on top of `vtherm_api`.

## What It Does

`vtherm_adaptive_tpi` provides an external `adaptive_tpi` proportional algorithm for Versatile Thermostat.

Its goal is to learn, during normal thermostat operation:

- deadtime (`nd`)
- thermal losses (`b`)
- heating authority (`a`)

and to use these learned values to adjust the thermostat gains over time.

The plugin stays in the TPI family:

- it computes an `on_percent`
- Versatile Thermostat still applies that command through its normal cycle scheduler
- learning happens only from completed real cycles

The integration includes:

- Home Assistant integration scaffolding
- registration through `vtherm_api`
- runtime connection to Versatile Thermostat cycle callbacks
- coarse deadtime estimation
- OFF-window learning for `b`
- ON-window learning for `a`
- conservative gain projection
- persistent runtime state
- diagnostics exposed in the climate `specific_states`

## Learning Overview

At startup, the plugin does not know the plant yet.

The normal progression is:

1. if no deadtime identification exists yet, startup bootstrap may force one or two clean OFF->ON attempts
2. deadtime starts to emerge
3. `b` starts learning from OFF windows
4. `a` starts only later, once deadtime is credible and `b` is stable

Typical early observations are:

- `a_hat` frozen
- `b_converged = false`
- gains still close to defaults
- `startup_bootstrap_active = true` during the initial forced sequence

The runtime loop is:

1. the controller computes `on_percent`
2. the VT scheduler commits a real cycle
3. the plugin records the cycle context
4. at cycle end, the plugin validates the cycle for learning
5. the deadtime model is updated
6. short learning windows are reconstructed from cycle history
7. `b` may learn from OFF windows
8. `a` may learn from ON windows, once deadtime and `b` are ready
9. `k_int` and `k_ext` are projected conservatively

## Startup Bootstrap

When deadtime is still unknown, startup may temporarily override the nominal command:

- if already at or above setpoint, stay OFF until `target - 0.3°C`
- if below setpoint, first heat to setpoint, then cool to `target - 0.3°C`
- from `target - 0.3°C`, heat at `100%` until setpoint
- each bootstrap threshold crossing forces an immediate cycle restart so the scheduler does not wait for the previous cycle boundary
- if no deadtime identification is produced, retry once, then fall back to normal regulation
- the forced OFF cooldown may also feed the initial `b` learning path even when it starts very close to setpoint

## Diagnostics

The plugin exposes learning diagnostics in the climate `specific_states`.

The most useful fields to inspect first are:

- `startup_bootstrap_active`
- `startup_bootstrap_stage`
- `startup_bootstrap_attempt`
- `startup_bootstrap_completion_reason`
- `nd_hat`
- `c_nd`
- `b_hat`
- `c_b`
- `b_samples_count`
- `a_hat`
- `c_a`
- `a_samples_count`
- `last_learning_attempt_reason`
- `a_last_reason`
- `b_last_reason`
- `cycle_started_calls_count`
- `cycle_completed_calls_count`
- `accepted_cycles_count`

Healthy learning often looks like this:

- `nd_hat` starts moving before it is considered reliable
- `b_hat` appears before `a_hat`
- `b_samples_count` increases slowly
- `a_last_reason` often stays at `deadtime_not_locked` for a while
- `k_int` and `k_ext` stay near defaults until confidence is good enough

## Main Documentation

If you want to go deeper:

- [Diagnostics](docs/diagnostics.md)
  User-facing runtime diagnostics and how to interpret them
- [Architecture](docs/architecture.md)
  Internal architecture and learning flow

## Repository Layout

- [custom_components/vtherm_adaptive_tpi](vtherm_adaptive_tpi/custom_components/vtherm_adaptive_tpi)
  Home Assistant integration and adaptive algorithm code
- [docs](vtherm_adaptive_tpi/docs)
  Project documentation
- [tests](vtherm_adaptive_tpi/tests)
  Behavioral tests for the integration
- [plans](vtherm_adaptive_tpi/plans)
  Design notes, mathematical specs, implementation plans, and review reports

## Development Notes

This plugin depends on:

- `versatile_thermostat`
- `vtherm_api`

Development should be done with compatible versions of both sides.
