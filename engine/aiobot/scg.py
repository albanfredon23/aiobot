"""SCG — Spherical Constraint Graph : le garde-fou physique et de sécurité.

Chaque trajectoire imaginée par le world model est une suite d'états
(N, H + 1, 4). Le SCG la projette dans l'espace de travail (positions du coude
et de l'effecteur, vitesse cartésienne de l'effecteur) puis vérifie un graphe
de contraintes :

    limites articulaires ── plan de travail ── sphères d'obstacles
                                                     │
    présence opérateur ──▶ sphère de protection ──▶ zone collaborative
                                                     │
                                          limite de vitesse réduite

Les volumes interdits sont des sphères (disques dans le plan du bras) : la
distance d'un point à une sphère est |p − c| − r, ce qui rend le test
exact, vectorisé et lisible dans le registre d'audit. Les arêtes du graphe
expriment les dépendances : la présence d'un opérateur active sa sphère de
protection et abaisse la vitesse autorisée dans la zone collaborative qui
l'entoure (logique de surveillance de vitesse et de séparation).

Chaque trajectoire rejetée est attribuée à la première contrainte violée,
dans l'ordre de ``REJECTION_KEYS``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .plant import ArmConfig, effector_velocity, forward_kinematics

REJECTION_KEYS = (
    "joint_limit",
    "workspace_floor",
    "obstacle_sphere",
    "operator_sphere",
    "speed_limit",
)


@dataclass(frozen=True)
class Sphere:
    center: tuple[float, float]
    radius: float
    label: str = ""

    def distance(self, points: np.ndarray) -> np.ndarray:
        return np.linalg.norm(points - np.asarray(self.center), axis=-1) - self.radius

    def to_dict(self) -> dict:
        return {"center": [round(c, 4) for c in self.center], "radius": round(self.radius, 4), "label": self.label}


@dataclass(frozen=True)
class SafetyEnvelope:
    floor_y: float = -0.12  # plan de travail (m), coude et effecteur au-dessus
    max_joint_speed: float = 4.0  # rad/s
    max_tool_speed: float = 1.5  # m/s hors zone collaborative
    collaborative_speed: float = 0.25  # m/s dans la zone collaborative
    collaborative_margin: float = 0.35  # m autour de la sphère de l'opérateur
    clearance: float = 0.02  # marge de sécurité sur toutes les sphères (m)
    min_robustness: float = 0.75  # part minimale de déroulés admissibles pour agir
    max_disagreement: float = 0.04  # m, désaccord maximal des membres du world model sur l'effecteur

    def labels(self) -> list[str]:
        return [
            f"FLOOR_Y_{self.floor_y:g}M",
            f"MAX_TOOL_SPEED_{self.max_tool_speed:g}MS",
            f"COLLAB_SPEED_{self.collaborative_speed:g}MS",
            f"MIN_ROBUSTNESS_{self.min_robustness * 100:g}%",
        ]


@dataclass
class CellState:
    """Ce que la cellule sait de son environnement à l'instant t (capteurs de zone, configuration)."""

    obstacles: list[Sphere] = field(default_factory=list)
    operator: Sphere | None = None

    def to_dict(self) -> dict:
        return {
            "obstacles": [o.to_dict() for o in self.obstacles],
            "operator": self.operator.to_dict() if self.operator else None,
        }


@dataclass
class SCGReport:
    n_trajectories: int
    admissible: np.ndarray  # (N,) booléens
    rejections: dict[str, int]
    min_clearance: np.ndarray  # (N,) plus petite distance aux sphères (m)

    @property
    def admissibility_ratio(self) -> float:
        return float(self.admissible.mean()) if self.n_trajectories else 0.0

    @property
    def pruning_rate(self) -> float:
        return 1.0 - self.admissibility_ratio

    def to_dict(self) -> dict:
        return {
            "n_trajectories": self.n_trajectories,
            "admissible": int(self.admissible.sum()),
            "admissibility_ratio": round(self.admissibility_ratio, 4),
            "rejections": dict(self.rejections),
        }


