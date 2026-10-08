"""API HTTP d'AIOBot (FastAPI). Simulation uniquement : aucun robot réel n'est commandé."""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import __version__
from .benchmark import ARM_LABELS, ARMS, REPORT_PATH
from .controller import ControllerConfig
from .episode import FIXTURE, MISSION, OPERATOR, SCENARIO_LABELS, SCENARIOS, STATIONS, run_mission
from .gate import GateConfig
from .integrity import IntegrityConfig
from .ledger import XAILedger, verify_chain
from .metrics import MetricsPublisher, now_utc, write_json_atomic
from .plant import ArmConfig
from .scg import SafetyEnvelope
from .training import MODEL_PATH, TRAINING_REPORT
from .world_model import WorldModel

_metrics = MetricsPublisher("engine", directory=None)


@lru_cache(maxsize=1)
def _model() -> WorldModel:
    if not MODEL_PATH.exists():
        raise HTTPException(status_code=503, detail="world model absent : python -m aiobot train")
    return WorldModel.load(MODEL_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Au démarrage : charge le world model et publie l'état du moteur dans le volume de métriques."""
    global _metrics
    _metrics = MetricsPublisher("engine")
    if MODEL_PATH.exists():
        _model()
    if _metrics.enabled:
        if REPORT_PATH.exists() and _metrics.directory is not None:
            try:
                write_json_atomic(_metrics.directory / "benchmark.json", json.loads(REPORT_PATH.read_text(encoding="utf-8")))
            except OSError:
                pass
        _metrics.update(service="aiobot-engine", version=__version__, status="ok", mode="simulation",
                        started_at=now_utc(), missions_served=0, last_mission=None)
    yield
    if _metrics.enabled:
        _metrics.update(status="stopped")


app = FastAPI(
    title="AIOBot API",
    version=__version__,
    description=(
        "Pilote industriel fondé sur un world model appris : filtre d'intégrité χ², gate de complexité, "
        "TAP, SCG, contrôle de robustesse et XAI Ledger. Simulation uniquement."
    ),
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def _rate_limit(request: Request, bucket: str, limit: int, window_s: float) -> None:
    client = request.client.host if request.client else "inconnu"
    now = time.monotonic()
    hits = _hits[(bucket, client)]
    while hits and now - hits[0] > window_s:
        hits.popleft()
    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="Trop de demandes, réessayez plus tard.")
    hits.append(now)


class MissionIn(BaseModel):
    scenario: str = Field("nominal", pattern="^(" + "|".join(SCENARIOS) + ")$")
    arm: str = Field("aiobot", pattern="^(" + "|".join(ARMS) + ")$")
    seed: int = Field(0, ge=0, le=100_000)
    frames: bool = True


class LedgerIn(BaseModel):
    jsonl: str = Field(..., max_length=5_000_000)


@lru_cache(maxsize=32)
def _mission(scenario: str, arm: str, seed: int) -> dict[str, Any]:
    ledger = XAILedger()
    result = run_mission(_model(), scenario, ARMS[arm], arm_name=arm, seed=seed, ledger=ledger, record_frames=True)
    records = ledger.records()
    verify = ledger.verify()
    holds = [r for r in records if r["decision"] == "HOLD"]
    return {
        "summary": result.summary(),
        "frames": result.frames,
        "ledger": {
            "records": len(ledger),
            "head_hash": ledger.head_hash,
            "verified": verify.ok,
            "sample": records[:1] + holds[:2] + records[-1:],
        },
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "mode": "simulation", "model_loaded": MODEL_PATH.exists()}


@app.get("/api/config")
def config() -> dict:
    return {
        "arm": asdict(ArmConfig()),
        "envelope": asdict(SafetyEnvelope()),
        "gate": asdict(GateConfig()),
        "integrity": asdict(IntegrityConfig()),
        "controller": {k: v for k, v in asdict(ControllerConfig()).items() if k != "fixed_budget"},
        "stations": {k: {"xy": list(xy), "elbow_up": up} for k, (xy, up) in STATIONS.items()},
        "mission": list(MISSION),
        "fixture": FIXTURE.to_dict(),
        "operator": OPERATOR.to_dict(),
        "scenarios": SCENARIO_LABELS,
        "arms": ARM_LABELS,
    }


@app.get("/api/model")
def model_info() -> dict:
    if not TRAINING_REPORT.exists():
        raise HTTPException(status_code=404, detail="rapport d'apprentissage absent : python -m aiobot train")
    return json.loads(TRAINING_REPORT.read_text(encoding="utf-8"))


@app.post("/api/mission")
def mission(body: MissionIn, request: Request) -> dict:
    """Joue une mission simulée et renvoie son bilan, ses images clés et un extrait du registre XAI."""
    _rate_limit(request, "mission", 20, 60)
    started = time.perf_counter()
    result = _mission(body.scenario, body.arm, body.seed)
    if _metrics.enabled:
        served = int(_metrics.snapshot().get("missions_served") or 0) + 1
        _metrics.update(missions_served=served, last_mission={
            "at": now_utc(), "scenario": body.scenario, "arm": body.arm, "seed": body.seed,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "completed": result["summary"]["completed"], "violations": result["summary"]["violations"],
            "ledger_verified": result["ledger"]["verified"],
        })
    return result if body.frames else {k: v for k, v in result.items() if k != "frames"}


@app.get("/api/benchmark")
def benchmark() -> dict:
    if not REPORT_PATH.exists():
        raise HTTPException(status_code=404, detail="rapport de benchmark absent : python -m aiobot benchmark")
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


@app.post("/api/ledger/verify")
def ledger_verify(body: LedgerIn, request: Request) -> dict:
    """Vérifie la chaîne d'empreintes SHA-256 d'un registre XAI (JSON Lines)."""
    _rate_limit(request, "ledger", 30, 60)
    try:
        records = [json.loads(line) for line in body.jsonl.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"JSON invalide ligne {exc.lineno}") from exc
    if not all(isinstance(r, dict) for r in records):
        raise HTTPException(status_code=422, detail="chaque ligne doit être un objet JSON")
    return verify_chain(records).to_dict()
