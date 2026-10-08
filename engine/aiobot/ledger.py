"""XAI Ledger : registre d'audit immuable, un enregistrement JSON par décision.

Chaque enregistrement contient l'état du filtre d'intégrité χ², le budget de
calcul choisi par le gate, le faisceau TAP, les rejets du SCG par contrainte,
la robustesse du plan retenu et l'action envoyée aux actionneurs.
Le registre est en ajout seul (JSON Lines) et chaîné : chaque ligne porte
l'empreinte SHA-256 de la ligne précédente (``prev_hash``) et la sienne
(``hash``), calculée sur le JSON canonique de l'enregistrement. Modifier,
supprimer ou réordonner une ligne casse la chaîne et ``verify`` le signale.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

GENESIS = "0" * 64
_CHAIN_KEYS = ("seq", "prev_hash", "hash")


def canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def record_hash(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    head_hash: str
    first_invalid_seq: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "records": self.records,
            "head_hash": self.head_hash,
            "first_invalid_seq": self.first_invalid_seq,
            "error": self.error,
        }


def verify_chain(records: Iterable[dict[str, Any]]) -> VerifyResult:
    prev = GENESIS
    count = 0
    for expected_seq, rec in enumerate(records):
        count += 1
        if rec.get("seq") != expected_seq:
            return VerifyResult(False, count, prev, expected_seq, "numéro de séquence inattendu")
        if rec.get("prev_hash") != prev:
            return VerifyResult(False, count, prev, expected_seq, "prev_hash ne correspond pas à la ligne précédente")
        if rec.get("hash") != record_hash(rec):
            return VerifyResult(False, count, prev, expected_seq, "empreinte invalide : enregistrement modifié")
        prev = rec["hash"]
    return VerifyResult(True, count, prev)


class XAILedger:
    """Registre chaîné en mémoire, éventuellement doublé d'un fichier JSONL en ajout seul."""

    def __init__(self, path: str | Path | None = None, keep_in_memory: int | None = None) -> None:
        self.path = Path(path) if path else None
        self.keep_in_memory = keep_in_memory
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._head = GENESIS
        if self.path and self.path.exists():
            existing = self.read_jsonl(self.path)
            result = verify_chain(existing)
            if not result.ok:
                raise ValueError(f"registre existant corrompu (séquence {result.first_invalid_seq}) : {result.error}")
            self._seq, self._head = result.records, result.head_hash
            self._records = existing[-keep_in_memory:] if keep_in_memory else existing

    @property
    def head_hash(self) -> str:
        return self._head

    def __len__(self) -> int:
        return self._seq

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        if any(k in record for k in _CHAIN_KEYS):
            raise ValueError("seq, prev_hash et hash sont réservés au registre")
        with self._lock:
            entry = dict(record)
            entry["seq"] = self._seq
            entry["prev_hash"] = self._head
            entry["hash"] = record_hash(entry)
            line = canonical(entry)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
                try:
                    os.write(fd, line + b"\n")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._records.append(entry)
            if self.keep_in_memory and len(self._records) > self.keep_in_memory:
                del self._records[: len(self._records) - self.keep_in_memory]
            self._seq += 1
            self._head = entry["hash"]
            return entry

    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def export_jsonl(self) -> str:
        return "".join(canonical(r).decode("utf-8") + "\n" for r in self._records)

    def verify(self) -> VerifyResult:
        if self.path and self.path.exists():
            return verify_chain(self.read_jsonl(self.path))
        if self.keep_in_memory and self._seq > len(self._records):
            raise ValueError("registre tronqué en mémoire : vérifiez le fichier JSONL")
        return verify_chain(self._records)

    @staticmethod
    def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    @staticmethod
    def parse_jsonl(text: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
