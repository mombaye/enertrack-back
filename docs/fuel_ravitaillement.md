# Plan de ravitaillement — module Carburant

Écran Suivi Carburant → Ravitaillement : quels sites livrer, avant quelle date, et combien.

| Méthode | Adresse | Rôle |
| --- | --- | --- |
| GET | `/api/fuel-tracking/bi/ravitaillement/` | plan, KPI avec explication, répartition par zone et par jour, sites hors plan |
| GET | `/api/fuel-tracking/bi/export/ravitaillement/` | classeur Excel : Synthèse, Plan, Par zone, Hors plan |

Paramètres : `zone`, `month` (mois de référence de la conso), `horizon` (1–30, défaut 7), `securite` (0–10, défaut 2),
`cible` (50–100 %, défaut 90). Valeur hors limites → 400.

## Règles (valeurs par défaut à valider avec le métier)

Mêmes données que Suivi Stock (`views_bi.stock_sites`) : stock capteur au dernier relevé, capacité de cuve, conso
mesurée moyenne par jour du mois de référence.

- stock estimé aujourd'hui = stock relevé − conso/jour × jours depuis le relevé (jamais sous 0) — c'est une estimation,
  affichée à côté du stock relevé et de sa date ;
- jours avant rupture = stock estimé ÷ conso/jour ;
- à livrer au plus tard = aujourd'hui + (jours avant rupture − marge), jamais avant aujourd'hui ;
- le site entre dans le plan si cette date est dans l'horizon ; « urgent » = aujourd'hui ou demain ;
- quantité = capacité × cible − stock estimé le jour de la livraison, arrondie aux 50 L supérieurs ;
  capacité inconnue → quantité `null` avec un motif, jamais inventée ;
- relevé de plus de 7 jours ou sans date → signalé « à confirmer ».

Hors plan, comptés et listés avec leur motif : sites sans stock capteur, sites sans conso mesurée sur le mois de référence.

Différent de l'estimation de commande (`FuelCommandeEstimationView`) : celle-ci estime le volume du mois suivant ;
le plan de ravitaillement est opérationnel, site par site, sur quelques jours.
