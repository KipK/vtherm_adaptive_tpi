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
{% set deadtime_minutes = diag.get('deadtime_minutes') %}
{% set deadtime_cycles = diag.get('deadtime_cycles') %}
{% set deadtime_confidence = diag.get('deadtime_confidence') %}
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
{% set deadtime_text = (deadtime_minutes | round(1) ~ ' min') if deadtime_minutes is not none else ((deadtime_cycles | round(2) ~ ' cycle(s)') if deadtime_cycles is not none else 'Not measured') %}
{% set deadtime_conf_text = ((deadtime_confidence * 100) | round(0) ~ ' %') if deadtime_confidence is not none else 'Unavailable' %}
{% set control_rate_text = (control_rate | round(2) ~ ' °C/h') if control_rate is not none else 'Pending' %}
{% set drift_rate_text = (drift_rate | round(3) ~ ' 1/h') if drift_rate is not none else 'Pending' %}
{% set tau_text = (tau_h | round(2) ~ ' h') if tau_h is not none else 'Pending' %}

## 🧠 {{ name }}

{% if debug %}`🛠️ Debug active`{% endif %}

| Overview | Value |
|---|---|
| 🧭 Phase | **{{ phase }}** |
| 🌡️ Mode | **{{ hvac_mode }}** |
| {{ icon_control }} Current cycle | **{{ current_cycle_text }}** |
| ⏭️ Next cycle | **{{ next_cycle_text }}** |
| 🚀 Startup | **{{ 'Active - ' ~ stage if startup_active else 'Inactive' }}** |

| Learning | Value |
|---|---|
| ⏳ Deadtime | **{{ deadtime_text }}** |
| 🎯 Deadtime confidence | **{{ deadtime_conf_text }}** |
| {{ icon_control }} {{ label_control }} | **{{ control_rate_text }}** |
| {{ icon_drift }} {{ label_drift }} | **{{ drift_rate_text }}** |
| 🏠 Thermal constant | **{{ tau_text }}** |

| Model status | Value |
|---|---|
| {{ icon_drift }} Drift model | **{{ 'Stable' if drift_converged else 'Learning' }}** |
| {{ icon_control }} Control model | **{{ 'Stable' if control_converged else 'Learning' }}** |
| {{ icon_control }} Control learning | **{{ 'Allowed' if control_enabled else 'Not yet' }}** |
| {{ icon_drift }} Drift samples | **{{ (drift_samples if drift_samples is not none else 0) ~ ' / ' ~ sample_window_size }}** |
| {{ icon_control }} Control samples | **{{ (control_samples if control_samples is not none else 0) ~ ' / ' ~ sample_window_size }}** |

| Recent activity | Value |
|---|---|
| 🔁 Last family | **{{ last_family }}** |
| ✅ Last result | **{{ '`' ~ last_result ~ '`' if last_result else 'No recent result' }}** |
| 🚧 Blocker | **{{ '`' ~ last_blocker ~ '`' if last_blocker else 'None' }}** |

{% if startup_active or startup_done %}
| Startup sequence | Value |
|---|---|
| 🪜 Stage | **{{ stage }}** |
| 🔄 Attempt | **{{ startup_attempt ~ ' / ' ~ startup_max if startup_attempt is not none and startup_max is not none else 'Unavailable' }}** |
| 🏁 Completion | **{{ '`' ~ startup_done ~ '`' if startup_done else 'In progress' }}** |
{% endif %}

{% if debug %}
## 🛠️ Debug

| Power / cycles | Value |
|---|---|
| `calculated_on_percent` | {{ ((debug.get('calculated_on_percent') * 100) | round(0) ~ ' %') if debug.get('calculated_on_percent') is not none else 'Unavailable' }} |
| `requested_on_percent` | {{ ((debug.get('requested_on_percent') * 100) | round(0) ~ ' %') if debug.get('requested_on_percent') is not none else 'Unavailable' }} |
| `committed_on_percent` | {{ ((debug.get('committed_on_percent') * 100) | round(0) ~ ' %') if debug.get('committed_on_percent') is not none else 'Unavailable' }} |
| `current_cycle_regime` | `{{ debug.get('current_cycle_regime', 'unavailable') }}` |
| `last_cycle_classification` | `{{ debug.get('last_cycle_classification', 'unavailable') }}` |
| `accepted_cycles_count` | {{ debug.get('accepted_cycles_count', 'unavailable') }} |
| `valid_cycles_count` | {{ debug.get('valid_cycles_count', 'unavailable') }} |

| Routing / estimation | Value |
|---|---|
| `learning_route_selected` | `{{ debug.get('learning_route_selected', 'unavailable') }}` |
| `learning_route_block_reason` | {{ '`' ~ debug.get('learning_route_block_reason') ~ '`' if debug.get('learning_route_block_reason') else 'None' }} |
| `last_learning_attempt_reason` | {{ '`' ~ debug.get('last_learning_attempt_reason') ~ '`' if debug.get('last_learning_attempt_reason') else 'Unavailable' }} |
| `a_hat` | {{ debug.get('a_hat') | round(4) if debug.get('a_hat') is not none else 'Unavailable' }} |
| `b_hat` | {{ debug.get('b_hat') | round(4) if debug.get('b_hat') is not none else 'Unavailable' }} |
| `c_a` | {{ ((debug.get('c_a') * 100) | round(0) ~ ' %') if debug.get('c_a') is not none else 'Unavailable' }} |
| `c_b` | {{ ((debug.get('c_b') * 100) | round(0) ~ ' %') if debug.get('c_b') is not none else 'Unavailable' }} |
| `control_rate_converged` | {{ 'Yes' if debug.get('control_rate_converged') else 'No' }} |
| `b_converged` | {{ 'Yes' if debug.get('b_converged') else 'No' }} |

| Deadtime | Value |
|---|---|
| `nd_hat` | {{ debug.get('nd_hat') | round(2) if debug.get('nd_hat') is not none else 'Unavailable' }} |
| `deadtime_min` | {{ (debug.get('deadtime_min') | round(1) ~ ' min') if debug.get('deadtime_min') is not none else 'Unavailable' }} |
| `deadtime_locked` | {{ 'Yes' if debug.get('deadtime_locked') else 'No' }} |
| `deadtime_pending_step` | {{ 'Yes' if debug.get('deadtime_pending_step') else 'No' }} |
| `deadtime_identification_count` | {{ debug.get('deadtime_identification_count', 'unavailable') }} |
| `deadtime_b_proxy` | {{ debug.get('deadtime_b_proxy') | round(4) if debug.get('deadtime_b_proxy') is not none else 'Unavailable' }} |
| `b_methods_consistent` | {{ 'Yes' if debug.get('b_methods_consistent') else 'No' }} |
{% endif %}
{% endif %}
