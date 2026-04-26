{% set entity = 'climate.my_thermostat' %}

{% set thermostat_name = none %}
{% set name = thermostat_name or state_attr(entity, 'friendly_name') or entity %}
{% set diag = ((state_attr(entity, 'specific_states') or {}).get('adaptive_tpi')) %}
{% set debug = ((diag or {}).get('debug')) %}
{% set hvac_mode = states(entity) %}
{% set is_cool = hvac_mode == 'cool' %}
{% set icon_control = '❄️' if is_cool else '🔥' %}
{% set icon_drift = '☀️' if is_cool else '💨' %}
{% set label_control = 'Cooling rate' if is_cool else 'Heating rate' %}
{% set label_drift = 'Heating drift' if is_cool else 'Cooling drift' %}
{% set phase_names = {
  'startup': 'Startup',
  'deadtime_learning': 'Deadtime',
  'drift_learning': 'Drift learning',
  'control_learning': 'Control learning',
  'stabilized': 'Stabilized'
} %}
{% set stage_names = {
  'idle': 'Idle',
  'active_to_target': 'Active to setpoint',
  'passive_drift_phase': 'Passive drift',
  'reactivation_to_target': 'Reactivation to setpoint',
  'reactivation_to_upper_target': 'Reactivation to upper target',
  'return_to_target': 'Return to setpoint',
  'completed': 'Completed',
  'abandoned': 'Stopped'
} %}
{% set family_names = {
  'control': 'Control',
  'drift': 'Drift'
} %}
{% if not diag %}
## {{ name }}

