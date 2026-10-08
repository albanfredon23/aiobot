"""Filtre d'intégrité χ² : le monde se comporte-t-il comme le world model l'avait prédit ?

À chaque pas, le world model a prédit l'état suivant à partir de l'état
mesuré et de l'action envoyée. Le résidu r = s_mesuré − s_prédit est comparé
à sa covariance de référence Σ, étalonnée en fonctionnement nominal :

    d² = rᵀ Σ⁻¹ r,   d² ~ χ²(4) si le monde est conforme au modèle.

- ``NOMINAL`` : d² sous le seuil d'alerte ;
- ``ALERT``   : dépassement isolé (pas d'arrêt) ;
- ``FREEZE``  : d² au-dessus du seuil de gel, ou 3 alertes sur les 5 derniers
  pas → arrêt sûr pendant ``cooldown`` pas sans nouvelle alerte.

Un écart persistant (charge inconnue, choc, capteur défaillant) déclenche
plusieurs gels successifs : au-delà de ``max_freezes`` gels dans une mission,
le robot reste à l'arrêt et demande une intervention humaine, car le modèle
ne décrit plus la machine réelle.

Les résidus d'un modèle appris ne sont pas exactement gaussiens : comme dans
AIOTrade, le seuil retenu est le plus strict des deux, max(seuil théorique,
quantile empirique au même niveau en fonctionnement nominal).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from .plant import STATE_DIM
from .stats import chi2_ppf

FEATURES = ("q1", "q2", "dq1", "dq2")


class IntegrityStatus(str, Enum):
    NOMINAL = "NOMINAL"
    ALERT = "ALERT"
    FREEZE = "FREEZE"
    INTERVENTION = "INTERVENTION"


@dataclass(frozen=True)
class IntegrityConfig:
    alpha: float = 0.01
    alpha_freeze: float = 1e-4
    persistence_alerts: int = 3
    persistence_window: int = 5
    cooldown: int = 6
    max_freezes: int = 3


@dataclass
class IntegrityReading:
    d2: float
    threshold: float
    status: IntegrityStatus
    contributions: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "d2": round(self.d2, 3),
            "threshold": round(self.threshold, 2),
            "status": self.status.value,
            "contributions": {k: round(v, 3) for k, v in self.contributions.items()},
        }


class IntegrityFilter:
    def __init__(
        self,
        cov: np.ndarray,
        threshold: float | None = None,
        threshold_freeze: float | None = None,
        config: IntegrityConfig | None = None,
    ) -> None:
        self.config = config or IntegrityConfig()
        cov = np.asarray(cov, dtype=float)
        if cov.shape != (STATE_DIM, STATE_DIM):
            raise ValueError("covariance 4 × 4 attendue")
        self.inv = np.linalg.inv(cov)
        self.threshold_theory = chi2_ppf(1.0 - self.config.alpha, STATE_DIM)  # 13,28
        self.threshold_freeze_theory = chi2_ppf(1.0 - self.config.alpha_freeze, STATE_DIM)  # 23,51
        self.threshold = max(self.threshold_theory, threshold or 0.0)
        self.threshold_freeze = max(self.threshold_freeze_theory, threshold_freeze or 0.0, self.threshold)
        self.reset()

    def reset(self) -> None:
        self._recent = 0
        self._freeze_left = 0
        self.freezes = 0

    @property
    def frozen(self) -> bool:
        return self._freeze_left > 0 or self.needs_intervention

    @property
    def needs_intervention(self) -> bool:
        return self.freezes >= self.config.max_freezes

    def distance(self, residual: np.ndarray) -> tuple[float, np.ndarray]:
        tmp = self.inv @ residual
        return float(residual @ tmp), residual * tmp

    def update(self, residual: np.ndarray) -> IntegrityReading:
        cfg = self.config
        d2, contrib = self.distance(np.asarray(residual, dtype=float))
        alert = d2 > self.threshold
        self._recent = (self._recent << 1 | int(alert)) & ((1 << cfg.persistence_window) - 1)
        persistent = bin(self._recent).count("1") >= cfg.persistence_alerts
        if (d2 > self.threshold_freeze or persistent) and self._freeze_left == 0:
            self.freezes += 1
            self._freeze_left = cfg.cooldown
            self._recent = 0
        elif self._freeze_left > 0:
            self._freeze_left = cfg.cooldown if alert else self._freeze_left - 1
        if self.needs_intervention:
            status = IntegrityStatus.INTERVENTION
        elif self._freeze_left > 0:
            status = IntegrityStatus.FREEZE
        else:
            status = IntegrityStatus.ALERT if alert else IntegrityStatus.NOMINAL
        return IntegrityReading(d2, self.threshold, status, dict(zip(FEATURES, contrib.tolist())))

    def tick_frozen(self) -> IntegrityReading:
        """Pas passé à l'arrêt sûr : aucune prédiction à juger, le compte à rebours avance."""
        if self._freeze_left > 0:
            self._freeze_left -= 1
        status = IntegrityStatus.INTERVENTION if self.needs_intervention else (
            IntegrityStatus.FREEZE if self._freeze_left > 0 else IntegrityStatus.NOMINAL
        )
        return IntegrityReading(0.0, self.threshold, status, dict.fromkeys(FEATURES, 0.0))


def calibrate(residuals: np.ndarray, config: IntegrityConfig | None = None) -> dict[str, np.ndarray]:
    """Étalonnage en boucle fermée nominale : covariance des résidus et seuils empiriques."""
    cfg = config or IntegrityConfig()
    residuals = np.asarray(residuals, dtype=float)
    if residuals.shape[0] < 300:
        raise ValueError("au moins 300 pas nominaux sont nécessaires pour étalonner le filtre")
    cov = np.cov(residuals.T) + 1e-12 * np.eye(STATE_DIM)
    inv = np.linalg.inv(cov)
    d2 = np.einsum("ni,ij,nj->n", residuals, inv, residuals)
    alert = max(chi2_ppf(1 - cfg.alpha, STATE_DIM), float(np.quantile(d2, 1 - cfg.alpha)))
    freeze_emp = float(np.quantile(d2, 0.999)) * chi2_ppf(1 - cfg.alpha_freeze, STATE_DIM) / chi2_ppf(0.999, STATE_DIM)
    freeze = max(chi2_ppf(1 - cfg.alpha_freeze, STATE_DIM), freeze_emp, alert)
    return {"cov": cov, "threshold": np.array(alert), "threshold_freeze": np.array(freeze), "n": np.array(d2.size)}
