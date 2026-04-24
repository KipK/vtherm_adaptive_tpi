{% set entity = 'climate.thermostat_bedroom' %}

{% set thermostat_name = none %}
{% set name = thermostat_name or state_attr(entity, 'friendly_name') or entity %}
{% set diag = ((state_attr(entity, 'specific_states') or {}).get('adaptive_tpi')) %}
{% set debug = ((diag or {}).get('debug')) %}
{% set hvac_mode = states(entity) %}
{% set is_cool = hvac_mode == 'cool' %}
{% set icon_control = '❄️' if is_cool else '🔥' %}
{% set icon_drift = '☀️' if is_cool else '💨' %}
{% set label_control = 'Vitesse de froid' if is_cool else 'Vitesse de chauffe' %}
{% set label_drift = 'Dérive thermique (chaud)' if is_cool else 'Dérive thermique (froid)' %}
{% set phase_names = {
  'startup': 'Démarrage',
  'deadtime_learning': 'Temps mort',
  'drift_learning': 'Apprentissage dérive',
  'control_learning': 'Apprentissage contrôle',
  'stabilized': 'Stabilisé'
} %}
{% set stage_names = {
  'idle': 'Inactive',
  'active_to_target': 'Actif vers la consigne',
  'passive_drift_phase': 'Dérive passive',
  'reactivation_to_target': 'Réactivation vers la consigne',
  'completed': 'Terminée',
  'abandoned': 'Interrompue'
} %}
{% set family_names = {
  'control': 'Contrôle',
  'drift': 'Dérive'
} %}
{% if not diag %}
## {{ name }}

Aucune donnée `specific_states.adaptive_tpi` trouvée pour `{{ entity }}`.
{% else %}
{% set phase = phase_names.get(diag.get('adaptive_phase'), diag.get('adaptive_phase') or 'Indisponible') %}
{% set stage = stage_names.get(diag.get('startup_sequence_stage'), diag.get('startup_sequence_stage') or 'Indisponible') %}
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
{% set last_family = family_names.get(diag.get('last_learning_family'), diag.get('last_learning_family') or 'Aucune') %}
{% set last_blocker = diag.get('last_runtime_blocker') %}
{% set current_cycle_text = ((current_cycle * 100) | round(0) ~ ' %') if current_cycle is not none else 'Indisponible' %}
{% set next_cycle_text = ((next_cycle * 100) | round(0) ~ ' %') if next_cycle is not none else 'Indisponible' %}
{% set calculated_cycle = debug.get('calculated_on_percent') if debug else none %}
{% set requested_cycle = debug.get('requested_on_percent') if debug else none %}
{% set committed_cycle = debug.get('committed_on_percent') if debug else none %}
{% set calculated_cycle_text = ((calculated_cycle * 100) | round(0) ~ ' %') if calculated_cycle is not none else 'Indisponible' %}
{% set requested_cycle_text = ((requested_cycle * 100) | round(0) ~ ' %') if requested_cycle is not none else 'Indisponible' %}
{% set committed_cycle_text = ((committed_cycle * 100) | round(0) ~ ' %') if committed_cycle is not none else 'Indisponible' %}
{% set deadtime_text = (deadtime_minutes | round(1) ~ ' min') if deadtime_minutes is not none else ((deadtime_cycles | round(2) ~ ' cycle(s)') if deadtime_cycles is not none else 'Non mesuré') %}
{% set deadtime_conf_text = ((deadtime_confidence * 100) | round(0) ~ ' %') if deadtime_confidence is not none else 'Indisponible' %}
{% set gain_indoor_text = (gain_indoor | round(3)) if gain_indoor is not none else 'Indisponible' %}
{% set gain_outdoor_text = (gain_outdoor | round(3)) if gain_outdoor is not none else 'Indisponible' %}
{% set control_rate_text = (control_rate | round(2) ~ ' °C/h') if control_rate is not none else 'En attente' %}
{% set drift_rate_text = (drift_rate | round(3) ~ ' 1/h') if drift_rate is not none else 'En attente' %}
{% set tau_text = (tau_h | round(2) ~ ' h') if tau_h is not none else 'En attente' %}
{% set actuator_mode_text = actuator_mode or 'Indisponible' %}
{% set valve_curve_status_text = 'Stable' if valve_curve_converged else ('Apprentissage actif' if valve_curve_learning_enabled else 'Figée') %}
{% set valve_curve_reason_text = valve_curve_last_reason if valve_curve_last_reason else 'Aucune' %}

