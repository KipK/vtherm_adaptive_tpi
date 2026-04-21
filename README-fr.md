# vtherm_adaptive_tpi

Plugin adaptif TPI pour [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat), construit sur la base de `vtherm_api`.

## Ce qu'il fait

`vtherm_adaptive_tpi` fournit un algorithme proportionnel `adaptive_tpi` externe pour Versatile Thermostat.

Son objectif est d'apprendre, durant le fonctionnement normal du thermostat :

- le temps mort (`nd`)
- les pertes thermiques (`b`)
- l'autorité de chauffage (`a`)

et d'utiliser ces valeurs apprises pour ajuster les gains du thermostat au fil du temps.

Le plugin reste dans la famille TPI :

- il calcule un `on_percent` demandé pour le prochain cycle
- Versatile Thermostat engage toujours la puissance réellement appliquée au cycle courant via son ordonnanceur de cycle normal
- l'apprentissage ne se fait que sur les cycles réels complétés

TPI est un algorithme de régulation construit autour d'une boucle proportionnelle via `gain_indoor` plus un terme d'anticipation via `gain_outdoor` pour compenser les pertes thermiques. Il n'y a pas de terme de correction intégrale utilisé pour annuler les erreurs en régime permanent, donc le `I` dans `TPI` peut être trompeur.

Si vous avez besoin d'un contrôleur plus avancé proportionnel-intégral avec anticipation, voir [vtherm-smartpi](https://github.com/KipK/vtherm_smartpi/).

L'intégration comprend :

- l'échafaudage d'intégration Home Assistant
- l'enregistrement via `vtherm_api`
- la connexion à l'exécution aux rappels de cycle de Versatile Thermostat
- l'estimation approximative du temps mort
- l'apprentissage OFF-window pour `b`
- l'apprentissage ON-window pour `a`
- la projection de gain conservatrice
- l'état d'exécution persistant
- les diagnostiques exposés dans les `specific_states` du climat

## Aperçu de l'apprentissage

Au démarrage, le plugin ne connaît pas encore la plante.

La progression normale est :

1. si aucune identification de temps mort n'existe encore, le bootstrap au démarrage peut forcer une ou deux tentatives propres OFF->ON
2. le temps mort commence à émerger
3. `b` commence l'apprentissage des fenêtres OFF
4. `a` ne commence que plus tard, une fois que le temps mort est crédible et `b` est stable

Les observations initiales typiques sont :

- `heating_rate_per_hour` toujours non défini
- `heating_rate_converged = false`
- `cooling_rate_converged = false`
- les gains toujours proches des valeurs par défaut
- `startup_sequence_active = true` pendant la séquence forcée initiale

La boucle d'exécution est :

1. le contrôleur calcule le `on_percent` demandé pour le prochain cycle
2. l'ordonnanceur VT engage un cycle réel avec la puissance appliquée à ce cycle
3. le plugin enregistre le contexte du cycle
4. à la fin du cycle, le plugin valide le cycle pour l'apprentissage
5. le modèle de temps mort est mis à jour
6. les fenêtres d'apprentissage courtes sont reconstruites à partir de l'historique des cycles
7. `b` peut apprendre des fenêtres OFF
8. `a` peut apprendre des fenêtres ON, une fois que le temps mort et `b` sont prêts
9. `gain_indoor` et `gain_outdoor` sont projetés de manière conservatrice

## Bootstrap au démarrage

Quand le temps mort est encore inconnu, le démarrage peut temporairement remplacer la commande nominale :

- si déjà au-dessus du point de consigne, rester OFF jusqu'à `target - 0.3°C`
- si au-dessous du point de consigne, d'abord chauffer au point de consigne, puis refroidir à `target - 0.3°C`
- à partir de `target - 0.3°C`, chauffer à `100%` jusqu'au point de consigne
- chaque franchissement de seuil du bootstrap force un redémarrage immédiat du cycle afin que l'ordonnanceur n'attende pas la limite de cycle précédente
- si aucune identification de temps mort n'est produite, réessayer une fois, puis revenir à la régulation normale
- le refroidissement OFF forcé peut également alimenter le chemin d'apprentissage initial de `b` même quand il démarre très proche du point de consigne

## Diagnostiques

Le plugin expose les diagnostiques d'apprentissage dans les `specific_states` du climat.

Les champs les plus utiles à inspecter en premier sont :

- `adaptive_phase`
- `current_cycle_percent`
- `next_cycle_percent`
- `startup_sequence_active`
- `startup_sequence_stage`
- `startup_sequence_attempt`
- `startup_sequence_completion_reason`
- `deadtime_cycles`
- `deadtime_confidence`
- `cooling_rate_per_hour`
- `cooling_rate_confidence`
- `cooling_samples`
- `sample_window_size`
- `heating_rate_per_hour`
- `heating_rate_confidence`
- `heating_rate_converged`
- `heating_samples`
- `last_learning_result`
- `last_learning_family`
- `last_runtime_blocker`

Un apprentissage sain ressemble souvent à ceci :

- `deadtime_cycles` commence à bouger avant d'être considéré comme fiable
- `cooling_rate_per_hour` apparaît avant `heating_rate_per_hour`
- `cooling_samples / sample_window_size` se remplit progressivement jusqu'à saturation de la fenêtre glissante
- `last_runtime_blocker` reste souvent lié au temps mort ou à la convergence du refroidissement pendant un certain temps
- `gain_indoor` et `gain_outdoor` restent proches des valeurs par défaut jusqu'à ce que la confiance soit suffisante

## Documentation principale

Si vous voulez approfondir :

- [Diagnostiques](docs/diagnostics-fr.md)
  Diagnostiques d'exécution destinés à l'utilisateur et comment les interpréter
- [Architecture](docs/architecture-fr.md)
  Architecture interne et flux d'apprentissage

## Disposition du référentiel

- [custom_components/vtherm_adaptive_tpi](vtherm_adaptive_tpi/custom_components/vtherm_adaptive_tpi)
  Code d'intégration Home Assistant et algorithme adaptif
- [docs](vtherm_adaptive_tpi/docs)
  Documentation du projet
- [tests](vtherm_adaptive_tpi/tests)
  Tests comportementaux pour l'intégration
- [plans](vtherm_adaptive_tpi/plans)
  Notes de conception, spécifications mathématiques, plans d'implémentation et rapports de révision

## Notes de développement

Ce plugin dépend de :

- `versatile_thermostat`
- `vtherm_api`

Le développement doit être effectué avec des versions compatibles des deux côtés.