class SphericalConstraintGraph:
    def __init__(self, arm: ArmConfig | None = None, envelope: SafetyEnvelope | None = None) -> None:
        self.arm = arm or ArmConfig()
        self.envelope = envelope or SafetyEnvelope()

    def speed_limit(self, effector: np.ndarray, cell: CellState) -> np.ndarray:
        """Vitesse d'outil autorisée en chaque point (arête opérateur → zone collaborative → vitesse)."""
        env = self.envelope
        limit = np.full(effector.shape[:-1], env.max_tool_speed)
        if cell.operator is not None:
            inside = cell.operator.distance(effector) < env.collaborative_margin
            limit = np.where(inside, env.collaborative_speed, limit)
        return limit

    def violations(self, states: np.ndarray, cell: CellState) -> dict[str, np.ndarray]:
        """Violations par contrainte, (…, T) booléens pour des états (…, T, 4)."""
        arm, env = self.arm, self.envelope
        q, dq = states[..., :2], states[..., 2:]
        elbow, effector = forward_kinematics(q, arm)
        speed = np.linalg.norm(effector_velocity(states, arm), axis=-1)
        out = {
            "joint_limit": np.any(q < np.array(arm.q_low), axis=-1) | np.any(q > np.array(arm.q_high), axis=-1)
            | np.any(np.abs(dq) > env.max_joint_speed, axis=-1),
            "workspace_floor": (elbow[..., 1] < env.floor_y) | (effector[..., 1] < env.floor_y),
            "obstacle_sphere": np.zeros(states.shape[:-1], dtype=bool),
            "operator_sphere": np.zeros(states.shape[:-1], dtype=bool),
            "speed_limit": speed > self.speed_limit(effector, cell) + 1e-9,
        }
        for sphere in cell.obstacles:
            out["obstacle_sphere"] |= self._hits(sphere, elbow, effector)
        if cell.operator is not None:
            out["operator_sphere"] |= self._hits(cell.operator, elbow, effector)
        return out

    def _hits(self, sphere: Sphere, elbow: np.ndarray, effector: np.ndarray) -> np.ndarray:
        """Le segment avant-bras (coude → effecteur) ou le coude pénètre-t-il la sphère ?"""
        c = np.asarray(sphere.center)
        seg = effector - elbow
        t = np.clip(np.sum((c - elbow) * seg, axis=-1) / np.maximum(np.sum(seg * seg, axis=-1), 1e-12), 0.0, 1.0)
        closest = elbow + t[..., None] * seg
        return (sphere.distance(closest) < self.envelope.clearance) | (sphere.distance(elbow) < self.envelope.clearance)

    def margin_penalty(self, states: np.ndarray, cell: CellState) -> np.ndarray:
        """Pénalité douce (N,) quand une trajectoire frôle une contrainte sans la violer.

        Sert au coût du planificateur : un plan qui longe une limite passerait le
        SCG mais échouerait au contrôle de robustesse au moindre bruit.
        """
        arm, env = self.arm, self.envelope
        q, dq = states[..., :2], states[..., 2:]
        elbow, effector = forward_kinematics(q, arm)
        speed = np.linalg.norm(effector_velocity(states, arm), axis=-1)
        limit = self.speed_limit(effector, cell)
        pen = np.maximum(0.0, speed / limit - 0.7) ** 2
        pen = pen + np.sum(np.maximum(0.0, np.abs(dq) / env.max_joint_speed - 0.7) ** 2, axis=-1)
        low, high = np.array(arm.q_low), np.array(arm.q_high)
        pen = pen + np.sum(np.maximum(0.0, 0.15 - (q - low)) ** 2 + np.maximum(0.0, 0.15 - (high - q)) ** 2, axis=-1) * 20
        for y in (elbow[..., 1], effector[..., 1]):
            pen = pen + np.maximum(0.0, 0.06 - (y - env.floor_y)) ** 2 * 200
        for sphere in cell.obstacles + ([cell.operator] if cell.operator else []):
            for p in (elbow, effector, 0.5 * (elbow + effector)):
                pen = pen + np.maximum(0.0, 0.08 - sphere.distance(p)) ** 2 * 200
        return pen.mean(axis=-1)

    def clearance(self, states: np.ndarray, cell: CellState) -> np.ndarray:
        """Plus petite distance de l'effecteur aux sphères actives le long de chaque trajectoire."""
        _, effector = forward_kinematics(states[..., :2], self.arm)
        spheres = cell.obstacles + ([cell.operator] if cell.operator else [])
        if not spheres:
            return np.full(states.shape[:-2], np.inf)
        return np.min([s.distance(effector).min(axis=-1) for s in spheres], axis=0)

    def evaluate(self, trajectories: np.ndarray, cell: CellState) -> SCGReport:
        """Élagage du faisceau : trajectoires (N, H + 1, 4) → masque des trajectoires admissibles.

        L'état initial (indice 0) est l'état mesuré : il n'est pas jugé, seul le futur imaginé l'est.
        """
        future = trajectories[:, 1:]
        per_key = {k: v.any(axis=-1) for k, v in self.violations(future, cell).items()}
        rejected = np.zeros(trajectories.shape[0], dtype=bool)
        rejections = {}
        for key in REJECTION_KEYS:
            fresh = per_key[key] & ~rejected
            rejections[key] = int(fresh.sum())
            rejected |= fresh
        return SCGReport(trajectories.shape[0], ~rejected, rejections, self.clearance(future, cell))

    def state_violations(self, state: np.ndarray, cell: CellState) -> list[str]:
        """Contraintes violées par un état réel (mesure de vérité terrain du benchmark)."""
        return [k for k, v in self.violations(state[None, None, :], cell).items() if bool(v[0, 0])]
