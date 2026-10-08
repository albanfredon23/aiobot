"""Benchmark à 4 bras : chaque brique d'AIOBot est retirée tour à tour.

Bras :
- ``aiobot``      : pipeline complet (χ², gate, TAP, SCG, robustesse) ;
- ``budget_fixe`` : identique, mais budget maximal à chaque pas (sans gate) ;
- ``sans_scg``    : le planificateur ignore les contraintes (ni SCG, ni robustesse) ;
- ``sans_chi2``   : pas de filtre d'intégrité.

Les cinq scénarios d'``episode`` sont joués avec plusieurs graines. Toutes les
mesures viennent du simulateur (vérité terrain) ou du comptage exact des
prédictions du world model ; rien n'est estimé à la main.
"""
from __future__ import annotations

import json
import os
import platform
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__
from .controller import ControllerConfig
from .episode import SCENARIO_LABELS, SCENARIOS, run_mission
from .world_model import WorldModel

REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "benchmark.json"

ARMS = {
    "aiobot": ControllerConfig(),
    "budget_fixe": ControllerConfig(use_gate=False),
    "sans_scg": ControllerConfig(use_scg=False),
    "sans_chi2": ControllerConfig(use_integrity=False),
}
ARM_LABELS = {
    "aiobot": "AIOBot complet",
    "budget_fixe": "Sans gate (budget maximal)",
    "sans_scg": "Sans SCG",
    "sans_chi2": "Sans filtre χ²",
}


@dataclass(frozen=True)
class BenchmarkConfig:
    seeds: int = 5
    scenarios: tuple[str, ...] = SCENARIOS
    arms: tuple[str, ...] = tuple(ARMS)
    workers: int = 0  # 0 : nombre de cœurs


_MODEL: WorldModel | None = None


def _init(model_path: str) -> None:
    global _MODEL
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    _MODEL = WorldModel.load(model_path)


def _run(task: tuple[str, str, int]) -> dict[str, Any]:
    arm, scenario, seed = task
    assert _MODEL is not None
    return run_mission(_MODEL, scenario, ARMS[arm], arm_name=arm, seed=seed).summary()


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    done = [r for r in rows if r["completed"]]
    viol: dict[str, int] = {}
    for r in rows:
        for k, v in r["violation_steps"].items():
            viol[k] = viol.get(k, 0) + v
    clearances = [r["min_operator_clearance_m"] for r in rows if r["min_operator_clearance_m"] is not None]
    return {
        "missions": len(rows),
        "completion_rate": round(len(done) / len(rows), 3),
        "cycle_time_s": round(float(np.mean([r["duration_s"] for r in done])), 2) if done else None,
        "violation_steps": sum(viol.values()),
        "violation_steps_by_constraint": viol,
        "missions_with_violation": sum(1 for r in rows if r["violations"] > 0),
        "event_violation_steps": sum(sum(r["event_violation_steps"].values()) for r in rows),
        "intervention_rate": round(sum(r["intervention"] for r in rows) / len(rows), 3),
        "freezes_per_mission": round(float(np.mean([r["freezes"] for r in rows])), 2),
        "holds_per_mission": round(float(np.mean([r["holds"] for r in rows])), 1),
        "gflop_per_mission": round(float(np.mean([r["flops_total"] for r in rows])) / 1e9, 2),
        "mflop_per_decision": round(float(np.mean([r["flops_per_decision"] for r in rows])) / 1e6, 2),
        "latency_ms": round(float(np.mean([r["latency_ms_mean"] for r in rows])), 2),
        "mechanical_energy_j": round(float(np.mean([r["mechanical_energy_j"] for r in rows])), 1),
        "min_operator_clearance_m": round(min(clearances), 4) if clearances else None,
    }


def run_benchmark(model_path: str | Path, config: BenchmarkConfig | None = None, log=print) -> dict[str, Any]:
    cfg = config or BenchmarkConfig()
    tasks = [(a, s, seed) for a in cfg.arms for s in cfg.scenarios for seed in range(cfg.seeds)]
    workers = cfg.workers or os.cpu_count() or 1
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(str(model_path),)) as pool:
        rows = list(pool.map(_run, tasks))
    log(f"{len(rows)} missions en {time.perf_counter() - t0:.0f} s ({workers} processus)")

    by_arm_scenario: dict[str, dict[str, Any]] = {}
    for arm in cfg.arms:
        by_arm_scenario[arm] = {
            sc: _aggregate([r for r in rows if r["arm"] == arm and r["scenario"] == sc]) for sc in cfg.scenarios
        }
        by_arm_scenario[arm]["_total"] = _aggregate([r for r in rows if r["arm"] == arm])

    def total(arm: str, key: str) -> float:
        return by_arm_scenario[arm]["_total"][key]

    headline: dict[str, Any] = {}
    if {"aiobot", "budget_fixe"} <= set(cfg.arms):
        headline["compute_saving_per_mission"] = round(1 - total("aiobot", "gflop_per_mission") / total("budget_fixe", "gflop_per_mission"), 3)
        headline["compute_saving_per_decision"] = round(1 - total("aiobot", "mflop_per_decision") / total("budget_fixe", "mflop_per_decision"), 3)
        headline["completion_aiobot"] = total("aiobot", "completion_rate")
        headline["completion_budget_fixe"] = total("budget_fixe", "completion_rate")
    for arm in cfg.arms:
        headline[f"violation_steps_{arm}"] = total(arm, "violation_steps")

    return {
        "version": __version__,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"python": platform.python_version(), "processor": platform.machine(), "cpu_count": os.cpu_count()},
        "protocol": {
            "seeds": cfg.seeds,
            "scenarios": {s: SCENARIO_LABELS[s] for s in cfg.scenarios},
            "arms": {a: ARM_LABELS[a] for a in cfg.arms},
            "notes": [
                "Simulation : bras 2 axes, pas de commande de 50 ms, bruit capteur gaussien.",
                "Violations : pas où l'état réel du simulateur viole une contrainte ; celles imposées par un choc au pas même du choc sont comptées à part.",
                "FLOP : prédictions du world model comptées exactement ; cinématique, contraintes et coût estimés à 120 FLOP par pas imaginé.",
                "Énergie mécanique : somme de |τ · q̇| · dt des moteurs, mesurée sur le simulateur (pas l'énergie électrique du calcul).",
            ],
        },
        "headline": headline,
        "results": by_arm_scenario,
    }


def save_report(report: dict[str, Any], path: str | Path = REPORT_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
