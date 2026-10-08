"""TAP — Trajectory Admissible Planning dans le world model.

Le robot ne choisit pas une action « au jugé » : à chaque pas de commande, il
imagine N suites d'actions sur un horizon de H pas, les déroule dans le world
model, laisse le SCG éliminer celles qui violent une contrainte, et retient la
meilleure parmi les survivantes. Seule sa première action est exécutée, puis
tout recommence au pas suivant (commande prédictive, ou MPC).

Échantillonnage : autour du plan retenu au pas précédent (décalé d'un pas),
avec un bruit gaussien, plus deux candidats structurés (maintien sur place et
plan précédent tel quel). ``iterations`` > 1 raffine la moyenne sur les
meilleures trajectoires admissibles (méthode de l'entropie croisée).

Le faisceau est imaginé avec un seul membre du world model (le moins cher) ;
le plan retenu est ensuite rejoué par tous les membres, avec bruit, pour le
contrôle de robustesse (voir ``controller``).

Comptage du calcul : les FLOP du world model sont exacts (couches du réseau) ;
cinématique, contraintes et coût sont estimés à 120 FLOP par pas imaginé.

La cible est une configuration articulaire (point appris, calculé une fois
par cinématique inverse) : cela fixe la posture du coude et évite les minima
locaux d'un coût purement cartésien.

Coût d'une trajectoire : distance de l'effecteur à la cible et écart
articulaire le long de l'horizon (pondérés vers la fin), vitesse résiduelle à l'arrivée, effort
moteur au-delà de la compensation de gravité, et à-coups de commande.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .plant import ArmConfig, effector_velocity, forward_kinematics
from .scg import CellState, SCGReport, SphericalConstraintGraph
from .world_model import WorldModel


@dataclass(frozen=True)
class PlanBudget:
    n_paths: int = 256
    horizon: int = 16
    iterations: int = 2

    def __post_init__(self) -> None:
        if self.n_paths < 8 or self.horizon < 2 or self.iterations < 1:
            raise ValueError("n_paths >= 8, horizon >= 2 et iterations >= 1 requis")

    def rollout_steps(self) -> int:
        return self.n_paths * self.horizon * self.iterations

    def to_dict(self) -> dict:
        return {"n_paths": self.n_paths, "horizon": self.horizon, "iterations": self.iterations}


FULL_BUDGET = PlanBudget(256, 12, 2)
STEP_OVERHEAD_FLOPS = 120  # estimation : cinématique, contraintes et coût par pas imaginé


@dataclass
class PlanResult:
    actions: np.ndarray  # (K, H, 2) candidats admissibles, du meilleur au moins bon
    costs: np.ndarray  # (K,)
    trajectories: np.ndarray  # (K, H + 1, 4)
    scg: SCGReport  # rapport du dernier tour d'élagage
    pruned_preview: np.ndarray  # quelques trajectoires rejetées (pour l'affichage), (P, H + 1, 4)
    model_steps: int  # nombre de prédictions d'un pas effectuées
    flops: int

    @property
    def has_candidate(self) -> bool:
        return self.actions.shape[0] > 0


def rollout(
    model: WorldModel, state: np.ndarray, actions: np.ndarray, member: int | None = None, bias: np.ndarray | None = None
) -> np.ndarray:
    """Déroule des suites d'actions (N, H, 2) depuis un état (4,) → trajectoires (N, H + 1, 4).

    ``bias`` : correction de biais par pas estimée en ligne (commande prédictive sans erreur statique).
    """
    n, h, _ = actions.shape
    traj = np.empty((n, h + 1, state.shape[-1]))
    traj[:, 0] = state
    s = np.broadcast_to(state, (n, state.shape[-1])).copy()
    for t in range(h):
        s = model.predict(s, actions[:, t], member=member)
        if bias is not None:
            s = s + bias
        traj[:, t + 1] = s
    return traj


def smooth_noise(rng: np.random.Generator, n: int, h: int, beta: float = 0.7) -> np.ndarray:
    """Bruit gaussien corrélé dans le temps (AR(1), variance marginale 1) : des gestes lisses, pas du tremblement."""
    eps = rng.standard_normal((n, h, 2))
    out = np.empty_like(eps)
    out[:, 0] = eps[:, 0]
    scale = np.sqrt(1.0 - beta * beta)
    for t in range(1, h):
        out[:, t] = beta * out[:, t - 1] + scale * eps[:, t]
    return out


def trajectory_cost(traj: np.ndarray, actions: np.ndarray, target_q: np.ndarray, hold: np.ndarray, arm: ArmConfig) -> np.ndarray:
    """Coût (N,) vers une configuration articulaire cible (point appris, comme en atelier)."""
    _, eff = forward_kinematics(traj[:, 1:, :2], arm)
    _, goal = forward_kinematics(np.asarray(target_q), arm)
    joint_dist = np.linalg.norm(traj[:, 1:, :2] - target_q, axis=-1)
    dist = np.linalg.norm(eff - goal, axis=-1) + 0.3 * joint_dist
    h = dist.shape[1]
    weights = np.linspace(0.3, 1.0, h)
    weights[-1] = 3.0
    near = np.exp(-dist[:, -1] / 0.08)
    speed_end = np.linalg.norm(effector_velocity(traj[:, -1], arm), axis=-1)
    effort = np.mean(np.sum((actions - hold) ** 2, axis=-1), axis=-1)
    jerk = np.mean(np.sum(np.diff(actions, axis=1) ** 2, axis=-1), axis=-1) if h > 1 else 0.0
    return (dist * weights).sum(axis=1) / weights.sum() + 0.4 * near * speed_end + 0.02 * effort + 0.05 * jerk


class TAPPlanner:
    def __init__(self, model: WorldModel, scg: SphericalConstraintGraph, noise: float = 0.3, elite_frac: float = 0.1) -> None:
        self.model = model
        self.scg = scg
        self.arm = scg.arm
        self.noise = noise
        self.elite_frac = elite_frac
        self._previous: np.ndarray | None = None
        self.bias: np.ndarray | None = None

    def reset(self) -> None:
        self._previous = None

    def warm_start(self, horizon: int, hold: np.ndarray) -> np.ndarray:
        if self._previous is None:
            return np.tile(hold, (horizon, 1))
        prev = np.vstack([self._previous[1:], self._previous[-1:]])
        if prev.shape[0] >= horizon:
            return prev[:horizon]
        return np.vstack([prev, np.tile(prev[-1], (horizon - prev.shape[0], 1))])

    def commit(self, plan: np.ndarray) -> None:
        self._previous = plan.copy()

    def plan(
        self,
        state: np.ndarray,
        target: np.ndarray,
        cell: CellState,
        budget: PlanBudget,
        hold: np.ndarray,
        rng: np.random.Generator,
    ) -> PlanResult:
        h, n = budget.horizon, budget.n_paths
        mean = self.warm_start(h, hold)
        sigma = np.full((h, 2), self.noise)
        steps = 0
        for it in range(budget.iterations):
            actions = mean + sigma * smooth_noise(rng, n, h)
            actions[0] = np.tile(hold, (h, 1))  # candidat « rester sur place »
            actions[1] = mean  # plan précédent tel quel
            actions = np.clip(actions, -1.0, 1.0)
            traj = rollout(self.model, state, actions, member=0, bias=self.bias)
            steps += n * h
            report = self.scg.evaluate(traj, cell)
            cost = trajectory_cost(traj, actions, target, hold, self.arm) + self.scg.margin_penalty(traj[:, 1:], cell)
            ok = report.admissible
            if ok.sum() >= 2:
                # Moyenne pondérée des trajectoires admissibles (MPPI), rejouée et jugée comme un candidat de plus.
                c_ok = cost[ok]
                temp = max(1e-6, 0.2 * (np.median(c_ok) - c_ok.min()))
                w = np.exp(-(c_ok - c_ok.min()) / temp)
                blended = np.clip(np.tensordot(w / w.sum(), actions[ok], axes=1), -1.0, 1.0)
                b_traj = rollout(self.model, state, blended[None], member=0, bias=self.bias)
                steps += h
                b_ok = self.scg.evaluate(b_traj, cell).admissible[0]
                if b_ok:
                    b_cost = trajectory_cost(b_traj, blended[None], target, hold, self.arm) + self.scg.margin_penalty(b_traj[:, 1:], cell)
                    actions = np.concatenate([actions, blended[None]])
                    traj = np.concatenate([traj, b_traj])
                    cost = np.concatenate([cost, b_cost])
                    ok = np.concatenate([ok, [True]])
                if it < budget.iterations - 1:
                    n_elite = max(2, int(self.elite_frac * ok.sum()))
                    idx = np.flatnonzero(ok)[np.argsort(cost[ok])[:n_elite]]
                    mean = actions[idx].mean(axis=0)
                    sigma = np.maximum(actions[idx].std(axis=0), 0.05)
        order = np.flatnonzero(ok)[np.argsort(cost[ok])]
        rejected = np.flatnonzero(~ok)
        preview = traj[rejected[:: max(1, rejected.size // 12)][:12]] if rejected.size else traj[:0]
        flops = steps * (self.model.flops_per_sample + STEP_OVERHEAD_FLOPS)
        return PlanResult(actions[order], cost[order], traj[order], report, preview, steps, flops)
