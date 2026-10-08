"""World model appris : un ensemble de réseaux qui prédit l'état suivant du robot.

Chaque membre est un perceptron multicouche (2 couches cachées, tanh) qui
prédit la variation d'état Δs = s(t+1) − s(t) à partir de

    x = [sin q1, cos q1, sin q2, cos q2, q̇1, q̇2, a1, a2].

Entrées et sorties sont normalisées (moyenne, écart type de l'apprentissage).
L'apprentissage se fait en NumPy pur (rétropropagation écrite à la main,
optimiseur Adam), sans framework : le moteur reste léger et auditable.

Plusieurs membres entraînés sur des rééchantillonnages différents (bootstrap)
donnent une mesure d'incertitude épistémique : quand ils divergent, le modèle
ne sait pas, et le SCG refuse de s'y fier.

Le coût de calcul est compté exactement : ``flops_per_sample`` additionne
les multiplications-additions des couches (2 FLOP par MAC) et les tanh.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .plant import ACTION_DIM, STATE_DIM

INPUT_DIM = 8


def features(states: np.ndarray, actions: np.ndarray) -> np.ndarray:
    q1, q2 = states[..., 0], states[..., 1]
    return np.concatenate(
        [
            np.stack([np.sin(q1), np.cos(q1), np.sin(q2), np.cos(q2)], axis=-1),
            states[..., 2:4],
            actions,
        ],
        axis=-1,
    )


@dataclass
class MLP:
    weights: list[np.ndarray]
    biases: list[np.ndarray]

    @classmethod
    def init(cls, sizes: list[int], rng: np.random.Generator) -> "MLP":
        weights = [rng.normal(0.0, np.sqrt(1.0 / n_in), size=(n_in, n_out)) for n_in, n_out in zip(sizes, sizes[1:])]
        return cls(weights, [np.zeros(n_out) for n_out in sizes[1:]])

    def forward(self, x: np.ndarray) -> np.ndarray:
        h = x
        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            h = h @ w + b
            if i < len(self.weights) - 1:
                h = np.tanh(h)
        return h

    def forward_cache(self, x: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
        acts = [x]
        h = x
        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            h = h @ w + b
            if i < len(self.weights) - 1:
                h = np.tanh(h)
            acts.append(h)
        return h, acts

    def gradients(self, acts: list[np.ndarray], grad_out: np.ndarray) -> tuple[list[np.ndarray], list[np.ndarray]]:
        gw, gb = [], []
        g = grad_out
        for i in range(len(self.weights) - 1, -1, -1):
            gw.append(acts[i].T @ g)
            gb.append(g.sum(axis=0))
            if i > 0:
                g = (g @ self.weights[i].T) * (1.0 - acts[i] ** 2)
        return gw[::-1], gb[::-1]

    def flops_per_sample(self) -> int:
        macs = sum(w.shape[0] * w.shape[1] for w in self.weights)
        tanh = sum(w.shape[1] for w in self.weights[:-1])
        return 2 * macs + sum(b.size for b in self.biases) + tanh


@dataclass
class TrainReport:
    epochs: int
    train_mse: float
    val_mse: float
    val_r2: list[float]
    residual_cov: list[list[float]]
    n_train: int
    n_val: int

    def to_dict(self) -> dict:
        return {
            "epochs": self.epochs,
            "train_mse_normalised": round(self.train_mse, 6),
            "val_mse_normalised": round(self.val_mse, 6),
            "val_r2": [round(v, 4) for v in self.val_r2],
            "n_train": self.n_train,
            "n_val": self.n_val,
        }


@dataclass
class WorldModel:
    members: list[MLP]
    x_mean: np.ndarray
    x_std: np.ndarray
    y_mean: np.ndarray
    y_std: np.ndarray
    residual_cov: np.ndarray  # covariance des résidus de validation (pas d'un pas), en unités d'état
    calibration: dict[str, np.ndarray] = field(default_factory=dict)  # étalonnage du filtre χ² en boucle fermée

    @property
    def n_members(self) -> int:
        return len(self.members)

    @property
    def flops_per_sample(self) -> int:
        """FLOP d'une prédiction d'un pas par un membre (normalisation et dénormalisation comprises)."""
        return self.members[0].flops_per_sample() + 2 * INPUT_DIM + 2 * STATE_DIM + 8 * 4  # + sin/cos

    def predict(self, states: np.ndarray, actions: np.ndarray, member: int | None = None) -> np.ndarray:
        """État suivant (…, 4). ``member`` = None : moyenne de l'ensemble ; sinon un membre précis."""
        x = (features(states, actions) - self.x_mean) / self.x_std
        shape = x.shape[:-1]
        x = x.reshape(-1, INPUT_DIM)
        if member is None:
            dy = np.mean([m.forward(x) for m in self.members], axis=0)
        else:
            dy = self.members[member].forward(x)
        delta = dy * self.y_std + self.y_mean
        return states + delta.reshape(*shape, STATE_DIM)

    def predict_members(self, states: np.ndarray, actions: np.ndarray) -> np.ndarray:
        """Prédictions de chaque membre (M, …, 4), pour mesurer leur désaccord."""
        return np.stack([self.predict(states, actions, member=i) for i in range(self.n_members)])

    # ------------------------------------------------------------ persistance
    def save(self, path: str | Path) -> None:
        arrays: dict[str, np.ndarray] = {
            "x_mean": self.x_mean, "x_std": self.x_std, "y_mean": self.y_mean, "y_std": self.y_std,
            "residual_cov": self.residual_cov, "n_members": np.array(self.n_members),
            "n_layers": np.array(len(self.members[0].weights)),
        }
        for key, value in self.calibration.items():
            arrays[f"cal_{key}"] = np.asarray(value)
        for i, m in enumerate(self.members):
            for j, (w, b) in enumerate(zip(m.weights, m.biases)):
                arrays[f"m{i}_w{j}"] = w
                arrays[f"m{i}_b{j}"] = b
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, **arrays)

    @classmethod
    def load(cls, path: str | Path) -> "WorldModel":
        with np.load(path) as data:
            n_members, n_layers = int(data["n_members"]), int(data["n_layers"])
            members = [
                MLP([data[f"m{i}_w{j}"] for j in range(n_layers)], [data[f"m{i}_b{j}"] for j in range(n_layers)])
                for i in range(n_members)
            ]
            calibration = {k[4:]: np.array(data[k]) for k in data.files if k.startswith("cal_")}
            return cls(members, data["x_mean"], data["x_std"], data["y_mean"], data["y_std"], data["residual_cov"], calibration)


