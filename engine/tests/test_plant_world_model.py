import numpy as np

from aiobot.plant import ArmConfig, ArmPlant, exploration_dataset, forward_kinematics, inverse_kinematics
from aiobot.training import MODEL_PATH
from aiobot.world_model import WorldModel, train_world_model


def test_cinematique_inverse_aller_retour():
    arm = ArmConfig()
    for target, up in [((0.72, 0.02), True), ((-0.62, 0.10), False), ((0.3, 0.7), False)]:
        q = inverse_kinematics(np.array(target), arm, up)
        _, eff = forward_kinematics(q, arm)
        assert np.allclose(eff, target, atol=1e-9)
        assert (q[1] < 0) == up


def test_compensation_de_gravite_tient_la_position():
    plant = ArmPlant(payload=0.2, state=np.array([0.8, -1.2, 0.0, 0.0]))
    for _ in range(20):
        plant.step(plant.gravity_action())
    assert np.allclose(plant.state[:2], [0.8, -1.2], atol=1e-6)


def test_charge_inconnue_change_la_dynamique():
    start = np.array([0.5, -0.5, 0.0, 0.0])
    a, b = ArmPlant(payload=0.15, state=start.copy()), ArmPlant(payload=1.15, state=start.copy())
    action = a.gravity_action()
    for _ in range(5):
        a.step(action), b.step(action)
    assert b.state[0] < a.state[0] - 0.05  # la charge lourde fait chuter l'épaule


def test_apprentissage_et_sauvegarde(tmp_path):
    rng = np.random.default_rng(0)
    s, a, n = exploration_dataset(6000, rng)
    model, report = train_world_model(s, a, n, rng, n_members=2, hidden=32, epochs=15)
    assert min(report.val_r2) > 0.8
    path = tmp_path / "wm.npz"
    model.calibration = {"threshold": np.array(15.0)}
    model.save(path)
    loaded = WorldModel.load(path)
    assert np.allclose(loaded.predict(s[:10], a[:10]), model.predict(s[:10], a[:10]))
    assert float(loaded.calibration["threshold"]) == 15.0


def test_modele_versionne_precis_sur_plusieurs_pas():
    model = WorldModel.load(MODEL_PATH)
    rng = np.random.default_rng(3)
    plant = ArmPlant(payload=0.15, state=np.array([1.0, -1.0, 0.0, 0.0]))
    state = plant.state.copy()
    action = plant.gravity_action()
    for _ in range(8):  # 0,4 s : l'horizon du contrôle de robustesse
        action = np.clip(0.8 * action + 0.2 * plant.gravity_action() + rng.normal(0, 0.2, 2), -1, 1)
        plant.step(action)
        state = model.predict(state, action)
    _, eff_true = forward_kinematics(plant.state[:2], ArmConfig())
    _, eff_pred = forward_kinematics(state[:2], ArmConfig())
    assert np.linalg.norm(eff_true - eff_pred) < 0.03
    assert model.n_members == 3 and "threshold" in model.calibration
