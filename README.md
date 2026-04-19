# vtherm_adaptive_tpi

Adaptive TPI plugin for [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat), built on top of `vtherm_api`.

## Work In Progress

This project is still a **work in progress**.

It is already usable for development and field experiments, but it is not yet a finished production-grade adaptive controller. Learning thresholds, diagnostics, and runtime behavior may still evolve as the plugin is validated on real traces.

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

## Current State

The current prototype already includes:

- Home Assistant integration scaffolding
- registration through `vtherm_api`
- runtime connection to Versatile Thermostat cycle callbacks
- coarse deadtime estimation
- OFF-window learning for `b`
- ON-window learning for `a`
- conservative gain projection
- persistent runtime state
- diagnostics exposed in the climate `specific_states`

What is still evolving:

- tuning of guards and thresholds
- convergence behavior on real systems
- documentation depth and polish

## First Steps

### 1. Treat it as an experimental adaptive plugin

This repository is best used today for:

- development
- validation on real thermostat traces
- debugging adaptive learning behavior

### 2. Expect learning to start gradually

At startup, the plugin does not know the plant yet.

A normal early progression is:

1. deadtime starts to emerge
2. `b` starts learning from OFF windows
3. `a` starts only later, once deadtime is credible and `b` is stable

So it is normal at first to see:

- `a_hat` frozen
- `b_converged = false`
- gains still close to defaults

### 3. Use diagnostics to understand learning

The plugin exposes learning diagnostics in the climate `specific_states`.

The most useful fields to inspect first are:

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

## How It Works

At a high level, the runtime loop is:

1. the controller computes `on_percent`
2. the VT scheduler commits a real cycle
3. the plugin records the cycle context
4. at cycle end, the plugin decides whether that cycle is valid for learning
5. the deadtime model is updated
6. short learning windows are reconstructed from real cycle history
7. `b` may learn from OFF windows
8. `a` may learn from ON windows, but only later in bootstrap
9. `k_int` and `k_ext` are projected conservatively

Important design points:

- learning uses real completed cycles, not every sensor refresh
- OFF and ON windows are handled separately
- learning is intentionally conservative
- diagnostics are first-class because they are essential for tuning and debugging

## What To Expect In Practice

Healthy early runtime often looks like this:

- `nd_hat` starts moving before it is considered reliable
- `b_hat` appears before `a_hat`
- `b_samples_count` increases slowly
- `a_last_reason` often stays at `deadtime_not_locked` for a while
- `k_int` and `k_ext` stay near defaults until confidence is good enough

This is normal for the current design.

## Main Documentation

If you want to go deeper:

- [Diagnostics](vtherm_adaptive_tpi/docs/diagnostics.md)  
  User-facing runtime diagnostics and how to interpret them
- [Architecture](vtherm_adaptive_tpi/docs/architecture.md)  
  Internal architecture and learning flow

## Repository Layout

- [custom_components/vtherm_adaptive_tpi](vtherm_adaptive_tpi/custom_components/vtherm_adaptive_tpi)  
  Home Assistant integration and adaptive algorithm code
- [docs](vtherm_adaptive_tpi/docs)  
  Project documentation
- [tests](vtherm_adaptive_tpi/tests)  
  Behavioral tests for the prototype
- [plans](vtherm_adaptive_tpi/plans)  
  Design notes, mathematical specs, implementation plans, and review reports

## Development Notes

This plugin depends on:

- `versatile_thermostat`
- `vtherm_api`

The current manifest uses the main branch of `vtherm_api`, so development should be done with compatible versions of both sides.
