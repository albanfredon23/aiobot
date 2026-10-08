"""Outils statistiques sans dépendance lourde (pas de SciPy) : loi du χ².

- ``chi2_sf`` : fonction de survie de la loi du χ² (p-value), via la fonction
  gamma incomplète régularisée (séries + fraction continue de Lentz).
- ``chi2_ppf`` : quantile de la loi du χ² (seuil critique), par bissection.
"""
from __future__ import annotations

import math


_EPS = 1e-15
_TINY = 1e-300
_MAX_ITER = 10_000


def _gamma_p_series(a: float, x: float) -> float:
    """Gamma incomplète inférieure régularisée P(a, x) par développement en série."""
    ap = a
    term = 1.0 / a
    total = term
    for _ in range(_MAX_ITER):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * _EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_q_continued_fraction(a: float, x: float) -> float:
    """Gamma incomplète supérieure régularisée Q(a, x) par fraction continue (Lentz)."""
    b = x + 1.0 - a
    c = 1.0 / _TINY
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_ITER):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _TINY:
            d = _TINY
        c = b + an / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(statistic: float, df: int) -> float:
    """P(X >= statistic) pour X ~ χ²(df)."""
    if df <= 0:
        raise ValueError("df doit être strictement positif")
    if statistic <= 0:
        return 1.0
    a = df / 2.0
    x = statistic / 2.0
    if x < a + 1.0:
        return max(0.0, min(1.0, 1.0 - _gamma_p_series(a, x)))
    return max(0.0, min(1.0, _gamma_q_continued_fraction(a, x)))


def chi2_ppf(probability: float, df: int) -> float:
    """Quantile x tel que P(X <= x) = probability pour X ~ χ²(df). Ex. chi2_ppf(0.99, 4) = 13,28."""
    if not 0.0 < probability < 1.0:
        raise ValueError("probability doit être dans ]0, 1[")
    target = 1.0 - probability
    lo, hi = 0.0, max(1.0, float(df))
    while chi2_sf(hi, df) > target:
        hi *= 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if chi2_sf(mid, df) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10 * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


