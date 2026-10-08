# AIOBot · Pilote industriel fondé sur un world model appris

> La productivité est une conséquence. La trajectoire admissible sous contraintes physiques et de sécurité est l'objectif.

AIOBot transpose l'architecture d'AIOTrade (filtre χ², TAP, SCG, registre XAI) de la finance à l'automatisation
industrielle. Au lieu d'imaginer des trajectoires de prix, il imagine des trajectoires physiques dans un
**world model** appris, élimine celles qui violent une contrainte de sécurité, et n'agit que si le plan retenu
reste sûr face à l'incertitude du modèle. Chaque décision est scellée dans un registre d'audit chaîné.

Ce dépôt contient le moteur (Python, NumPy pur), son API (FastAPI), les tests et la conteneurisation. Tout se
passe en **simulation** : aucun robot réel n'est commandé.

## Qu'est-ce qu'un world model ?

Un simulateur appris. On lui donne l'état d'une machine (angles, vitesses) et une action (couples moteur), il
prédit l'état suivant. On l'entraîne sur des transitions (état, action, état suivant), comme celles des journaux
d'un robot. Une fois appris, il permet d'**imaginer avant d'agir** : tester des centaines de gestes en quelques
millisecondes, sans risque pour la machine ni pour les personnes, puis n'exécuter que le meilleur. On recommence à
chaque pas de commande : c'est la commande prédictive (MPC), dans sa version « model-based ».

## Pipeline

```text
mesure capteurs (50 ms)
   │
   ▼
filtre χ²     le monde suit-il le world model ?  d² = rᵀ Σ⁻¹ r  ── FREEZE ──▶ arrêt sûr
   │                                              3 gels dans la mission ──▶ intervention humaine
   ▼
gate          complexité de l'instant → budget : N = 64…256 trajectoires, H = 10…12 pas
   │
   ▼
TAP           N suites d'actions imaginées dans le world model (MPPI + entropie croisée, 2 tours)
   │
   ▼
SCG           élagage : limites articulaires, plan de travail, sphères d'obstacles,
   │          sphère de l'opérateur, vitesse réduite en zone collaborative
   ▼
robustesse    meilleur plan rejoué par les 3 membres de l'ensemble, avec bruit :
   │          ≥ 75 % de déroulés admissibles et désaccord < 4 cm, sinon plan suivant, sinon arrêt sûr
   ▼
EXECUTE (première action) ou HOLD (arrêt sûr de catégorie 1)  ──▶  XAI Ledger (SHA-256 chaîné)
```

| Brique | Fichier | Ce qu'elle fait |
|---|---|---|
| Cellule simulée | `engine/aiobot/plant.py` | Bras 2 axes dans un plan vertical (dynamique complète M(q)q̈ + C + G + Bq̇ = τ), charge utile, freinage sûr |
| World model | `engine/aiobot/world_model.py` | Ensemble de 3 réseaux (2 × 96 neurones, tanh) qui prédit Δs ; apprentissage NumPy + Adam ; FLOP comptés exactement |
| Apprentissage | `engine/aiobot/training.py` | 80 000 transitions d'exploration, validation sur 15 %, étalonnage du χ² en boucle fermée |
| Filtre χ² | `engine/aiobot/integrity.py` | Résidu mesure − prédiction, seuil max(théorique, empirique), gel, intervention |
| Gate | `engine/aiobot/gate.py` | Budget décidé **avant** la planification : le calcul non demandé n'est jamais fait |
| TAP | `engine/aiobot/tap.py` | Faisceau de trajectoires imaginées, coût vers une posture cible, correction de biais en ligne |
| SCG | `engine/aiobot/scg.py` | Graphe de contraintes ; volumes interdits sphériques ; la présence d'un opérateur abaisse la vitesse autorisée |
| Pilote | `engine/aiobot/controller.py` | Enchaîne les briques, contrôle de robustesse, décision EXECUTE / HOLD |
| Registre XAI | `engine/aiobot/ledger.py` | Un enregistrement JSON par décision, chaîné SHA-256, vérifiable |
| Missions | `engine/aiobot/episode.py` | Cycle prise → passage → dépose → parking, 5 scénarios, vérité terrain |
| Benchmark | `engine/aiobot/benchmark.py` | 4 bras d'ablation × 5 scénarios × 5 graines |

## Résultats mesurés

Benchmark complet (`engine/reports/benchmark.json`) : 100 missions, 4 bras × 5 scénarios × 5 graines. Les
violations sont comptées sur l'état **réel** du simulateur, pas sur les prédictions.

