# Architecture adaptative TPI

## En cours de développement

Ce document décrit l'état d'implémentation actuel de `vtherm_adaptive_tpi`.

Ce n'est pas une spécification gelée. Le projet évolue toujours, et certains détails peuvent changer à mesure que la logique d'apprentissage est affinée sur la télémétrie réelle.

## Flux de haut niveau

Le plugin est attaché à Versatile Thermostat via `vtherm_api` et réagit à deux types d'événements :

1. les rafraîchissements réguliers de régulation
2. les limites de cycle de l'ordonnanceur

À un haut niveau :

1. `calculate()` calcule le `on_percent` demandé pour le prochain cycle
2. l'ordonnanceur VT engage un cycle réel avec sa puissance appliquée
3. le plugin enregistre le contexte de début de cycle
4. à la fin du cycle, le plugin décide si le cycle est valide pour l'apprentissage
5. s'il est valide, il met à jour :
   - la recherche de temps mort
   - l'apprentissage OFF routé par régime pour `b`
   - l'apprentissage ON routé par régime pour `a`
6. les gains projetés `k_int` et `k_ext` sont rafraîchis de manière conservatrice

## Modules principaux

### `handler.py`

Colle d'interface avec Home Assistant et Versatile Thermostat.

Responsabilités :

- initialiser l'algorithme adaptif
- lier les rappels de l'ordonnanceur
- sauvegarder et restaurer l'état persistant
- publier les diagnostiques dans les `specific_states` du climat

### `algo.py`

Couche d'orchestration à l'exécution.

Responsabilités :

- calculer le `on_percent` demandé
- capturer les données de cycle engagées
- valider les conditions d'apprentissage
- acheminer les cycles vers le temps mort, `b`, ou `a`
- mettre à jour l'état d'exécution public
- rafraîchir les gains projetés

### `adaptive_tpi/deadtime.py`

Estimateur de temps mort approximatif utilisant une méthode de temps jusqu'à la première montée.

Responsabilités :

- garder un historique de cycle temporellement contigu
- détecter les fronts de montée de puissance (transitions OFF→ON) et mesurer le délai jusqu'à la première hausse visible de température (`RISE_EPSILON` cumulatif ou `RISE_EPSILON_STEP` par cycle)
- agréger les identifications via la médiane pondérée sur les derniers `N_HIST` événements et verrouiller quand l'écart (en cycles) et les conditions de qualité sont remplies
- exposer :
  - `nd_hat`
  - `c_nd`
  - les meilleurs et deuxième meilleur candidats
  - un proxy côté temps mort pour `b`

Note importante :

- la recherche de temps mort utilise tous les cycles réels gardés dans l'historique aligné
- certains cycles sont valides pour l'historique mais pas informatifs pour la notation

### `adaptive_tpi/learning_window.py`

Construit des fenêtres d'apprentissage courtes et bornées à partir de l'historique des cycles réels.

Responsabilités :

- reconstruire les fenêtres OFF récentes pour `b`
- reconstruire les fenêtres ON récentes pour `a`
- ancrer les fenêtres sur le cycle actuellement complété
- appliquer des fenêtres bornées par une politique adaptative
- rejeter les fenêtres quand :
  - le signal est trop faible
  - le signe du régime est incohérent
  - un changement de point de consigne récent contredit le régime
  - la fenêtre intersecte toujours l'extinction de temps mort post-transition

Le silence d'apprentissage dépend du temps mort :

- silence de `ceil(nd_hat)` cycles après une transition de régime
- avec un silence de sécurité minimum de `1` cycle quand le temps mort n'est pas encore connu

La garde de saut de point de consigne est orientée par régime :

- les fenêtres ON tolèrent les sauts de point de consigne vers le haut qui renforcent le chauffage
- les fenêtres OFF tolèrent les sauts de point de consigne vers le bas qui renforcent le régime sans chauffage actuel
- les sauts contradictoires invalident toujours la fenêtre

Les limites de taille de fenêtre sont adaptatives (voir `adaptive_tpi/learning_policy.py`) :

- `max_cycles` s'adapte à la durée de cycle pour ne pas bloquer les cycles courts
- la durée maximale est de 120 min (plus conservateur que les 240 min de SmartPI)

La validation du signal thermique utilise deux niveaux :