def train_world_model(
    states: np.ndarray,
    actions: np.ndarray,
    next_states: np.ndarray,
    rng: np.random.Generator,
    n_members: int = 3,
    hidden: int = 64,
    epochs: int = 40,
    batch_size: int = 256,
    lr: float = 2e-3,
    val_fraction: float = 0.15,
) -> tuple[WorldModel, TrainReport]:
    if not (states.shape[0] == actions.shape[0] == next_states.shape[0]):
        raise ValueError("états, actions et états suivants doivent avoir le même nombre de lignes")
    if actions.shape[-1] != ACTION_DIM or states.shape[-1] != STATE_DIM:
        raise ValueError("dimensions d'état ou d'action inattendues")
    n = states.shape[0]
    order = rng.permutation(n)
    n_val = int(n * val_fraction)
    val_idx, tr_idx = order[:n_val], order[n_val:]

    x_all = features(states, actions)
    y_all = next_states - states
    x_mean, x_std = x_all[tr_idx].mean(axis=0), x_all[tr_idx].std(axis=0) + 1e-8
    y_mean, y_std = y_all[tr_idx].mean(axis=0), y_all[tr_idx].std(axis=0) + 1e-8
    xn, yn = (x_all - x_mean) / x_std, (y_all - y_mean) / y_std

    members: list[MLP] = []
    train_mse = 0.0
    for _ in range(n_members):
        boot = rng.choice(tr_idx, size=tr_idx.size, replace=True)
        mlp = MLP.init([INPUT_DIM, hidden, hidden, STATE_DIM], rng)
        params = mlp.weights + mlp.biases
        m1 = [np.zeros_like(p) for p in params]
        m2 = [np.zeros_like(p) for p in params]
        step = 0
        for epoch in range(epochs):
            rate = lr * (0.5 * (1 + np.cos(np.pi * epoch / epochs)))  # décroissance cosinus
            perm = rng.permutation(boot)
            for k in range(0, perm.size, batch_size):
                idx = perm[k : k + batch_size]
                out, acts = mlp.forward_cache(xn[idx])
                grad = 2.0 * (out - yn[idx]) / idx.size
                gw, gb = mlp.gradients(acts, grad)
                step += 1
                for p, g, a, b in zip(params, gw + gb, m1, m2):
                    a *= 0.9
                    a += 0.1 * g
                    b *= 0.999
                    b += 0.001 * g * g
                    p -= rate * (a / (1 - 0.9**step)) / (np.sqrt(b / (1 - 0.999**step)) + 1e-8)
        train_mse += float(np.mean((mlp.forward(xn[tr_idx]) - yn[tr_idx]) ** 2)) / n_members
        members.append(mlp)

    model = WorldModel(members, x_mean, x_std, y_mean, y_std, np.eye(STATE_DIM))
    pred = model.predict(states[val_idx], actions[val_idx])
    resid = next_states[val_idx] - pred
    model.residual_cov = np.cov(resid.T) + 1e-12 * np.eye(STATE_DIM)
    val_mse = float(np.mean(((pred - states[val_idx] - y_mean) / y_std - yn[val_idx]) ** 2))
    var_y = np.var(y_all[val_idx], axis=0)
    r2 = (1.0 - np.mean(resid**2, axis=0) / var_y).tolist()
    report = TrainReport(epochs, train_mse, val_mse, r2, model.residual_cov.tolist(), int(tr_idx.size), int(n_val))
    return model, report
