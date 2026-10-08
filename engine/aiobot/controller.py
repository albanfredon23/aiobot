"""Le pilote AIOBot : une décision sûre et traçable à chaque pas de commande.

    mesure capteurs
        │
        ▼
    filtre χ² (le monde suit-il le world model ?) ── FREEZE ──▶ arrêt sûr
        │
        ▼
    gate de complexité → budget (N trajectoires, horizon H, tours)
        │
        ▼
    TAP : N suites d'actions imaginées dans le world model
        │
        ▼
    SCG : élagage des trajectoires qui violent une contrainte
        │
        ▼
    robustesse : le meilleur plan rejoué par chaque membre de l'ensemble,
    avec bruit ; il faut ≥ 75 % de déroulés admissibles et un désaccord
    des membres sous 4 cm, sinon plan suivant (3 au plus), sinon arrêt sûr
        │
        ▼
    EXECUTE (première action du plan) ou HOLD (arrêt sûr) → XAI Ledger

« La productivité est une conséquence, la trajectoire admissible sous
contraintes physiques et de sécurité est l'objectif. »
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .gate import ComplexityGate, GateConfig, GateDecision
from .integrity import IntegrityConfig, IntegrityFilter, IntegrityReading, IntegrityStatus
from .ledger import XAILedger
from .plant import ArmConfig, forward_kinematics
from .scg import CellState, SafetyEnvelope, SphericalConstraintGraph
from .tap import FULL_BUDGET, STEP_OVERHEAD_FLOPS, PlanBudget, TAPPlanner, rollout
from .world_model import WorldModel


@dataclass(frozen=True)
class ControllerConfig:
    use_gate: bool = True  # False : budget maximal à chaque pas
    use_scg: bool = True  # False : planification sans contraintes (bras d'ablation)
    use_integrity: bool = True  # False : pas de filtre χ² (bras d'ablation)
    robustness_samples: int = 8  # déroulés bruités par membre de l'ensemble
    robustness_horizon: int = 8  # pas vérifiés (0,4 s) : au-delà de l'arrêt sûr, le plan est rejoué à chaque pas
    max_candidates: int = 3
    fixed_budget: PlanBudget = FULL_BUDGET


@dataclass
class Decision:
    step: int
    action: str  # EXECUTE | HOLD
    reason: str
    command: np.ndarray | None
    integrity: IntegrityReading | None
    gate: GateDecision | None
    scg: dict[str, Any] | None
    robustness: float | None
    disagreement: float | None
    plan: np.ndarray | None  # trajectoire retenue (H + 1, 4)
    pruned: np.ndarray | None  # aperçu de trajectoires rejetées
    model_steps: int
    flops: int
    latency_ms: float
    extra: dict[str, Any] = field(default_factory=dict)

    def record(self, target: np.ndarray, cell: CellState, state: np.ndarray, envelope: SafetyEnvelope) -> dict[str, Any]:
        """Enregistrement du registre XAI (JSON pur, valeurs arrondies)."""
        return {
            "step": self.step,
            "decision": self.action,
            "reason": self.reason,
            "command": None if self.command is None else [round(float(v), 4) for v in self.command],
            "state_measured": [round(float(v), 4) for v in state],
            "target_q": [round(float(v), 4) for v in target],
            "cell": cell.to_dict(),
            "integrity": self.integrity.to_dict() if self.integrity else None,
            "gate": self.gate.to_dict() if self.gate else None,
            "scg": self.scg,
            "robustness": None if self.robustness is None else round(self.robustness, 4),
            "disagreement_m": None if self.disagreement is None else round(self.disagreement, 4),
            "model_steps": self.model_steps,
            "flops": self.flops,
            "constraints": envelope.labels(),
        }


class AIOBotController:
    def __init__(
        self,
        model: WorldModel,
        arm: ArmConfig | None = None,
        envelope: SafetyEnvelope | None = None,
        config: ControllerConfig | None = None,
        gate_config: GateConfig | None = None,
        integrity_config: IntegrityConfig | None = None,
        ledger: XAILedger | None = None,
    ) -> None:
        self.model = model
        self.arm = arm or ArmConfig()
        self.envelope = envelope or SafetyEnvelope()
        self.config = config or ControllerConfig()
        self.scg = SphericalConstraintGraph(self.arm, self.envelope)
        # Bras « sans SCG » : le planificateur ne voit ni sphères ni limites de vitesse.
        self._scg_planning = self.scg if self.config.use_scg else SphericalConstraintGraph(
            self.arm, SafetyEnvelope(floor_y=-10.0, max_joint_speed=1e9, max_tool_speed=1e9, collaborative_speed=1e9)
        )
        self.planner = TAPPlanner(model, self._scg_planning)
        self.gate = ComplexityGate(gate_config, self.arm)
        cal = model.calibration
        self.integrity = IntegrityFilter(
            cal.get("cov", model.residual_cov),
            float(cal["threshold"]) if "threshold" in cal else None,
            float(cal["threshold_freeze"]) if "threshold_freeze" in cal else None,
            integrity_config,
        )
        self.ledger = ledger
        self.reset()

    def reset(self) -> None:
        self.planner.reset()
        self.integrity.reset()
        self._step = 0
        self._predicted: np.ndarray | None = None
        self._last_d2_ratio = 0.0
        self._last_robustness = 1.0
        self._held = False
        self.residuals: list[np.ndarray] = []  # résidus observés (étalonnage du filtre χ²)
        self._bias = np.zeros(4)  # biais du world model estimé en ligne (planification seulement)
        self.planner.bias = self._bias

    def hold_action(self, state: np.ndarray, payload_guess: float = 0.15) -> np.ndarray:
        """Compensation de gravité estimée pour la charge nominale (point de départ des plans)."""
        from .plant import ArmPlant

        return ArmPlant(cfg=self.arm, payload=payload_guess).gravity_action(state)

    def _view(self, cell: CellState) -> CellState:
        return cell if self.config.use_scg else CellState()

    def robustness(
        self, state: np.ndarray, plan: np.ndarray, cell: CellState, rng: np.random.Generator
    ) -> tuple[float, float, int]:
        """Part des déroulés bruités (tous membres) admissibles, désaccord des membres (m), prédictions."""
        plan = plan[: self.config.robustness_horizon]
        k, members, h = self.config.robustness_samples, self.model.n_members, plan.shape[0]
        chol = np.linalg.cholesky(self.integrity_cov())
        trajs, clean = [], []
        for m in range(members):
            clean.append(rollout(self.model, state, plan[None], member=m, bias=self._bias)[0])
            s = np.tile(state, (k, 1))
            traj = np.empty((k, h + 1, state.size))
            traj[:, 0] = s
            for t in range(h):
                s = self.model.predict(s, np.tile(plan[t], (k, 1)), member=m) + self._bias
                s = s + rng.standard_normal((k, state.size)) @ chol.T
                traj[:, t + 1] = s
            trajs.append(traj)
        report = self._scg_planning.evaluate(np.concatenate(trajs), self._view(cell))
        # Désaccord mesuré sur les 8 premiers pas (0,4 s) : le plan est rejoué à chaque pas.
        _, eff = forward_kinematics(np.stack(clean)[:, :9, :2], self.arm)  # (M, ≤ 9, 2)
        disagreement = float(np.max(np.linalg.norm(eff - eff.mean(axis=0), axis=-1)))
        return report.admissibility_ratio, disagreement, members * (k + 1) * h

    def integrity_cov(self) -> np.ndarray:
        return np.asarray(self.model.calibration.get("cov", self.model.residual_cov))

    def step(self, state: np.ndarray, target: np.ndarray, cell: CellState, rng: np.random.Generator) -> Decision:
        t0 = time.perf_counter()
        cfg = self.config
        step = self._step
        self._step += 1
        hold = self.hold_action(state)

        # 1. Intégrité : comparer la mesure à ce que le world model avait prédit.
        reading: IntegrityReading | None = None
        if self._predicted is not None:
            residual = state - self._predicted
            self.residuals.append(residual)
            # Correction de biais lente et bornée (3 σ) : supprime l'erreur statique sans masquer une anomalie,
            # car le filtre χ² juge toujours la prédiction brute du world model.
            sigma = np.sqrt(np.diag(self.integrity_cov()))
            self._bias[:] = np.clip(0.9 * self._bias + 0.1 * residual, -3 * sigma, 3 * sigma)
        if cfg.use_integrity:
            if self._predicted is not None:
                reading = self.integrity.update(state - self._predicted)
                self._last_d2_ratio = reading.d2 / reading.threshold
            else:
                reading = self.integrity.tick_frozen()
            if reading.status in (IntegrityStatus.FREEZE, IntegrityStatus.INTERVENTION):
                return self._hold(step, "INTERVENTION_REQUISE" if reading.status == IntegrityStatus.INTERVENTION
                                  else "CHI2_FREEZE", reading, None, None, None, None, 0, t0)

        # 2. Gate : budget de calcul.
        if cfg.use_gate:
            gate = self.gate.decide(state, self._view(cell), self._last_d2_ratio, self._last_robustness, self._held)
        else:
            gate = GateDecision(1.0, cfg.fixed_budget, {"fixed": 1.0})

        # 3. TAP + SCG.
        result = self.planner.plan(state, target, self._view(cell), gate.budget, hold, rng)
        model_steps, flops = result.model_steps, result.flops
        scg = result.scg.to_dict()
        if not result.has_candidate:
            self._last_robustness = 0.0
            return self._hold(step, "AUCUNE_TRAJECTOIRE_ADMISSIBLE", reading, gate, scg, None, None, flops, t0,
                              model_steps, result.pruned_preview)

        # 4. Robustesse du plan retenu (ensemble + bruit de procédé).
        robustness = disagreement = None
        for i in range(min(cfg.max_candidates, result.actions.shape[0])):
            robustness, disagreement, steps = self.robustness(state, result.actions[i], cell, rng)
            model_steps += steps
            flops += steps * (self.model.flops_per_sample + STEP_OVERHEAD_FLOPS)
            if robustness >= self.envelope.min_robustness and disagreement <= self.envelope.max_disagreement:
                break
        else:
            self._last_robustness = robustness or 0.0
            reason = "ROBUSTESSE_INSUFFISANTE" if (robustness or 0) < self.envelope.min_robustness else "MODELE_INCERTAIN"
            return self._hold(step, reason, reading, gate, scg, robustness, disagreement, flops, t0,
                              model_steps, result.pruned_preview)

        plan = result.actions[i]
        self._last_robustness = robustness
        self.planner.commit(plan)
        command = plan[0]
        self._predicted = self.model.predict(state[None], command[None])[0]
        model_steps += self.model.n_members
        flops += self.model.n_members * self.model.flops_per_sample
        self._held = False
        return Decision(step, "EXECUTE", "PLAN_ADMISSIBLE", command, reading, gate, scg, robustness, disagreement,
                        result.trajectories[i], result.pruned_preview, model_steps, flops,
                        (time.perf_counter() - t0) * 1000.0, {"candidate_rank": i})

    def _hold(self, step, reason, reading, gate, scg, robustness, disagreement, flops, t0, model_steps=0, pruned=None):
        self._predicted = None  # pas de prédiction à juger pendant l'arrêt (les freins agissent)
        self._held = True
        self.planner.reset()
        return Decision(step, "HOLD", reason, None, reading, gate, scg, robustness, disagreement, None, pruned,
                        model_steps, flops, (time.perf_counter() - t0) * 1000.0)

    def log(self, decision: Decision, target: np.ndarray, cell: CellState, state: np.ndarray) -> dict[str, Any] | None:
        if self.ledger is None:
            return None
        return self.ledger.append(decision.record(target, cell, state, self.envelope))