- **standard** : `|amplitude| ≥ 0,08 °C` et `durée ≥ 8 min`
- **relaxé** : `|amplitude| ≥ 0,05 °C` et `durée ≥ 8 min` et au moins 2 variations dans le bon sens

Un **démarrage glissant** est tenté quand la fenêtre complète a le mauvais signe thermique
à cause d'une inertie post-transition ; la fenêtre est ancrée sur le premier point où le signe se corrige.

Les fenêtres OFF transportent toujours `allow_near_setpoint_b = True` pour que l'estimateur
ne les rejette pas en raison d'un faible écart au point de consigne —
`b = -dTdt / delta_out` n'en a pas besoin.

### `adaptive_tpi/learning_policy.py`

Calcule la politique de fenêtre adaptative utilisée par `learning_window.py`.

Responsabilités :

- dériver `max_cycles` et `max_duration_min` à partir de la durée de cycle courante
- exposer les seuils d'amplitude standard et relaxé
- garder la construction de politique isolée de la logique de fenêtre

### `adaptive_tpi/estimator.py`

Estimateurs découplés pour `b` et `a`.

Responsabilités :

- apprendre `b` à partir des fenêtres OFF ou quasi-OFF
- apprendre `a` à partir des fenêtres ON
- garder les estimations bornées et les valeurs de confiance
- exposer les comptages d'échantillons et les dernières raisons de rejet

Choix de conception :

- l'estimateur utilise un estimateur robuste roulant borné
- c'est intentionnellement plus simple qu'une approche LMS/RLS en ligne plus agressive
- `b` utilise `MIN_B_DELTA_OUT = 0,5` — exploitable avec un contraste extérieur modéré
- `a` garde `MIN_A_DELTA_OUT = 1,0` — plus conservateur
- la gate d'écart au point de consigne (`MIN_SETPOINT_ERROR`) s'applique à `a` uniquement ;
  `b` la contourne quand `allow_near_setpoint_b` est positionné par la fenêtre d'apprentissage

### `adaptive_tpi/controller.py`

Projection de gain et calcul de commande.

Responsabilités :

- dériver les objectifs de gain structurel à partir de `a_hat`, `b_hat` et `nd_hat`
- projeter les gains lentement avec les limites de taux bornées
- calculer le `on_percent` nominal demandé

### `adaptive_tpi/startup_bootstrap.py`

Remplacement de commande au démarrage utilisé avant la première identification de temps mort.

Responsabilités :

- forcer une séquence de démarrage propre autour du point de consigne
- garder la séquence bornée à au maximum deux tentatives d'identification OFF->ON
- exposer les diagnostiques détaillés de démarrage-bootstrap consommés par l'état d'exécution

## Séquence d'apprentissage

### 1. Démarrage du cycle

Quand VT démarre un cycle réel, le plugin capture :

- la température cible
- la température intérieure
- la température extérieure
- la puissance appliquée
- le mode hvac

Cet instantané devient le contexte de cycle en attente.

Avant l'existence du temps mort, l'exécution peut temporairement contourner la commande P+anticipation nominale et utiliser la séquence de bootstrap de démarrage à la place :

- si `current_temp >= target_temp`, commander `0%` jusqu'à `target_temp - 0.3°C`
- si `current_temp < target_temp`, commander `100%` jusqu'à `target_temp`
- une fois que la pièce a atteint `target_temp`, commander `0%` jusqu'à `target_temp - 0.3°C`
- à partir de `target_temp - 0.3°C`, commander `100%` jusqu'à `target_temp`
- chaque franchissement de seuil du bootstrap force un redémarrage immédiat de l'ordonnanceur afin que le cycle actuel puisse se terminer sans attendre sa limite nominale
- si aucune identification de temps mort n'a été produite, répéter un cycle OFF->ON supplémentaire
- après la deuxième tentative infructueuse, revenir à la régulation nominale
- les cycles de refroidissement OFF créés par cette séquence peuvent également alimenter les premières mises à jour de `b`, même s'ils commencent près du point de consigne

### 2. Fin de cycle

À la fin du cycle :

- le contexte de cycle engagé capturé au démarrage est conservé pour l'apprentissage
- les cycles interrompus sont rejetés de l'apprentissage
- les cycles acceptés sont ajoutés à l'historique du modèle de temps mort

### 3. Mise à jour du temps mort

Le modèle de temps mort évalue l'ensemble de candidats et met à jour :

- `nd_hat`
- `c_nd`
- `deadtime_locked`
- les coûts des candidats

