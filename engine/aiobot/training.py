"""Apprentissage du world model puis étalonnage du filtre χ² en boucle fermée.

1. Collecte de transitions par exploration aléatoire lissée (charge nominale).
2. Apprentissage de l'ensemble (NumPy, Adam), validation sur 15 % des données.
3. Étalonnage : missions nominales pilotées par AIOBot (filtre χ² désactivé),
   collecte des résidus mesure − prédiction, covariance et seuils empiriques.
   Le bruit des capteurs est donc inclus dans la référence.

Tout est déterministe pour une graine donnée : ``python -m aiobot train``
reproduit le fichier de poids versionné.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .controller import ControllerConfig
from .integrity import calibrate
from .plant import exploration_dataset
from .world_model import WorldModel, train_world_model

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "world_model.npz"
TRAINING_REPORT = Path(__file__).resolve().parent.parent / "reports" / "training.json"


@dataclass(frozen=True)
class TrainConfig:
    seed: int = 7
    n_transitions: int = 80_000
    n_members: int = 3
    hidden: int = 96
    epochs: int = 40
    calibration_seeds: tuple[int, ...] = (1001, 1002, 1003, 1004)


def build_world_model(config: TrainConfig | None = None, log=print) -> tuple[WorldModel, dict[str, Any]]:
    from .episode import run_mission  # import local : episode dépend du contrôleur

    cfg = config or TrainConfig()
    rng = np.random.default_rng(cfg.seed)
    t0 = time.perf_counter()
    states, actions, nexts = exploration_dataset(cfg.n_transitions, rng)
    log(f"données : {states.shape[0]} transitions ({time.perf_counter() - t0:.1f} s)")
    t1 = time.perf_counter()
    model, report = train_world_model(states, actions, nexts, rng, cfg.n_members, cfg.hidden, cfg.epochs)
    log(f"apprentissage : R² validation {[round(r, 4) for r in report.val_r2]} ({time.perf_counter() - t1:.1f} s)")

    model.calibration = {}
    residuals = []
    for seed in cfg.calibration_seeds:
        controller_residuals: list[np.ndarray] = []
        run_mission(model, "nominal", ControllerConfig(use_integrity=False), seed=seed, residual_sink=controller_residuals)
        residuals.extend(controller_residuals)
    cal = calibrate(np.array(residuals))
    model.calibration = cal
    log(f"étalonnage χ² : {int(cal['n'])} pas, seuil {float(cal['threshold']):.2f}, gel {float(cal['threshold_freeze']):.2f}")
    summary = {
        "seed": cfg.seed,
        "transitions": int(states.shape[0]),
        "members": cfg.n_members,
        "hidden": cfg.hidden,
        "parameters_per_member": int(sum(w.size + b.size for w, b in zip(model.members[0].weights, model.members[0].biases))),
        "flops_per_prediction": model.flops_per_sample,
        "training": report.to_dict(),
        "chi2_calibration": {
            "steps": int(cal["n"]),
            "threshold": round(float(cal["threshold"]), 3),
            "threshold_freeze": round(float(cal["threshold_freeze"]), 3),
        },
        "duration_s": round(time.perf_counter() - t0, 1),
    }
    return model, summary
