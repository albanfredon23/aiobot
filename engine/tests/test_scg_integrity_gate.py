import numpy as np
import pytest

from aiobot.gate import ComplexityGate
from aiobot.integrity import IntegrityFilter, IntegrityStatus, calibrate
from aiobot.plant import ArmConfig, inverse_kinematics
from aiobot.scg import REJECTION_KEYS, CellState, SphericalConstraintGraph, Sphere
from aiobot.tap import FULL_BUDGET


def pose(xy, up=True, dq=(0.0, 0.0)):
    q = inverse_kinematics(np.array(xy), ArmConfig(), up)
    return np.concatenate([q, dq])


def test_sphere_et_plan_de_travail():
    scg = SphericalConstraintGraph()
    cell = CellState(obstacles=[Sphere((0.6, 0.3), 0.1)])
    inside = pose((0.6, 0.3))
    below = pose((0.7, -0.2))
    free = pose((0.3, 0.8), up=False)
    assert "obstacle_sphere" in scg.state_violations(inside, cell)
    assert "workspace_floor" in scg.state_violations(below, cell)
    assert scg.state_violations(free, cell) == []


def test_zone_collaborative_abaisse_la_vitesse():
    scg = SphericalConstraintGraph()
    near_operator = pose((0.72, 0.10), dq=(0.6, 0.0))  # ~0,4 m/s à l'outil
    alone = CellState()
    with_operator = CellState(operator=Sphere((0.98, 0.42), 0.22))
    assert "speed_limit" not in scg.state_violations(near_operator, alone)
    assert "speed_limit" in scg.state_violations(near_operator, with_operator)


def test_attribution_a_la_premiere_contrainte():
    scg = SphericalConstraintGraph()
    cell = CellState(obstacles=[Sphere((0.6, 0.3), 0.1)])
    traj = np.stack([np.stack([pose((0.3, 0.8), up=False)] * 4), np.stack([pose((0.6, 0.3))] * 4)])
    report = scg.evaluate(traj, cell)
    assert report.admissible.tolist() == [True, False]
    assert sum(report.rejections.values()) == 1 and report.rejections["obstacle_sphere"] == 1
    assert set(report.rejections) == set(REJECTION_KEYS)


def make_filter(rng):
    return IntegrityFilter(**{k: v for k, v in zip(("cov", "threshold", "threshold_freeze"),
                                                   (lambda c: (c["cov"], float(c["threshold"]), float(c["threshold_freeze"])))(
                                                       calibrate(rng.normal(0, 0.01, (2000, 4)))))})


def test_chi2_nominal_puis_gel_puis_intervention():
    rng = np.random.default_rng(0)
    f = make_filter(rng)
    assert f.threshold == pytest.approx(13.28, abs=1.5)
    statuses = [f.update(rng.normal(0, 0.01, 4)).status for _ in range(300)]
    assert IntegrityStatus.FREEZE not in statuses
    assert f.update(np.full(4, 0.2)).status == IntegrityStatus.FREEZE
    for _ in range(40):  # écart persistant : gels successifs puis intervention humaine
        reading = f.update(np.full(4, 0.2))
        if not f.frozen:
            continue
        for _ in range(f.config.cooldown):
            f.tick_frozen()
    assert f.needs_intervention and reading.status == IntegrityStatus.INTERVENTION


def test_gate_budget_proportionnel_a_la_difficulte():
    gate = ComplexityGate()
    far = pose((0.3, 0.8), up=False)
    easy = gate.decide(far, CellState(), chi2_ratio=0.0, last_robustness=1.0, resumed=False)
    hard = gate.decide(pose((0.72, 0.1)), CellState(operator=Sphere((0.98, 0.42), 0.22)), 0.1, 1.0, False)
    resumed = gate.decide(far, CellState(), 0.1, 1.0, resumed=True)
    assert easy.budget.n_paths == gate.config.min_paths and easy.budget.horizon == gate.config.min_horizon
    assert hard.budget.rollout_steps() > easy.budget.rollout_steps()
    assert resumed.budget == FULL_BUDGET
