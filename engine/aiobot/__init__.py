"""AIOBot : pilote industriel fondé sur un world model appris.

« La productivité est une conséquence, la trajectoire admissible sous
contraintes physiques et de sécurité est l'objectif. »

Pipeline : mesure capteurs → filtre d'intégrité χ² (le monde suit-il le
world model ?) → gate de complexité (budget de calcul) → TAP (trajectoires
imaginées dans le world model) → SCG (contraintes physiques et sphères de
sécurité) → contrôle de robustesse (ensemble + bruit, ≥ 75 %) → EXECUTE ou
arrêt sûr → XAI Ledger.
"""
from __future__ import annotations

__version__ = "0.1.0"
