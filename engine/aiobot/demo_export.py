"""Export des missions de démonstration pour le site (rejeu de vraies décisions du moteur).

Chaque fichier contient les images clés compactes d'une mission : angles du bras,
cible, décision, filtre χ², budget du gate, rejets du SCG, robustesse, plan retenu
et quelques trajectoires élaguées. Le site les rejoue tels quels : rien n'y est
inventé ni retouché.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__
from .benchmark import ARM_LABELS, ARMS, REPORT_PATH
from .episode import FIXTURE, MISSION, OPERATOR, SCENARIO_LABELS, SCENARIOS, STATIONS, run_mission
from .ledger import XAILedger
from .plant import ArmConfig
from .scg import REJECTION_KEYS, SafetyEnvelope
from .training import TRAINING_REPORT
from .world_model import WorldModel

# Missions exportées : AIOBot sur chaque scénario, et les bras d'ablation là où ils sont parlants.
DEMO_MISSIONS = [(s, "aiobot") for s in SCENARIOS] + [
    ("nominal", "budget_fixe"),
    ("operateur", "sans_scg"),
    ("charge", "sans_chi2"),
]


def _compact(frame: dict[str, Any]) -> dict[str, Any]:
    scg = frame.get("scg") or {}
    out: dict[str, Any] = {
        "q": frame["q"],
        "t": frame["target"],
        "st": frame["station"],
        "d": frame["decision"][0],  # E | H
        "r": frame["reason"],
        "op": frame["operator"]["center"] if frame["operator"] else None,
        "kg": frame["payload_kg"],
        "fl": frame["flops"],
    }
    if frame.get("chi2"):
        out["c2"] = [frame["chi2"]["d2"], frame["chi2"]["status"][0]]  # N | A | F | I
    if frame.get("budget"):
        out["b"] = [frame["budget"]["n_paths"], frame["budget"]["horizon"]]
        out["cx"] = frame["complexity"]
    if scg:
        out["sc"] = [scg["admissible"], scg["n_trajectories"]] + [scg["rejections"][k] for k in REJECTION_KEYS]
    if frame.get("robustness") is not None:
        out["rb"] = frame["robustness"]
    if frame.get("plan"):
        out["p"] = [[round(x, 3), round(y, 3)] for x, y in frame["plan"]]
    if frame.get("pruned"):
        out["x"] = [[[round(x, 3), round(y, 3)] for x, y in path] for path in frame["pruned"][:3]]
    return out


def export_demo(model: WorldModel, out_dir: str | Path, seed: int = 0) -> list[Path]:
    out = Path(out_dir)
    (out / "missions").mkdir(parents=True, exist_ok=True)
    arm, env = ArmConfig(), SafetyEnvelope()
    written: list[Path] = []
    index = {
        "version": __version__,
        "seed": seed,
        "dt": arm.dt,
        "arm": {"l1": arm.l1, "l2": arm.l2, "q_low": list(arm.q_low), "q_high": list(arm.q_high)},
        "envelope": {"floor_y": env.floor_y, "collaborative_margin": env.collaborative_margin,
                     "collaborative_speed": env.collaborative_speed, "max_tool_speed": env.max_tool_speed},
        "fixture": FIXTURE.to_dict(),
        "operator": OPERATOR.to_dict(),
        "stations": {k: list(xy) for k, (xy, _) in STATIONS.items()},
        "mission": list(MISSION),
        "rejection_keys": list(REJECTION_KEYS),
        "scenarios": SCENARIO_LABELS,
        "arms": ARM_LABELS,
        "missions": [],
    }
    for scenario, arm_name in DEMO_MISSIONS:
        ledger = XAILedger()
        result = run_mission(model, scenario, ARMS[arm_name], arm_name=arm_name, seed=seed, ledger=ledger, record_frames=True)
        records = ledger.records()
        name = f"{scenario}-{arm_name}"
        holds = [r for r in records if r["decision"] == "HOLD"]
        data = {
            "scenario": scenario,
            "arm": arm_name,
            "summary": result.summary(),
            "frames": [_compact(f) for f in result.frames],
            "ledger": {"records": len(ledger), "head_hash": ledger.head_hash, "verified": ledger.verify().ok,
                       "sample": (holds[:1] or records[-1:])},
        }
        path = out / "missions" / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        written.append(path)
        index["missions"].append({"id": name, "scenario": scenario, "arm": arm_name, "summary": result.summary()})
    for src, dst in ((REPORT_PATH, "benchmark.json"), (TRAINING_REPORT, "training.json")):
        if src.exists():
            (out / dst).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            written.append(out / dst)
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    written.append(out / "index.json")
    return written