No `specific_states.adaptive_tpi` data found for `{{ entity }}`.
{% else %}
{% set phase = phase_names.get(diag.get('adaptive_phase'), diag.get('adaptive_phase') or 'Unavailable') %}
{% set stage = stage_names.get(diag.get('startup_sequence_stage'), diag.get('startup_sequence_stage') or 'Unavailable') %}
{% set current_cycle = diag.get('current_cycle_percent') %}
{% set next_cycle = diag.get('next_cycle_percent') %}
{% set actuator_mode = diag.get('actuator_mode') %}
{% set valve_curve_params = diag.get('valve_curve_params') %}
{% set valve_curve_learning_enabled = diag.get('valve_curve_learning_enabled') %}
{% set valve_curve_converged = diag.get('valve_curve_converged') %}
{% set valve_curve_observations_accepted = diag.get('valve_curve_observations_accepted') %}
{% set valve_curve_observations_rejected = diag.get('valve_curve_observations_rejected') %}
{% set valve_curve_last_reason = diag.get('valve_curve_last_reason') %}
{% set deadtime_minutes = diag.get('deadtime_minutes') %}
{% set deadtime_cycles = diag.get('deadtime_cycles') %}
{% set deadtime_confidence = diag.get('deadtime_confidence') %}
{% set deadtime_on_minutes = diag.get('deadtime_on_minutes') %}
{% set deadtime_on_cycles = diag.get('deadtime_on_cycles') %}
{% set deadtime_on_confidence = diag.get('deadtime_on_confidence') %}
{% set deadtime_on_locked = diag.get('deadtime_on_locked') %}
{% set deadtime_off_minutes = diag.get('deadtime_off_minutes') %}
{% set deadtime_off_cycles = diag.get('deadtime_off_cycles') %}
{% set deadtime_off_confidence = diag.get('deadtime_off_confidence') %}
{% set deadtime_off_locked = diag.get('deadtime_off_locked') %}
{% set gain_indoor = diag.get('gain_indoor') %}
{% set gain_outdoor = diag.get('gain_outdoor') %}
{% set control_rate = diag.get('control_rate_per_hour') %}
{% set drift_rate = diag.get('drift_rate_per_hour') %}
{% set tau_h = diag.get('thermal_time_constant_hours') %}
{% set control_samples = diag.get('control_samples') %}
{% set drift_samples = diag.get('drift_samples') %}
{% set sample_window_size = diag.get('sample_window_size') or 12 %}
{% set control_enabled = diag.get('control_learning_enabled') %}
{% set control_converged = diag.get('control_rate_converged') %}
{% set drift_converged = diag.get('drift_rate_converged') %}
{% set startup_active = diag.get('startup_sequence_active') %}
{% set startup_attempt = diag.get('startup_sequence_attempt') %}
{% set startup_max = diag.get('startup_sequence_max_attempts') %}
{% set startup_done = diag.get('startup_sequence_completion_reason') %}
{% set last_result = diag.get('last_learning_result') %}
{% set last_family = family_names.get(diag.get('last_learning_family'), diag.get('last_learning_family') or 'None') %}
{% set last_blocker = diag.get('last_runtime_blocker') %}
{% set current_cycle_text = ((current_cycle * 100) | round(0) ~ ' %') if current_cycle is not none else 'Unavailable' %}
{% set next_cycle_text = ((next_cycle * 100) | round(0) ~ ' %') if next_cycle is not none else 'Unavailable' %}
{% set calculated_cycle = debug.get('calculated_on_percent') if debug else none %}
{% set requested_cycle = debug.get('requested_on_percent') if debug else none %}
{% set committed_cycle = debug.get('committed_on_percent') if debug else none %}
{% set calculated_cycle_text = ((calculated_cycle * 100) | round(0) ~ ' %') if calculated_cycle is not none else 'Unavailable' %}
{% set requested_cycle_text = ((requested_cycle * 100) | round(0) ~ ' %') if requested_cycle is not none else 'Unavailable' %}
{% set committed_cycle_text = ((committed_cycle * 100) | round(0) ~ ' %') if committed_cycle is not none else 'Unavailable' %}
{% set deadtime_text = (deadtime_minutes | round(1) ~ ' min') if deadtime_minutes is not none else ((deadtime_cycles | round(2) ~ ' cycle(s)') if deadtime_cycles is not none else 'Not measured') %}
{% set deadtime_conf_text = ((deadtime_confidence * 100) | round(0) ~ ' %') if deadtime_confidence is not none else 'Unavailable' %}
{% set deadtime_on_text = (deadtime_on_minutes | round(1) ~ ' min') if deadtime_on_minutes is not none else ((deadtime_on_cycles | round(2) ~ ' cycle(s)') if deadtime_on_cycles is not none else 'Not measured') %}
{% set deadtime_off_text = (deadtime_off_minutes | round(1) ~ ' min') if deadtime_off_minutes is not none else ((deadtime_off_cycles | round(2) ~ ' cycle(s)') if deadtime_off_cycles is not none else 'Not measured') %}
{% set deadtime_on_conf_text = ((deadtime_on_confidence * 100) | round(0) ~ ' %') if deadtime_on_confidence is not none else 'Unavailable' %}
{% set deadtime_off_conf_text = ((deadtime_off_confidence * 100) | round(0) ~ ' %') if deadtime_off_confidence is not none else 'Unavailable' %}
{% set gain_indoor_text = (gain_indoor | round(3)) if gain_indoor is not none else 'Unavailable' %}
{% set gain_outdoor_text = (gain_outdoor | round(3)) if gain_outdoor is not none else 'Unavailable' %}
{% set control_rate_text = (control_rate | round(2) ~ ' °C/h') if control_rate is not none else 'Pending' %}
{% set drift_rate_text = (drift_rate | round(3) ~ ' 1/h') if drift_rate is not none else 'Pending' %}
{% set tau_text = (tau_h | round(2) ~ ' h') if tau_h is not none else 'Pending' %}
{% set actuator_mode_text = actuator_mode or 'Unavailable' %}
{% set has_valve_curve_params = valve_curve_params is not none %}
{% set valve_curve_status_text = 'Stable' if valve_curve_converged else ('Learning enabled' if valve_curve_learning_enabled else ('Compensation disabled' if actuator_mode == 'valve' and not has_valve_curve_params else 'Frozen')) %}
{% set valve_curve_reason_text = valve_curve_last_reason if valve_curve_last_reason else 'None' %}
{% set startup_attempt_text = startup_attempt ~ ' / ∞' if startup_attempt is not none and startup_max == 0 else (startup_attempt ~ ' / ' ~ startup_max if startup_attempt is not none and startup_max is not none else 'Unavailable') %}

## 🧠 {{ name }}

{% if debug %}`🛠️ Debug active`{% endif %}

| Overview                         | Value                                                           |
| -------------------------------- | --------------------------------------------------------------- |
| 🧭 Phase                          | **{{ phase }}**                                                 |
| 🌡️ Mode                           | **{{ hvac_mode }}**                                             |
| {{ icon_control }} Current cycle | **{{ current_cycle_text }}**                                    |
| ⏭️ Next cycle                     | **{{ next_cycle_text }}**                                       |
| 🚀 Startup                        | **{{ 'Active - ' ~ stage if startup_active else 'Inactive' }}** |