Il expose également un proxy `b` temporaire à partir de l'ajustement du meilleur candidat.

### 4. Extraction de fenêtre

L'algorithme classe d'abord le cycle complété dans un régime approximatif :

- `off`
- `on`
- `mixed`

Il construit ensuite une fenêtre ancrée pour ce même cycle complété :

- une fenêtre OFF pour `b`
- ou une fenêtre ON pour `a`

L'exécution ne recherche plus l'historique complet pour quel que soit le régime qui se présente en premier.
Le cycle actuellement complété décide de la route d'apprentissage.

### 5. Estimation

La logique d'acheminement est :

- `b` peut apprendre des fenêtres OFF acheminées à partir d'un cycle complété OFF
- `a` attend :
  - un temps mort crédible
  - `b` convergent
- à la fois `a` et `b` sont bloqués tant que la fenêtre candidate se trouve dans le silence de temps mort après une transition de régime
- les cycles mixtes n'alimentent pas `a` ou `b`

Le proxy côté temps mort `b` est également utilisé comme une graine de bootstrap légère pour l'estimateur `b` explicite quand aucun échantillon OFF n'a été accepté encore.

### 6. Projection de gain

Une fois les estimations disponibles, les gains sont projetés de manière conservatrice :

- borné par les limites de taux dépendantes de la phase
- pondérés par la confiance
- fixés aux plages sûres

## Philosophie des diagnostiques

Les diagnostiques du climat sont destinés à répondre à trois questions pratiques :

1. l'ordonnanceur livre-t-il réellement des cycles complets ?
2. le cycle est-il accepté pour l'apprentissage ?
3. si non, où est-il bloqué ?

Groupes de diagnostic utiles :

- flux de cycle :
  - `debug["cycle_started_calls_count"]`
  - `debug["cycle_completed_calls_count"]`
  - `debug["last_cycle_started_at"]`
  - `debug["last_cycle_completed_at"]`
- temps mort :
  - `deadtime_cycles`
  - `deadtime_confidence`
  - `debug["deadtime_identification_qualities"]`
  - `debug["deadtime_b_proxy"]`
- bootstrap au démarrage :
  - `startup_sequence_active`
  - `startup_sequence_stage`
  - `startup_sequence_attempt`
  - `startup_sequence_completion_reason`
- estimateur :
  - `control_rate_per_hour`
  - `drift_rate_per_hour`
  - `control_rate_confidence`
  - `drift_rate_confidence`
  - `control_rate_converged`
  - `control_samples`
  - `drift_samples`
  - `debug["a_last_reason"]`
  - `debug["b_last_reason"]`
- acheminement :
  - `control_learning_enabled`
  - `debug["current_cycle_regime"]`
  - `debug["learning_route_selected"]`
  - `debug["learning_route_block_reason"]`
  - `debug["deadtime_learning_blackout_active"]`
- vérification croisée :
  - `debug["deadtime_b_proxy"]`
  - `debug["b_crosscheck_error"]`
  - `debug["b_methods_consistent"]`

## Phases de bootstrap

L'algorithme progresse à travers une séquence de phases contrôlées par `supervisor.py`.
Chaque phase détermine quelles opérations d'apprentissage sont permises et à quelle agressivité les gains peuvent se déplacer.

### Progression de phase

```
STARTUP → A → B → C → D
```

Les phases n'avancent qu'en avant. Un réinitialisation (`reset_learning`) retourne à STARTUP.
Un démarrage à chaud après une longue absence peut revenir à A ou B (voir la section Démarrage à chaud ci-dessous).

---

### STARTUP

Entrée : lors de la première initialisation ou après une réinitialisation complète.

- Aucun cycle accepté encore.
- Les gains sont maintenus à `default_kint` / `default_kext`.
- Pas de recherche de temps mort, pas d'estimation.

Sortie : immédiatement au premier cycle valide accepté → avancer à A.

---

### Phase A

Entrée : premier cycle valide reçu.

Objectif : accumuler suffisamment d'observations pour commencer la recherche de temps mort.

- Les gains sont gelés (limite de taux = 0).
- La recherche de temps mort s'exécute et accumule l'historique.
- L'estimation `b` est bloquée.
- L'estimation `a` est bloquée.

Conditions de sortie (toutes requises) :
- `valid_cycles_count ≥ 5`
- `informative_deadtime_cycles_count ≥ 3`