| Bras | Missions réussies | Pas en violation | Calcul par mission | Calcul par décision | Latence | Énergie mécanique |
|---|---|---|---|---|---|---|
| **AIOBot complet** | 80 % | **0** | **9,7 GFLOP** | **61 MFLOP** | 12 ms | 57,6 J |
| Sans gate (budget maximal) | 80 % | 2 | 19,0 GFLOP | 124 MFLOP | 22 ms | 64,7 J |
| Sans SCG | 52 % | 788 | 5,8 GFLOP | 56 MFLOP | 11 ms | 65,4 J |
| Sans filtre χ² | 80 % | 79 | 11,1 GFLOP | 50 MFLOP | 13 ms | 89,4 J |

Les 20 % de missions non terminées par AIOBot sont toutes celles du scénario « charge » : le robot s'y arrête
volontairement et demande une intervention, ce qui est le comportement attendu.

Ce que montrent les chiffres :
- **Le gate divise le calcul par deux** (−49 % par mission, −51 % par décision) sans perdre de mission, avec un
  temps de cycle équivalent (8,5 s contre 8,6 s) et 11 % d'énergie mécanique en moins.
- **Le SCG supprime les violations** : sans lui, 788 pas en violation (vitesse en zone collaborative, limites
  articulaires, collisions avec le montage), dans les 25 missions.
- **Le filtre χ² détecte la charge inconnue** : avec une pièce de 1 kg plus lourde que tout ce que le modèle a vu,
  AIOBot gèle trois fois puis s'arrête sans aucune violation. Sans le filtre, le bras continue : il touche le plan de
  travail pendant 79 pas (dans 2 missions sur 5) et dépense 171 J par mission sans en terminer aucune.
- **L'opérateur n'est jamais touché** : distance minimale de 11 cm entre l'outil et sa sphère de protection.
- Les chocs extérieurs imposent quelques pas en violation au moment même de l'impact (5 pour AIOBot) : ils sont
  comptés à part, car aucun pilote ne peut les empêcher.

World model (`engine/reports/training.json`) : R² de validation de 0,997 à 0,9998 selon la variable, 10 564
paramètres par membre, 21 180 FLOP par prédiction ; seuil χ² étalonné à 14,9 (théorique 13,3), gel à 44,5.

Limites à connaître :
- Tout est simulé : un bras 2 axes, un pas de 50 ms, un bruit capteur gaussien. Le passage à un robot réel
  demande de réapprendre le world model sur ses journaux et de valider le SCG avec ses normes de sécurité
  (ISO 10218, ISO/TS 15066). AIOBot n'est pas un automate de sécurité certifié.
- Le filtre χ² déclenche un gel injustifié environ une mission nominale sur cinq (0,2 gel par mission) : il
  privilégie la prudence.
- Les FLOP du world model sont exacts ; la cinématique, les contraintes et le coût sont estimés à 120 FLOP par pas
  imaginé. L'énergie mesurée est celle des moteurs, pas celle du processeur.
- Un horizon plus long que 12 pas dégrade la planification, parce que l'erreur du world model grandit avec
  l'horizon : c'est pourquoi le budget maximal est de 256 × 12 × 2.

## Démarrage

```bash
git clone https://github.com/albanfredon23/aiobot.git
cd aiobot
docker compose up --build
```

Puis ouvrir <http://127.0.0.1:8000/api/docs>. L'API n'est publiée que sur l'interface locale ; le futur site
sera le seul service exposé publiquement.

Sans Docker :

```bash
cd engine
pip install -r requirements-dev.txt
python -m pytest                              # 29 tests
python -m aiobot mission --scenario charge    # une mission et son bilan
python -m aiobot mission --ledger registre.jsonl && python -m aiobot verify registre.jsonl
python -m aiobot benchmark                    # 100 missions, environ 1 min sur 4 cœurs
python -m aiobot train                        # réapprend le world model (environ 45 s, déterministe)
```

## API

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/health` | État du moteur |
| GET | `/api/config` | Bras, enveloppe de sécurité, gate, filtre, postes, scénarios |
| GET | `/api/model` | Rapport d'apprentissage du world model |
| POST | `/api/mission` | Joue une mission (`scenario`, `arm`, `seed`) : bilan, images clés, extrait du registre |
| GET | `/api/benchmark` | Rapport du benchmark |
| POST | `/api/ledger/verify` | Vérifie la chaîne d'un registre XAI (JSON Lines) |

## Conteneur

| Directive | Mise en œuvre |
|---|---|
| Bytecode seul | `engine/Dockerfile` multi-étapes : `python -m compileall -b .` puis suppression des `.py` ; les tests de l'étape `test` tournent sur ce bytecode |
| Durcissement | Système de fichiers en lecture seule, `cap_drop: ALL`, `no-new-privileges`, utilisateur non root |
| Exposition | API publiée sur `127.0.0.1` uniquement |
| Secrets | Aucun n'est nécessaire ; `.gitignore` et `.dockerignore` excluent `.env`, clés, certificats, bases locales et registres |

Limite à connaître : le bytecode Python se décompile avec des outils publics. Il dissuade la lecture
occasionnelle mais ne protège pas totalement le code.
