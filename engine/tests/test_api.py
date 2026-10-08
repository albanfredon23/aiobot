import json

from fastapi.testclient import TestClient

from aiobot.api import app
from aiobot.ledger import XAILedger

client = TestClient(app)


def test_sante_et_configuration():
    assert client.get("/api/health").json()["status"] == "ok"
    cfg = client.get("/api/config").json()
    assert cfg["mission"] == ["prise", "passage", "depose", "parking"]
    assert "charge" in cfg["scenarios"] and cfg["envelope"]["min_robustness"] == 0.75


def test_modele():
    info = client.get("/api/model").json()
    assert info["members"] == 3 and min(info["training"]["val_r2"]) > 0.99


def test_mission_simulee():
    body = client.post("/api/mission", json={"scenario": "nominal", "seed": 0, "frames": False}).json()
    assert body["summary"]["completed"] and body["ledger"]["verified"]
    assert "frames" not in body


def test_mission_parametres_invalides():
    assert client.post("/api/mission", json={"scenario": "inconnu"}).status_code == 422


def test_verification_du_registre():
    ledger = XAILedger()
    for i in range(3):
        ledger.append({"step": i, "decision": "EXECUTE"})
    text = ledger.export_jsonl()
    assert client.post("/api/ledger/verify", json={"jsonl": text}).json()["ok"]
    lines = text.splitlines()
    rec = json.loads(lines[1])
    rec["decision"] = "HOLD"
    lines[1] = json.dumps(rec)
    tampered = client.post("/api/ledger/verify", json={"jsonl": "\n".join(lines)}).json()
    assert not tampered["ok"] and tampered["first_invalid_seq"] == 1
