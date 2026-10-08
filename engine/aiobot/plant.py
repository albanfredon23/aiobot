"""Cellule robotisée simulée : bras 2 axes dans un plan vertical, avec charge utile.

C'est le « monde réel » du démonstrateur. Le world model ne le voit jamais
directement : il n'apprend qu'à partir de transitions (état, action, état
suivant), comme il le ferait sur les journaux d'un robot réel.

Dynamique du manipulateur (masses ponctuelles au bout des segments) :

    M(q) q̈ + C(q, q̇) + G(q) + B q̇ = τ

- q = (q1 épaule, q2 coude) en radians, q̇ en rad/s ;
- τ = couples moteur, commandés par une action a ∈ [−1 ; 1]² multipliée par
  le couple maximal de chaque axe ;
- la charge utile s'ajoute à la masse de l'effecteur. Une charge hors de la
  plage nominale (scénario « charge ») change la dynamique sans prévenir.

Un pas de commande dure ``dt`` = 50 ms, intégré en 5 sous-pas (Euler
semi-implicite). L'arrêt sûr est un arrêt de catégorie 1 : décélération
commandée puis freins, la position est tenue.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

GRAVITY = 9.81


@dataclass(frozen=True)
class ArmConfig:
    l1: float = 0.55  # m
    l2: float = 0.45  # m
    m1: float = 1.2  # kg, masse ramenée au coude
    m2: float = 0.8  # kg, masse ramenée à l'effecteur (sans charge)
    damping: tuple[float, float] = (0.6, 0.4)  # N·m·s/rad
    tau_max: tuple[float, float] = (32.0, 14.0)  # N·m
    dt: float = 0.05  # s, période de commande
    substeps: int = 5
    nominal_payload: tuple[float, float] = (0.0, 0.3)  # kg, plage vue à l'apprentissage
    q_low: tuple[float, float] = (-0.35, -2.6)
    q_high: tuple[float, float] = (np.pi + 0.35, 2.6)
    brake_decay: float = 0.35  # part de vitesse conservée par pas pendant l'arrêt sûr


STATE_DIM = 4  # q1, q2, q̇1, q̇2
ACTION_DIM = 2


def forward_kinematics(q: np.ndarray, cfg: ArmConfig) -> tuple[np.ndarray, np.ndarray]:
    """Positions (…, 2) du coude et de l'effecteur pour des angles q (…, 2). Base en (0, 0)."""
    q1, q2 = q[..., 0], q[..., 1]
    elbow = np.stack([cfg.l1 * np.cos(q1), cfg.l1 * np.sin(q1)], axis=-1)
    effector = elbow + np.stack([cfg.l2 * np.cos(q1 + q2), cfg.l2 * np.sin(q1 + q2)], axis=-1)
    return elbow, effector


def effector_velocity(state: np.ndarray, cfg: ArmConfig) -> np.ndarray:
    """Vitesse cartésienne (…, 2) de l'effecteur : v = J(q) q̇."""
    q1, q2, d1, d2 = state[..., 0], state[..., 1], state[..., 2], state[..., 3]
    s1, c1 = np.sin(q1), np.cos(q1)
    s12, c12 = np.sin(q1 + q2), np.cos(q1 + q2)
    vx = -cfg.l1 * s1 * d1 - cfg.l2 * s12 * (d1 + d2)
    vy = cfg.l1 * c1 * d1 + cfg.l2 * c12 * (d1 + d2)
    return np.stack([vx, vy], axis=-1)


def inverse_kinematics(target: np.ndarray, cfg: ArmConfig, elbow_up: bool = True) -> np.ndarray:
    """Angles atteignant une cible cartésienne (coude en haut par défaut)."""
    x, y = float(target[0]), float(target[1])
    c2 = np.clip((x * x + y * y - cfg.l1**2 - cfg.l2**2) / (2 * cfg.l1 * cfg.l2), -1.0, 1.0)
    q2 = -np.arccos(c2) if elbow_up else np.arccos(c2)
    q1 = np.arctan2(y, x) - np.arctan2(cfg.l2 * np.sin(q2), cfg.l1 + cfg.l2 * np.cos(q2))
    return np.array([q1, q2])


