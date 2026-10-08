"""Ligne de commande d'AIOBot.

    python -m aiobot train                    apprend le world model et étalonne le filtre χ²
    python -m aiobot mission --scenario charge  joue une mission et affiche son bilan
    python -m aiobot benchmark                benchmark à 4 bras sur 5 scénarios
    python -m aiobot verify registre.jsonl    vérifie la chaîne d'un registre XAI
"""
from __future__ import annotations

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")  # avant NumPy : un cœur par processus du benchmark

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402

from .benchmark import REPORT_PATH, BenchmarkConfig, run_benchmark, save_report  # noqa: E402
from .episode import SCENARIOS, run_mission  # noqa: E402
from .ledger import XAILedger, verify_chain  # noqa: E402
from .training import MODEL_PATH, TRAINING_REPORT, TrainConfig, build_world_model  # noqa: E402
from .world_model import WorldModel  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiobot", description="Pilote industriel fondé sur un world model appris")
    sub = parser.add_subparsers(dest="command", required=True)

    p_train = sub.add_parser("train", help="apprendre le world model et étalonner le filtre χ²")
    p_train.add_argument("--seed", type=int, default=TrainConfig.seed)
    p_train.add_argument("--transitions", type=int, default=TrainConfig.n_transitions)
    p_train.add_argument("--epochs", type=int, default=TrainConfig.epochs)
    p_train.add_argument("--out", default=str(MODEL_PATH))

    p_mission = sub.add_parser("mission", help="jouer une mission")
    p_mission.add_argument("--scenario", choices=SCENARIOS, default="nominal")
    p_mission.add_argument("--seed", type=int, default=0)
    p_mission.add_argument("--model", default=str(MODEL_PATH))
    p_mission.add_argument("--ledger", help="fichier JSONL du registre XAI (ajout seul)")

    p_bench = sub.add_parser("benchmark", help="benchmark à 4 bras")
    p_bench.add_argument("--seeds", type=int, default=BenchmarkConfig.seeds)
    p_bench.add_argument("--workers", type=int, default=0)
    p_bench.add_argument("--model", default=str(MODEL_PATH))
    p_bench.add_argument("--out", default=str(REPORT_PATH))

    p_verify = sub.add_parser("verify", help="vérifier un registre XAI")
    p_verify.add_argument("path")

    args = parser.parse_args(argv)
    if args.command == "train":
        model, summary = build_world_model(TrainConfig(seed=args.seed, n_transitions=args.transitions, epochs=args.epochs))
        model.save(args.out)
        TRAINING_REPORT.parent.mkdir(parents=True, exist_ok=True)
        TRAINING_REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    if args.command == "mission":
        model = WorldModel.load(args.model)
        ledger = XAILedger(args.ledger) if args.ledger else None
        result = run_mission(model, args.scenario, seed=args.seed, ledger=ledger)
        print(json.dumps(result.summary(), ensure_ascii=False, indent=1))
        return 0
    if args.command == "benchmark":
        report = run_benchmark(args.model, BenchmarkConfig(seeds=args.seeds, workers=args.workers))
        save_report(report, args.out)
        print(json.dumps(report["headline"], ensure_ascii=False, indent=1))
        return 0
    if args.command == "verify":
        result = verify_chain(XAILedger.read_jsonl(args.path))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1))
        return 0 if result.ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
