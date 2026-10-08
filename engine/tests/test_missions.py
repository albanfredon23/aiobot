"""Tests de bout en bout sur le world model versionné (models/world_model.npz)."""
import numpy as np
import pytest

from aiobot.controller import ControllerConfig
from aiobot.episode import run_mission
from aiobot.ledger import XAILedger
from aiobot.training import MODEL_PATH
from aiobot.world_model import WorldModel


@pytest.fixture(scope="module")
def model():
    return WorldModel.load(MODEL_PATH)


def test_mission_nominale_sans_violation_et_tracee(model):
    ledger = XAILedger()
    result = run_mission(model, "nominal", seed=0, ledger=ledger)
    assert result.completed and result.violations == 0
    assert len(ledger) == result.steps and ledger.verify().ok
    record = ledger.records()[0]
    assert record["decision"] in ("EXECUTE", "HOLD") and record["gate"]["budget"]["iterations"] == 2


def test_gate_reduit_le_calcul_par_decision(model):
    adaptive = run_mission(model, "nominal", seed=0)
    fixed = run_mission(model, "nominal", ControllerConfig(use_gate=False), seed=0)
    assert adaptive.flops_per_decision < 0.75 * fixed.flops_per_decision


def test_charge_inconnue_arret_sur_et_intervention(model):
    result = run_mission(model, "charge", seed=0)
    assert result.intervention and not result.completed
    assert result.violations == 0
    assert result.hold_reasons.get("CHI2_FREEZE", 0) > 0


def test_operateur_jamais_touche(model):
    result = run_mission(model, "operateur", seed=0)
    assert result.violations == 0
    assert result.min_operator_clearance is not None and result.min_operator_clearance > 0


def test_sans_scg_le_monde_reel_est_viole(model):
    result = run_mission(model, "nominal", ControllerConfig(use_scg=False), seed=0)
    assert result.violations > 0


def test_les_images_cles_sont_serialisables(model):
    import json

    result = run_mission(model, "operateur", seed=1, record_frames=True)
    frame = next(f for f in result.frames if f["decision"] == "EXECUTE" and f.get("plan"))
    assert len(frame["plan"]) >= 2 and frame["budget"]["n_paths"] >= 64
    json.dumps(result.frames, allow_nan=False)
    assert np.isfinite(result.mechanical_energy_j)