@dataclass
class ArmPlant:
    cfg: ArmConfig = field(default_factory=ArmConfig)
    payload: float = 0.1
    state: np.ndarray = field(default_factory=lambda: np.array([np.pi / 2, -np.pi / 2, 0.0, 0.0]))

    def accelerations(self, state: np.ndarray, tau: np.ndarray) -> np.ndarray:
        cfg = self.cfg
        q1, q2, d1, d2 = state
        m1, m2 = cfg.m1, cfg.m2 + self.payload
        l1, l2 = cfg.l1, cfg.l2
        c2, s2 = np.cos(q2), np.sin(q2)
        m11 = (m1 + m2) * l1**2 + m2 * l2**2 + 2 * m2 * l1 * l2 * c2
        m12 = m2 * l2**2 + m2 * l1 * l2 * c2
        m22 = m2 * l2**2
        h = m2 * l1 * l2 * s2
        coriolis = np.array([-h * (2 * d1 * d2 + d2 * d2), h * d1 * d1])
        g1 = (m1 + m2) * GRAVITY * l1 * np.cos(q1) + m2 * GRAVITY * l2 * np.cos(q1 + q2)
        g2 = m2 * GRAVITY * l2 * np.cos(q1 + q2)
        rhs = tau - coriolis - np.array([g1, g2]) - np.array(cfg.damping) * np.array([d1, d2])
        det = m11 * m22 - m12 * m12
        return np.array([m22 * rhs[0] - m12 * rhs[1], -m12 * rhs[0] + m11 * rhs[1]]) / det

    def step(self, action: np.ndarray, external_impulse: np.ndarray | None = None) -> np.ndarray:
        """Applique l'action pendant un pas de commande et renvoie le nouvel état."""
        cfg = self.cfg
        tau = np.clip(np.asarray(action, dtype=float), -1.0, 1.0) * np.array(cfg.tau_max)
        s = self.state.copy()
        h = cfg.dt / cfg.substeps
        for _ in range(cfg.substeps):
            acc = self.accelerations(s, tau)
            s[2:] += h * acc
            s[:2] += h * s[2:]
        if external_impulse is not None:
            s[2:] += external_impulse
        self.state = s
        return s.copy()

    def brake(self) -> np.ndarray:
        """Arrêt sûr (catégorie 1) : la vitesse décroît puis les freins tiennent la position."""
        s = self.state.copy()
        s[2:] *= self.cfg.brake_decay
        s[:2] += self.cfg.dt * s[2:]
        if np.all(np.abs(s[2:]) < 1e-3):
            s[2:] = 0.0
        self.state = s
        return s.copy()

    def gravity_action(self, state: np.ndarray | None = None) -> np.ndarray:
        """Action qui compense la gravité à l'arrêt (sert à initialiser les plans)."""
        cfg = self.cfg
        q1, q2 = (self.state if state is None else state)[:2]
        m2 = cfg.m2 + self.payload
        g1 = (cfg.m1 + m2) * GRAVITY * cfg.l1 * np.cos(q1) + m2 * GRAVITY * cfg.l2 * np.cos(q1 + q2)
        g2 = m2 * GRAVITY * cfg.l2 * np.cos(q1 + q2)
        return np.clip(np.array([g1, g2]) / np.array(cfg.tau_max), -1.0, 1.0)


def exploration_dataset(
    n_transitions: int, rng: np.random.Generator, cfg: ArmConfig | None = None, episode_len: int = 60
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Transitions (état, action, état suivant) collectées par une exploration aléatoire lissée.

    La charge utile est tirée dans la plage nominale à chaque épisode ; les
    épisodes qui sortent de l'espace de travail sont interrompus.
    """
    cfg = cfg or ArmConfig()
    states, actions, nexts = [], [], []
    low, high = np.array(cfg.q_low), np.array(cfg.q_high)
    while len(states) < n_transitions:
        plant = ArmPlant(cfg=cfg, payload=float(rng.uniform(*cfg.nominal_payload)))
        q = rng.uniform(low + 0.3, high - 0.3)
        plant.state = np.concatenate([q, rng.normal(0.0, 0.5, size=2)])
        a = plant.gravity_action()
        for _ in range(episode_len):
            # Bruit d'Ornstein-Uhlenbeck autour de la compensation de gravité : mouvements variés et lisses.
            a = 0.8 * a + 0.2 * plant.gravity_action() + rng.normal(0.0, 0.25, size=2)
            a = np.clip(a, -1.0, 1.0)
            s = plant.state.copy()
            s_next = plant.step(a)
            states.append(s)
            actions.append(a.copy())
            nexts.append(s_next)
            if np.any(s_next[:2] < low) or np.any(s_next[:2] > high) or np.any(np.abs(s_next[2:]) > 5.0):
                break
    return np.array(states[:n_transitions]), np.array(actions[:n_transitions]), np.array(nexts[:n_transitions])