Détection de blocage : si ≥ 10 cycles valides ont passé et `c_nd` reste en dessous de 0.2, `last_freeze_reason` est défini sur `"insufficient_excitation_bootstrap"`.

---

### Phase B

Entrée : suffisamment de cycles pour la recherche de temps mort.

Objectif : identifier le temps mort et converger `b`.

- Les gains se déplacent lentement : `delta_kint_max = 0.01`, `delta_kext_max = 0.002`.
- La recherche de temps mort continue.
- L'estimation `b` est autorisée (fenêtres OFF uniquement).
- L'estimation `a` est toujours bloquée.

Conditions de sortie (toutes requises) :
- `deadtime_locked = True`
- `c_nd ≥ 0.6`
- `b_converged = True`

---

### Phase C

Entrée : temps mort verrouillé et `b` convergent.

Objectif : apprentissage actif — à la fois `a` et `b` se mettent à jour, les gains se déplacent vers les objectifs structurels.

- Les gains se déplacent plus rapidement : `delta_kint_max = 0.03`, `delta_kext_max = 0.005`.
- L'estimation `b` continue (fenêtres OFF).
- L'estimation `a` est activée (fenêtres ON, nécessite le temps mort verrouillé et `b` convergent).
- Le compteur `adaptive_cycles_since_phase_c` est réinitialisé à 0 à l'entrée.

Conditions de sortie (toutes requises, vérifiées après chaque mise à jour d'estimateur) :
- `c_a ≥ 0.6` et `c_b ≥ 0.5`
- `adaptive_cycles_since_phase_c ≥ 20`
- `a` et `b` se sont chacun déplacés de moins de 10 % sur les 11 derniers cycles acceptés

---

### Phase D

Entrée : `a` et `b` ont convergé dans la Phase C.

Objectif : fonctionnement à l'état stable à long terme.

- Les gains se déplacent lentement à nouveau : `delta_kint_max = 0.01`, `delta_kext_max = 0.002`.
- À la fois `a` et `b` continuent à s'adapter lentement.
- C'est le régime de fonctionnement nominal.

Pas de sortie automatique. La phase reste D indéfiniment à moins qu'une revalidation de démarrage à chaud se produise.

---

### Résumé des limites de taux de gain

| Phase   | `delta_kint_max` | `delta_kext_max` |
|---------|------------------|------------------|
| STARTUP | —  (fixe)        | —  (fixe)        |
| A       | 0.0              | 0.0              |
| B       | 0.01             | 0.002            |
| C       | 0.03             | 0.005            |
| D       | 0.01             | 0.002            |

---

### Démarrage à chaud et revalidation de phase

Quand l'état persistant est chargé après une pause :

- **Écart > 30 jours** : les confiances sont réduites de moitié (`decay_confidences(0.5)`). Si `c_nd` tombe en dessous de 0.6, `deadtime_locked` est effacé et la phase est reculée à B.
- **Écart > 90 jours** : les confiances sont complètement réinitialisées et la phase est reculée à A.
- **`cycle_min` changé** : les valeurs persistées de `a` et `b` sont stockées par heure, et le temps mort est stocké en minutes. Au chargement elles sont converties vers la période courante de l'ordonnanceur, en conservant les confiances, les échantillons et la phase.

---

### `deadtime_locked` et ce qui l'efface

`deadtime_locked` est recalculé à chaque cycle. C'est `False` quand l'un des éléments suivants est vrai :

- moins de 10 cycles acceptés dans le modèle de temps mort (`"deadtime_insufficient_cycles"`)
- rapport de dominance du meilleur candidat < 2.0 sur le deuxième meilleur (`"deadtime_insufficient_separation"`)
- le meilleur candidat a remporté moins de 7 des 10 derniers cycles (`"deadtime_inconsistent_winner"`)
- décroissance de confiance après > 30 jours réduit `c_nd` en dessous de 0.6
- réinitialisation complète de confiance après un écart > 90 jours

Le diagnostic compact `last_runtime_blocker` nomme toujours le bloqueur actif.

## Limites connues

Limites actuellement connues du prototype :

- les seuils sont toujours conservateurs et peuvent nécessiter un ajustement sur les données de terrain
- la confiance de temps mort peut augmenter lentement sur les traces clairsemées ou de faible contraste
- `a` commence intentionnellement plus tard que `b`
- le plugin est toujours au stade expérimental et non finalisé pour une utilisation en production
