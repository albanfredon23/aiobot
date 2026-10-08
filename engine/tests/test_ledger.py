import json

import pytest

from aiobot.ledger import GENESIS, XAILedger, record_hash, verify_chain

SAMPLE = {
    "step": 42,
    "decision": "EXECUTE",
    "integrity": {"d2": 4.12, "threshold": 14.89, "status": "NOMINAL"},
    "gate": {"complexity": 0.31, "budget": {"n_paths": 124, "horizon": 11, "iterations": 2}},
    "scg": {"n_trajectories": 124, "admissible": 97, "rejections": {"obstacle_sphere": 20, "speed_limit": 7}},
    "robustness": 0.9583,
    "command": [0.35, -0.12],
    "constraints": ["FLOOR_Y_-0.12M", "MIN_ROBUSTNESS_75%"],
}


def filled(n=5):
    ledger = XAILedger()
    for i in range(n):
        ledger.append({**SAMPLE, "robustness": 0.1 * i})
    return ledger


def test_chainage_sha256():
    ledger = filled()
    recs = ledger.records()
    assert recs[0]["prev_hash"] == GENESIS
    assert all(recs[i]["prev_hash"] == recs[i - 1]["hash"] for i in range(1, len(recs)))
    assert all(r["hash"] == record_hash(r) for r in recs)
    assert ledger.verify().ok and ledger.head_hash == recs[-1]["hash"]


@pytest.mark.parametrize("tamper", ["modify", "delete", "swap"])
def test_falsification_detectee(tamper):
    recs = filled().records()
    if tamper == "modify":
        recs[2]["robustness"] = 0.9
    elif tamper == "delete":
        del recs[2]
    else:
        recs[1], recs[2] = recs[2], recs[1]
    result = verify_chain(recs)
    assert not result.ok
    assert result.first_invalid_seq is not None


def test_cles_reservees():
    with pytest.raises(ValueError):
        XAILedger().append({**SAMPLE, "hash": "x"})


def test_fichier_jsonl_ajout_seul(tmp_path):
    path = tmp_path / "audit" / "decisions.jsonl"
    a = XAILedger(path)
    a.append(SAMPLE)
    a.append(SAMPLE)
    b = XAILedger(path)  # reprise : la chaîne continue
    rec = b.append(SAMPLE)
    assert rec["seq"] == 2 and len(b) == 3
    lines = path.read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["decision"] == "EXECUTE"
    assert b.verify().ok
    assert XAILedger.parse_jsonl(b.export_jsonl())[-1]["hash"] == rec["hash"]


def test_fichier_corrompu_refuse(tmp_path):
    path = tmp_path / "decisions.jsonl"
    ledger = XAILedger(path)
    ledger.append(SAMPLE)
    ledger.append(SAMPLE)
    lines = path.read_text().splitlines()
    path.write_text(lines[1] + "\n" + lines[0] + "\n")
    with pytest.raises(ValueError):
        XAILedger(path)


def test_format_conserve():
    rec = filled(1).records()[0]
    for key in SAMPLE:
        assert rec[key] == SAMPLE[key] or key == "robustness"
