"""Gate de complexité : un budget de calcul proportionnel à la difficulté de l'instant.

Contrairement à un masque appliqué après coup, ce gate décide AVANT la
planification combien de trajectoires imaginer (N), sur quel horizon (H) et
en combien de tours de raffinement. Le calcul non demandé n'est jamais fait :
l'économie se lit directement dans le nombre de prédictions du world model,
compté pas à pas et publié dans le registre.

Score de complexité c ∈ [0 ; 1], le plus élevé des signaux suivants :
- proximité : effecteur à moins de 0,4 m d'une sphère active (obstacle, opérateur) ;
- opérateur présent dans la cellule (plancher à 0,5) ;
- intégrité : rapport d²/seuil du filtre χ² au dernier pas ;
- robustesse : marge du dernier plan retenu sous 100 % ;
- reprise : le pas précédent était un arrêt sûr.

Budget : N = 64 → 256 trajectoires, H = 10 → 12 pas, 2 tours de raffinement.

Réglage mesuré (voir le benchmark) : un horizon plus long que 12 pas dégrade
la planification, car l'erreur du world model grandit avec l'horizon ; le
budget maximal est donc 256 × 12 × 2, et le gate joue surtout sur N.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .plant import ArmConfig, forward_kinematics
from .scg import CellState
from .tap import FULL_BUDGET, PlanBudget


@dataclass(frozen=True)
class GateConfig:
    min_paths: int = 64
    max_paths: int = FULL_BUDGET.n_paths
    min_horizon: int = 10
    max_horizon: int = FULL_BUDGET.horizon
    refine_above: float = 0.0  # le raffinement (2 tours) reste toujours actif : c'est lui qui rend un petit faisceau efficace
    proximity_range: float = 0.4  # m
    operator_floor: float = 0.5


@dataclass
class GateDecision:
    complexity: float
    budget: PlanBudget
    signals: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "complexity": round(self.complexity, 3),
            "budget": self.budget.to_dict(),
            "signals": {k: round(v, 3) for k, v in self.signals.items()},
        }


class ComplexityGate:
    def __init__(self, config: GateConfig | None = None, arm: ArmConfig | None = None) -> None:
        self.config = config or GateConfig()
        self.arm = arm or ArmConfig()

    def decide(
        self, state: np.ndarray, cell: CellState, chi2_ratio: float, last_robustness: float, resumed: bool
    ) -> GateDecision:
        cfg = self.config
        _, eff = forward_kinematics(state[:2], self.arm)
        spheres = cell.obstacles + ([cell.operator] if cell.operator else [])
        clearance = min((float(s.distance(eff)) for s in spheres), default=np.inf)
        signals = {
            "proximity": float(np.clip(1.0 - clearance / cfg.proximity_range, 0.0, 1.0)),
            "operator": cfg.operator_floor if cell.operator is not None else 0.0,
            "integrity": float(np.clip(chi2_ratio, 0.0, 1.0)),
            "robustness": float(np.clip((1.0 - last_robustness) / 0.25, 0.0, 1.0)),
            "resume": 1.0 if resumed else 0.0,
        }
        c = max(signals.values())
        budget = PlanBudget(
            n_paths=int(round(cfg.min_paths + c * (cfg.max_paths - cfg.min_paths))),
            horizon=int(round(cfg.min_horizon + c * (cfg.max_horizon - cfg.min_horizon))),
            iterations=2 if c >= cfg.refine_above else 1,
        )
        return GateDecision(c, budget, signals)