| Learning                               | Value                                                                                                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| ⏱️ Deadtime ON                          | **{{ deadtime_on_text }} · {{ 'locked' if deadtime_on_locked else 'learning' }} · {{ deadtime_on_conf_text }}**    |
| 📴 Deadtime OFF                         | **{{ deadtime_off_text }} · {{ 'locked' if deadtime_off_locked else 'learning' }} · {{ deadtime_off_conf_text }}** |
| 🎚️ Indoor gain                          | **{{ gain_indoor_text }}**                                                                                         |
| 🌤️ Outdoor gain                         | **{{ gain_outdoor_text }}**                                                                                        |
| {{ icon_control }} {{ label_control }} | **{{ control_rate_text }}**                                                                                        |
| {{ icon_drift }} {{ label_drift }}     | **{{ drift_rate_text }}**                                                                                          |
| 🏠 Thermal constant                     | **{{ tau_text }}**                                                                                                 |

| Model status                        | Value                                                                                          |
| ----------------------------------- | ---------------------------------------------------------------------------------------------- |
| {{ icon_drift }} Drift model        | **{{ 'Stable' if drift_converged else 'Learning' }}**                                          |
| {{ icon_control }} Control model    | **{{ 'Stable' if control_converged else 'Learning' }}**                                        |
| {{ icon_control }} Control learning | **{{ 'Allowed' if control_enabled else 'Not yet' }}**                                          |
| {{ icon_drift }} Drift samples      | **{{ (drift_samples if drift_samples is not none else 0) ~ ' / ' ~ sample_window_size }}**     |
| {{ icon_control }} Control samples  | **{{ (control_samples if control_samples is not none else 0) ~ ' / ' ~ sample_window_size }}** |

{% if actuator_mode == 'valve' %}
| Valve curve             | Value                                                                                               |
| ----------------------- | --------------------------------------------------------------------------------------------------- |
| 🧩 Actuator mode         | **{{ actuator_mode_text }}**                                                                        |
| 📈 Curve learning        | **{{ valve_curve_status_text }}**                                                                   |
| ✅ Accepted observations | **{{ valve_curve_observations_accepted if valve_curve_observations_accepted is not none else 0 }}** |
| 🚫 Rejected observations | **{{ valve_curve_observations_rejected if valve_curve_observations_rejected is not none else 0 }}** |
| 📝 Last curve result     | **{{ '`' ~ valve_curve_reason_text ~ '`' }}**                                                       |

| Demand vs applied power   | Value                           |
| ------------------------- | ------------------------------- |
| 🎯 Linear demand           | **{{ calculated_cycle_text }}** |
| 🛞 Requested valve command | **{{ requested_cycle_text }}**  |
| ⚙️ Applied power           | **{{ committed_cycle_text }}**  |

{% if has_valve_curve_params %}
| Valve curve parameters | Value                                      |
| ---------------------- | ------------------------------------------ |
| `min_valve`            | **{{ valve_curve_params.get('min_valve')   | round(1) }} %** |
| `knee_demand`          | **{{ valve_curve_params.get('knee_demand') | round(1) }} %** |
| `knee_valve`           | **{{ valve_curve_params.get('knee_valve')  | round(1) }} %** |
| `max_valve`            | **{{ valve_curve_params.get('max_valve')   | round(1) }} %** |
{% endif %}
{% endif %}

| Recent activity | Value                                                                    |
| --------------- | ------------------------------------------------------------------------ |
| 🔁 Last family   | **{{ last_family }}**                                                    |
| ✅ Last result   | **{{ '`' ~ last_result ~ '`' if last_result else 'No recent result' }}** |
| 🚧 Blocker       | **{{ '`' ~ last_blocker ~ '`' if last_blocker else 'None' }}**           |

{% if startup_active or startup_done %}
| Startup sequence | Value                                                                 |
| ---------------- | --------------------------------------------------------------------- |
| 🪜 Stage          | **{{ stage }}**                                                       |
| 🔄 Attempt        | **{{ startup_attempt_text }}**                                        |
| 🏁 Completion     | **{{ '`' ~ startup_done ~ '`' if startup_done else 'In progress' }}** |
| ⏱️ ON acquired    | **{{ 'Yes' if deadtime_on_locked else 'No' }}**                       |
| 📴 OFF acquired   | **{{ 'Yes' if deadtime_off_locked else 'No' }}**                      |
{% endif %}

{% if debug %}
## 🛠️ Debug