## 🧠 {{ name }}

{% if debug %}`🛠️ Debug actif`{% endif %}

| Vue rapide | Valeur |
|---|---|
| 🧭 Phase | **{{ phase }}** |
| 🌡️ Mode | **{{ hvac_mode }}** |
| {{ icon_control }} Cycle en cours | **{{ current_cycle_text }}** |
| ⏭️ Cycle suivant | **{{ next_cycle_text }}** |
| 🚀 Démarrage | **{{ 'Actif - ' ~ stage if startup_active else 'Inactif' }}** |

| Apprentissage | Valeur |
|---|---|
| ⏳ Temps mort | **{{ deadtime_text }}** |
| 🎯 Confiance temps mort | **{{ deadtime_conf_text }}** |
| 🎚️ Gain intérieur | **{{ gain_indoor_text }}** |
| 🌤️ Gain extérieur | **{{ gain_outdoor_text }}** |
| {{ icon_control }} {{ label_control }} | **{{ control_rate_text }}** |
| {{ icon_drift }} {{ label_drift }} | **{{ drift_rate_text }}** |
| 🏠 Constante thermique | **{{ tau_text }}** |

| État du modèle | Valeur |
|---|---|
| {{ icon_drift }} Modèle dérive | **{{ 'Stable' if drift_converged else 'En apprentissage' }}** |
| {{ icon_control }} Modèle contrôle | **{{ 'Stable' if control_converged else 'En apprentissage' }}** |
| {{ icon_control }} Apprentissage contrôle | **{{ 'Autorisé' if control_enabled else 'Pas encore' }}** |
| {{ icon_drift }} Échantillons dérive | **{{ (drift_samples if drift_samples is not none else 0) ~ ' / ' ~ sample_window_size }}** |
| {{ icon_control }} Échantillons contrôle | **{{ (control_samples if control_samples is not none else 0) ~ ' / ' ~ sample_window_size }}** |

{% if actuator_mode == 'valve' %}
| Courbe de vanne | Valeur |
|---|---|
| 🧩 Mode actionneur | **{{ actuator_mode_text }}** |
| 📈 Apprentissage courbe | **{{ valve_curve_status_text }}** |
| ✅ Observations acceptées | **{{ valve_curve_observations_accepted if valve_curve_observations_accepted is not none else 0 }}** |
| 🚫 Observations rejetées | **{{ valve_curve_observations_rejected if valve_curve_observations_rejected is not none else 0 }}** |
| 📝 Dernier résultat courbe | **{{ '`' ~ valve_curve_reason_text ~ '`' }}** |

| Demande vs puissance appliquée | Valeur |
|---|---|
| 🎯 Demande linéaire | **{{ calculated_cycle_text }}** |
| 🛞 Commande vanne demandée | **{{ requested_cycle_text }}** |
| ⚙️ Puissance appliquée | **{{ committed_cycle_text }}** |

| Paramètres de courbe | Valeur |
|---|---|
| `min_valve` | **{{ valve_curve_params.get('min_valve') | round(1) }} %** |
| `knee_demand` | **{{ valve_curve_params.get('knee_demand') | round(1) }} %** |
| `knee_valve` | **{{ valve_curve_params.get('knee_valve') | round(1) }} %** |
| `max_valve` | **{{ valve_curve_params.get('max_valve') | round(1) }} %** |
{% endif %}

| Dernière activité | Valeur |
|---|---|
| 🔁 Famille utilisée | **{{ last_family }}** |
| ✅ Résultat | **{{ '`' ~ last_result ~ '`' if last_result else 'Aucun récent' }}** |
| 🚧 Blocage | **{{ '`' ~ last_blocker ~ '`' if last_blocker else 'Aucun' }}** |

