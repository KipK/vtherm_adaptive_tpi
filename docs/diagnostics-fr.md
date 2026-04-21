# Diagnostiques adaptés TPI

## Objectif

Ce document explique les diagnostiques exposés par `adaptive_tpi` dans les `specific_states` du climat.

Les diagnostiques sont divisés en deux couches :

- une vue sans débogage compacte destinée à la surveillance normale
- une vue plus grande destinée au débogage destinée au réglage fin et à l'analyse interne

## Diagnostiques sans débogage

Ces clés sont les diagnostiques principaux destinés à l'utilisateur.

### Phase d'apprentissage

- `adaptive_phase`

Valeurs possibles :

- `startup`
- `deadtime_learning`
- `cooling_learning`
- `heating_learning`
- `stabilized`

### Gains

- `gain_indoor`
- `gain_outdoor`

Ce sont les gains projetés actuellement utilisés par le contrôleur.

### Temps mort

- `deadtime_cycles`
- `deadtime_minutes`
- `deadtime_confidence`

Interprétation :

- `deadtime_cycles` est l'estimation actuelle du temps mort en cycles de l'ordonnanceur
- `deadtime_minutes` est l'estimation mesurée du temps mort en minutes avant conversion en cycles
- si aucune valeur mesurée en minutes n'est encore disponible, `deadtime_minutes` revient à la valeur normalisée construite depuis `deadtime_cycles` et la dernière durée de cycle acceptée
- `deadtime_confidence` est la confiance dans cette estimation, dans `[0, 1]`

### Taux de chauffage et de refroidissement

- `heating_rate_per_hour`
- `cooling_rate_per_hour`
- `thermal_time_constant_hours`
- `heating_rate_confidence`
- `cooling_rate_confidence`
- `cooling_rate_converged`

Interprétation :

- `heating_rate_per_hour` est l'autorité de chauffage apprise normalisée en `°C/heure`
- `cooling_rate_per_hour` est le taux de perte de refroidissement appris normalisé en `1/heure`
- `thermal_time_constant_hours` est dérivé du taux de refroidissement
- `cooling_rate_converged` indique si l'estimation du refroidissement est stable enough pour ouvrir l'apprentissage du chauffage

### Compteurs d'échantillons

- `heating_samples`
- `cooling_samples`
- `heating_learning_enabled`

Interprétation :

- `heating_samples` compte les mises à jour `a` acceptées
- `cooling_samples` compte les mises à jour `b` acceptées
- `heating_learning_enabled` indique si les conditions d'exécution permettent l'apprentissage `a` quand une fenêtre ON est valide

### Séquence de démarrage

- `startup_sequence_active`
- `startup_sequence_stage`
- `startup_sequence_attempt`
- `startup_sequence_max_attempts`
- `startup_sequence_completion_reason`

Valeurs possibles de `startup_sequence_stage` :

- `idle`
- `heating_to_target`
- `cooling_below_target`
- `reheating_to_target`
- `completed`
- `abandoned`

Interprétation :

- `startup_sequence_active = true` signifie que le bootstrap de démarrage remplace actuellement la commande nominale
- `current_cycle_percent` est la puissance engagée pour le cycle actuellement en cours
- `next_cycle_percent` est la puissance demandée préparée pour le prochain cycle de l'ordonnanceur

### Dernier résultat et bloqueur

- `last_learning_result`
- `last_learning_family`
- `last_runtime_blocker`

Interprétation :

- `last_learning_result` est le dernier résultat d'apprentissage ou la raison du rejet
- `last_learning_family` identifie la branche considérée en dernier :
  - `heating`
  - `cooling`
- `last_runtime_blocker` est la dernière raison de gel d'exécution du superviseur

## Diagnostiques de débogage

Quand le mode de débogage est activé, les diagnostiques incluent un mappage `debug` avec les champs internes techniques.

Ce mappage conserve les noms orientés implémentation utilisés par l'algorithme.

### Groupes techniques principaux

- superviseur et phase :
  - `bootstrap_phase`
  - `phase`
  - `last_freeze_reason`
- gains et estimateurs :
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
- temps mort :
  - `nd_hat`
  - `nd_hat_cycles`
  - `deadtime_min`
- `deadtime_min` suit la même règle "minutes mesurées d'abord" que `deadtime_minutes`
  - `c_nd`
  - `deadtime_identification_count`
  - `deadtime_identification_qualities`
  - `deadtime_b_proxy`
  - `deadtime_locked`
  - `deadtime_pending_step`
  - `deadtime_best_candidate`
  - `deadtime_second_best_candidate`
- bootstrap au démarrage :
  - `startup_bootstrap_active`
  - `startup_bootstrap_stage`
  - `startup_bootstrap_attempt`
  - `startup_bootstrap_max_attempts`
  - `startup_bootstrap_target_temp`
  - `startup_bootstrap_lower_target_temp`
  - `startup_bootstrap_command_on_percent`
  - `startup_bootstrap_completion_reason`
- acheminement :
  - `current_cycle_regime`
  - `learning_route_selected`
  - `learning_route_block_reason`
  - `deadtime_learning_blackout_active`
  - `a_learning_enabled`
  - `a_last_reason`
  - `b_last_reason`
  - `last_learning_attempt_reason`
  - `last_learning_attempt_regime`
- flux de cycle :
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
- vérification croisée :
  - `b_crosscheck_error`
  - `b_methods_consistent`

## Ordre de lecture pratique

Pour la surveillance normale, lisez les diagnostiques dans cet ordre :

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

## Situations courantes

### Le démarrage s'exécute toujours

Regardez :

- `startup_sequence_active`
- `startup_sequence_stage`
- `startup_sequence_attempt`
- `startup_sequence_completion_reason`
- `deadtime_cycles`

### Le refroidissement ne progresse pas

Regardez :

- `cooling_samples`
- `cooling_rate_per_hour`
- `cooling_rate_confidence`
- `last_learning_result`

Si nécessaire, activez le mode débogage et inspectez :

- `debug["b_last_reason"]`
- `debug["learning_route_selected"]`
- `debug["learning_route_block_reason"]`

### L'apprentissage du chauffage ne commence pas

Regardez :

- `heating_learning_enabled`
- `cooling_rate_converged`
- `deadtime_confidence`
- `heating_samples`
- `last_runtime_blocker`

Si nécessaire, activez le mode débogage et inspectez :

- `debug["a_last_reason"]`

### La régulation semble toujours gelée

Regardez :

- `last_runtime_blocker`
- `gain_indoor`
- `gain_outdoor`

Si nécessaire, activez le mode débogage et inspectez :

- `debug["last_cycle_classification"]`
- `debug["calculated_on_percent"]`
- `debug["requested_on_percent"]`
- `debug["committed_on_percent"]`

## Note de persistance

Les diagnostiques compacts destinés à l'utilisateur décrivent l'état adaptatif actuel.

Les champs d'acheminement de débogage restent orientés vers l'exécution et ne sont pas destinés à être un contrat de persistance stable pour les tableaux de bord à travers les redémarrages.