| Power / cycles              | Value                                                         |
| --------------------------- | ------------------------------------------------------------- |
| `calculated_on_percent`     | {{ ((debug.get('calculated_on_percent') * 100)                | round(0) ~ ' %') if debug.get('calculated_on_percent') is not none else 'Unavailable' }} |
| `requested_on_percent`      | {{ ((debug.get('requested_on_percent') * 100)                 | round(0) ~ ' %') if debug.get('requested_on_percent') is not none else 'Unavailable' }}  |
| `committed_on_percent`      | {{ ((debug.get('committed_on_percent') * 100)                 | round(0) ~ ' %') if debug.get('committed_on_percent') is not none else 'Unavailable' }}  |
| `current_cycle_regime`      | `{{ debug.get('current_cycle_regime', 'unavailable') }}`      |
| `last_cycle_classification` | `{{ debug.get('last_cycle_classification', 'unavailable') }}` |
| `accepted_cycles_count`     | {{ debug.get('accepted_cycles_count', 'unavailable') }}       |
| `valid_cycles_count`        | {{ debug.get('valid_cycles_count', 'unavailable') }}          |

| Routing / estimation           | Value                                                                                                                       |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `learning_route_selected`      | `{{ debug.get('learning_route_selected', 'unavailable') }}`                                                                 |
| `learning_route_block_reason`  | {{ '`' ~ debug.get('learning_route_block_reason') ~ '`' if debug.get('learning_route_block_reason') else 'None' }}          |
| `last_learning_attempt_reason` | {{ '`' ~ debug.get('last_learning_attempt_reason') ~ '`' if debug.get('last_learning_attempt_reason') else 'Unavailable' }} |
| `a_hat`                        | {{ debug.get('a_hat')                                                                                                       | round(4) if debug.get('a_hat') is not none else 'Unavailable' }}       |
| `b_hat`                        | {{ debug.get('b_hat')                                                                                                       | round(4) if debug.get('b_hat') is not none else 'Unavailable' }}       |
| `c_a`                          | {{ ((debug.get('c_a') * 100)                                                                                                | round(0) ~ ' %') if debug.get('c_a') is not none else 'Unavailable' }} |
| `c_b`                          | {{ ((debug.get('c_b') * 100)                                                                                                | round(0) ~ ' %') if debug.get('c_b') is not none else 'Unavailable' }} |
| `control_rate_converged`       | {{ 'Yes' if debug.get('control_rate_converged') else 'No' }}                                                                |
| `b_converged`                  | {{ 'Yes' if debug.get('b_converged') else 'No' }}                                                                           |

| Deadtime                        | Value                                                           |
| ------------------------------- | --------------------------------------------------------------- |
| `nd_hat`                        | {{ debug.get('nd_hat')                                          | round(2) if debug.get('nd_hat') is not none else 'Unavailable' }}                          |
| `deadtime_min`                  | {{ (debug.get('deadtime_min')                                   | round(1) ~ ' min') if debug.get('deadtime_min') is not none else 'Unavailable' }}          |
| `deadtime_locked`               | {{ 'Yes' if debug.get('deadtime_locked') else 'No' }}           |
| `deadtime_on_cycles`            | {{ debug.get('deadtime_on_cycles')                              | round(2) if debug.get('deadtime_on_cycles') is not none else 'Unavailable' }}              |
| `deadtime_on_minutes`           | {{ (debug.get('deadtime_on_minutes')                            | round(1) ~ ' min') if debug.get('deadtime_on_minutes') is not none else 'Unavailable' }}   |
| `deadtime_on_confidence`        | {{ ((debug.get('deadtime_on_confidence') * 100)                 | round(0) ~ ' %') if debug.get('deadtime_on_confidence') is not none else 'Unavailable' }}  |
| `deadtime_on_locked`            | {{ 'Yes' if debug.get('deadtime_on_locked') else 'No' }}        |
| `deadtime_off_cycles`           | {{ debug.get('deadtime_off_cycles')                             | round(2) if debug.get('deadtime_off_cycles') is not none else 'Unavailable' }}             |
| `deadtime_off_minutes`          | {{ (debug.get('deadtime_off_minutes')                           | round(1) ~ ' min') if debug.get('deadtime_off_minutes') is not none else 'Unavailable' }}  |
| `deadtime_off_confidence`       | {{ ((debug.get('deadtime_off_confidence') * 100)                | round(0) ~ ' %') if debug.get('deadtime_off_confidence') is not none else 'Unavailable' }} |
| `deadtime_off_locked`           | {{ 'Yes' if debug.get('deadtime_off_locked') else 'No' }}       |
| `deadtime_pending_step`         | {{ 'Yes' if debug.get('deadtime_pending_step') else 'No' }}     |
| `deadtime_identification_count` | {{ debug.get('deadtime_identification_count', 'unavailable') }} |
| `deadtime_b_proxy`              | {{ debug.get('deadtime_b_proxy')                                | round(4) if debug.get('deadtime_b_proxy') is not none else 'Unavailable' }}                |
| `b_methods_consistent`          | {{ 'Yes' if debug.get('b_methods_consistent') else 'No' }}      |
{% endif %}
{% endif %}