{% if startup_active or startup_done %}
| Séquence de démarrage | Valeur |
|---|---|
| 🪜 Étape | **{{ stage }}** |
| 🔄 Tentative | **{{ startup_attempt ~ ' / ' ~ startup_max if startup_attempt is not none and startup_max is not none else 'Indisponible' }}** |
| 🏁 Fin de séquence | **{{ '`' ~ startup_done ~ '`' if startup_done else 'En cours' }}** |
{% endif %}

{% if debug %}
## 🛠️ Debug

| Puissance / cycles | Valeur |
|---|---|
| `calculated_on_percent` | {{ ((debug.get('calculated_on_percent') * 100) | round(0) ~ ' %') if debug.get('calculated_on_percent') is not none else 'Indisponible' }} |
| `requested_on_percent` | {{ ((debug.get('requested_on_percent') * 100) | round(0) ~ ' %') if debug.get('requested_on_percent') is not none else 'Indisponible' }} |
| `committed_on_percent` | {{ ((debug.get('committed_on_percent') * 100) | round(0) ~ ' %') if debug.get('committed_on_percent') is not none else 'Indisponible' }} |
| `current_cycle_regime` | `{{ debug.get('current_cycle_regime', 'indisponible') }}` |
| `last_cycle_classification` | `{{ debug.get('last_cycle_classification', 'indisponible') }}` |
| `accepted_cycles_count` | {{ debug.get('accepted_cycles_count', 'indisponible') }} |
| `valid_cycles_count` | {{ debug.get('valid_cycles_count', 'indisponible') }} |

| Routage / estimation | Valeur |
|---|---|
| `learning_route_selected` | `{{ debug.get('learning_route_selected', 'indisponible') }}` |
| `learning_route_block_reason` | {{ '`' ~ debug.get('learning_route_block_reason') ~ '`' if debug.get('learning_route_block_reason') else 'Aucun' }} |
| `last_learning_attempt_reason` | {{ '`' ~ debug.get('last_learning_attempt_reason') ~ '`' if debug.get('last_learning_attempt_reason') else 'Indisponible' }} |
| `a_hat` | {{ debug.get('a_hat') | round(4) if debug.get('a_hat') is not none else 'Indisponible' }} |
| `b_hat` | {{ debug.get('b_hat') | round(4) if debug.get('b_hat') is not none else 'Indisponible' }} |
| `c_a` | {{ ((debug.get('c_a') * 100) | round(0) ~ ' %') if debug.get('c_a') is not none else 'Indisponible' }} |
| `c_b` | {{ ((debug.get('c_b') * 100) | round(0) ~ ' %') if debug.get('c_b') is not none else 'Indisponible' }} |
| `control_rate_converged` | {{ 'Oui' if debug.get('control_rate_converged') else 'Non' }} |
| `b_converged` | {{ 'Oui' if debug.get('b_converged') else 'Non' }} |

| Temps mort | Valeur |
|---|---|
| `nd_hat` | {{ debug.get('nd_hat') | round(2) if debug.get('nd_hat') is not none else 'Indisponible' }} |
| `deadtime_min` | {{ (debug.get('deadtime_min') | round(1) ~ ' min') if debug.get('deadtime_min') is not none else 'Indisponible' }} |
| `deadtime_locked` | {{ 'Oui' if debug.get('deadtime_locked') else 'Non' }} |
| `deadtime_pending_step` | {{ 'Oui' if debug.get('deadtime_pending_step') else 'Non' }} |
| `deadtime_identification_count` | {{ debug.get('deadtime_identification_count', 'indisponible') }} |
| `deadtime_b_proxy` | {{ debug.get('deadtime_b_proxy') | round(4) if debug.get('deadtime_b_proxy') is not none else 'Indisponible' }} |
| `b_methods_consistent` | {{ 'Oui' if debug.get('b_methods_consistent') else 'Non' }} |
{% endif %}
{% endif %}
