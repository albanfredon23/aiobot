"""Missions de démonstration : un cycle de prise et dépose dans une cellule robotisée.

Le bras enchaîne trois postes (prise, dépose, parking) en contournant un
montage fixe par un point de passage, comme une trajectoire robot programmée
en atelier. Cinq scénarios perturbent la mission :

- ``nominal``   : rien d'anormal ;
- ``operateur`` : un opérateur entre dans la cellule près du poste de prise
  (sphère de protection et zone collaborative à vitesse réduite) ;
- ``charge``    : la pièce saisie pèse 1 kg de plus que tout ce que le world
  model a vu à l'apprentissage ;
- ``capteur``   : le codeur du coude renvoie un décalage de 0,3 rad pendant 6 pas ;
- ``choc``      : un choc extérieur impose une vitesse brutale aux deux axes.

La vérité terrain (violations réelles, énergie mécanique dépensée par les
moteurs) est mesurée sur le simulateur, jamais sur les prédictions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .controller import AIOBotController, ControllerConfig
from .ledger import XAILedger
from .plant import ArmConfig, ArmPlant, forward_kinematics, inverse_kinematics
from .scg import REJECTION_KEYS, CellState, SafetyEnvelope, Sphere, SphericalConstraintGraph
from .world_model import WorldModel

# Postes : position cartésienne et posture du coude (True : coude « haut », q2 < 0).
STATIONS = {
    "prise": ((0.72, 0.02), True),
    "passage": ((0.02, 0.80), True),  # point de passage au-dessus du montage (pas d'arrêt exigé)
    "depose": ((-0.62, 0.10), False),
    "parking": ((0.30, 0.72), False),
}
MISSION = ("prise", "passage", "depose", "parking")
VIA_POINTS = {"passage"}
FIXTURE = Sphere((0.05, 0.42), 0.10, "montage")
OPERATOR = Sphere((0.98, 0.42), 0.22, "opérateur")
SCENARIOS = ("nominal", "operateur", "charge", "capteur", "choc")
SCENARIO_LABELS = {
    "nominal": "Cycle nominal",
    "operateur": "Opérateur dans la cellule",
    "charge": "Charge inconnue (+1 kg)",
    "capteur": "Codeur du coude défaillant",
    "choc": "Choc extérieur",
}


@dataclass(frozen=True)
class Scenario:
    name: str = "nominal"
    operator_window: tuple[int, int] | None = None
    payload_change: tuple[int, float] | None = None  # (pas, nouvelle charge kg)
    sensor_fault: tuple[int, int, float] | None = None  # (début, fin, décalage rad sur q2)
    impulse: tuple[int, tuple[float, float]] | None = None  # (pas, Δq̇ rad/s)

    @classmethod
    def named(cls, name: str) -> "Scenario":
        presets = {
            "nominal": cls("nominal"),
            "operateur": cls("operateur", operator_window=(8, 150)),
            "charge": cls("charge", payload_change=(20, 1.15)),
            "capteur": cls("capteur", sensor_fault=(30, 36, 0.3)),
            "choc": cls("choc", impulse=(30, (2.5, -3.0))),
        }
        if name not in presets:
            raise ValueError(f"scénario inconnu : {name} (attendus : {', '.join(SCENARIOS)})")
        return presets[name]


@dataclass(frozen=True)
class MissionConfig:
    max_steps: int = 480  # 24 s à 50 ms
    tolerance: float = 0.04  # m
    via_tolerance: float = 0.10  # m
    settle_speed: float = 0.15  # m/s
    sensor_noise: tuple[float, float] = (5e-4, 5e-3)  # rad, rad/s
    nominal_payload: float = 0.15


@dataclass
class MissionResult:
    scenario: str
    arm: str
    completed: bool
    stations_reached: int
    steps: int
    duration_s: float
    holds: int
    hold_reasons: dict[str, int]
    freezes: int
    intervention: bool
    violation_steps: dict[str, int]
    min_operator_clearance: float | None
    event_violation_steps: dict[str, int]
    mechanical_energy_j: float
    flops_total: int
    flops_per_decision: float
    model_steps_total: int
    latency_ms_mean: float
    frames: list[dict[str, Any]] = field(default_factory=list)

    @property
    def violations(self) -> int:
        return int(sum(self.violation_steps.values()))

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "arm": self.arm,
            "completed": self.completed,
            "stations_reached": self.stations_reached,
            "steps": self.steps,
            "duration_s": round(self.duration_s, 2),
            "holds": self.holds,
            "hold_reasons": self.hold_reasons,
            "freezes": self.freezes,
            "intervention": self.intervention,
            "violation_steps": self.violation_steps,
            "violations": self.violations,
            "event_violation_steps": self.event_violation_steps,
            "min_operator_clearance_m": None if self.min_operator_clearance is None else round(self.min_operator_clearance, 4),
            "mechanical_energy_j": round(self.mechanical_energy_j, 3),
            "flops_total": self.flops_total,
            "flops_per_decision": round(self.flops_per_decision, 1),
            "model_steps_total": self.model_steps_total,
            "latency_ms_mean": round(self.latency_ms_mean, 3),
        }


OPERATOR_ENTRY_X = 1.45  # l'opérateur est détecté en bordure de cellule…
OPERATOR_WALK_STEPS = 12  # … et rejoint son poste en 0,6 s


def cell_at(step: int, scenario: Scenario) -> tuple[CellState, CellState]:
    """(cellule réelle, cellule vue par le pilote) à l'instant ``step``.

    Dès sa détection par le scanner de sécurité, la zone de l'opérateur est
    réservée à sa position de travail : le pilote planifie avec ce volume
    réservé, tandis que la vérité terrain suit l'opérateur pendant son approche.
    """
    if not (scenario.operator_window and scenario.operator_window[0] <= step < scenario.operator_window[1]):
        empty = CellState(obstacles=[FIXTURE])
        return empty, empty
    progress = min(1.0, (step - scenario.operator_window[0]) / OPERATOR_WALK_STEPS)
    x = OPERATOR_ENTRY_X + progress * (OPERATOR.center[0] - OPERATOR_ENTRY_X)
    actual = Sphere((x, OPERATOR.center[1]), OPERATOR.radius, OPERATOR.label)
    return CellState(obstacles=[FIXTURE], operator=actual), CellState(obstacles=[FIXTURE], operator=OPERATOR)


def run_mission(
    model: WorldModel,
    scenario: Scenario | str = "nominal",
    controller_config: ControllerConfig | None = None,
    arm_name: str = "aiobot",
    seed: int = 0,
    mission: MissionConfig | None = None,
    ledger: XAILedger | None = None,
    record_frames: bool = False,
    arm: ArmConfig | None = None,
    envelope: SafetyEnvelope | None = None,
    residual_sink: list | None = None,
) -> MissionResult:
    scenario = Scenario.named(scenario) if isinstance(scenario, str) else scenario
    mission = mission or MissionConfig()
    arm = arm or ArmConfig()
    rng = np.random.default_rng(seed)
    controller = AIOBotController(model, arm, envelope, controller_config, ledger=ledger)
    truth = SphericalConstraintGraph(arm, controller.envelope)
    plant = ArmPlant(cfg=arm, payload=mission.nominal_payload, state=np.array([np.pi / 2, -np.pi / 2, 0.0, 0.0]))

    station = 0
    holds, freezes_seen, flops, model_steps = 0, 0, 0, 0
    hold_reasons: dict[str, int] = {}
    violation_steps = dict.fromkeys(REJECTION_KEYS, 0)
    latencies: list[float] = []
    energy = 0.0
    min_op: float | None = None
    event_violations: dict[str, int] = {}
    frames: list[dict[str, Any]] = []
    noise = np.array([mission.sensor_noise[0]] * 2 + [mission.sensor_noise[1]] * 2)

    step = 0
    for step in range(mission.max_steps):
        if station >= len(MISSION):
            break
        cell, planning_cell = cell_at(step, scenario)
        xy, elbow_up = STATIONS[MISSION[station]]
        target = np.array(xy)
        target_q = inverse_kinematics(target, arm, elbow_up)
        if scenario.payload_change and step == scenario.payload_change[0]:
            plant.payload = scenario.payload_change[1]

        measured = plant.state + rng.normal(0.0, 1.0, 4) * noise
        if scenario.sensor_fault and scenario.sensor_fault[0] <= step < scenario.sensor_fault[1]:
            measured[1] += scenario.sensor_fault[2]

        decision = controller.step(measured, target_q, planning_cell, rng)
        controller.log(decision, target_q, planning_cell, measured)
        flops += decision.flops
        model_steps += decision.model_steps
        latencies.append(decision.latency_ms)
        before = plant.state.copy()
        impulse = None
        if scenario.impulse and step == scenario.impulse[0]:
            impulse = np.array(scenario.impulse[1])
        if decision.action == "EXECUTE":
            tau = np.clip(decision.command, -1, 1) * np.array(arm.tau_max)
            after = plant.step(decision.command, impulse)
            energy += float(np.sum(np.abs(tau * 0.5 * (before[2:] + after[2:])))) * arm.dt
        else:
            holds += 1
            hold_reasons[decision.reason] = hold_reasons.get(decision.reason, 0) + 1
            after = plant.brake()
            if impulse is not None:
                plant.state[2:] += impulse
                after = plant.state.copy()

        for key in truth.state_violations(after, cell):
            if impulse is not None:
                event_violations[key] = event_violations.get(key, 0) + 1  # imposée par le choc, pas par le pilote
            else:
                violation_steps[key] += 1
        _, eff = forward_kinematics(after[:2], arm)
        if cell.operator is not None:
            d = float(cell.operator.distance(eff))
            min_op = d if min_op is None else min(min_op, d)

        via = MISSION[station] in VIA_POINTS
        reached = np.linalg.norm(eff - target) < (mission.via_tolerance if via else mission.tolerance)
        settled = via or np.linalg.norm(after[2:]) * (arm.l1 + arm.l2) < mission.settle_speed
        if record_frames:
            frames.append(_frame(step, after, eff, target, MISSION[station], cell, decision, plant.payload, arm))
        if reached and settled:
            station += 1
        if controller.integrity.needs_intervention:
            break

    if residual_sink is not None:
        residual_sink.extend(controller.residuals)
    n = max(1, len(latencies))
    return MissionResult(
        scenario=scenario.name,
        arm=arm_name,
        completed=station >= len(MISSION),
        stations_reached=station,
        steps=len(latencies),
        duration_s=len(latencies) * arm.dt,
        holds=holds,
        hold_reasons=hold_reasons,
        freezes=controller.integrity.freezes,
        intervention=controller.integrity.needs_intervention,
        violation_steps=violation_steps,
        min_operator_clearance=min_op,
        event_violation_steps=event_violations,
        mechanical_energy_j=energy,
        flops_total=flops,
        flops_per_decision=flops / n,
        model_steps_total=model_steps,
        latency_ms_mean=float(np.mean(latencies)) if latencies else 0.0,
        frames=frames,
    )


def _r(values, nd: int = 4) -> list[float]:
    return [round(float(v), nd) for v in np.ravel(values)]


def _frame(step, state, eff, target, station, cell, decision, payload, arm) -> dict[str, Any]:
    elbow, _ = forward_kinematics(state[:2], arm)
    frame: dict[str, Any] = {
        "step": step,
        "q": _r(state[:2]),
        "elbow": _r(elbow),
        "effector": _r(eff),
        "target": _r(target),
        "station": station,
        "operator": cell.operator.to_dict() if cell.operator else None,
        "decision": decision.action,
        "reason": decision.reason,
        "payload_kg": round(payload, 3),
        "chi2": decision.integrity.to_dict() if decision.integrity else None,
        "budget": decision.gate.budget.to_dict() if decision.gate else None,
        "complexity": round(decision.gate.complexity, 3) if decision.gate else None,
        "scg": decision.scg,
        "robustness": None if decision.robustness is None else round(decision.robustness, 3),
        "flops": decision.flops,
    }
    if decision.plan is not None:
        _, path = forward_kinematics(decision.plan[:, :2], arm)
        frame["plan"] = [_r(p, 3) for p in path]
    if decision.pruned is not None and len(decision.pruned):
        _, pruned = forward_kinematics(decision.pruned[:6, :, :2], arm)
        frame["pruned"] = [[_r(p, 3) for p in path] for path in pruned]
    return frame
