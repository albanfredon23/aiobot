"""Métriques publiées dans le volume partagé avec le site (lecture seule côté web).

Le moteur n'expose aucun port : il écrit ici de petits fichiers JSON que le
conteneur web sert sous /metrics/. Écriture atomique (fichier temporaire puis
renommage) pour que le site ne lise jamais un fichier à moitié écrit. Aucune
clé, aucun identifiant et aucune donnée personnelle n'y figure.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENV_DIR = "AIOBOT_METRICS_DIR"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def metrics_dir() -> Path | None:
    """Dossier des métriques (variable AIOBOT_METRICS_DIR), ou None si la publication est désactivée."""
    raw = os.environ.get(ENV_DIR, "").strip()
    return Path(raw) if raw else None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class MetricsPublisher:
    """Publie un fichier JSON de métriques ; sans dossier configuré, ne fait rien."""

    def __init__(self, name: str, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else metrics_dir()
        self.path = self.directory / f"{name}.json" if self.directory else None
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {}
        self._warned = False

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def update(self, **fields: Any) -> None:
        if self.path is None:
            return
        with self._lock:
            self._state.update(fields, updated_at=now_utc())
            try:
                write_json_atomic(self.path, self._state)
            except OSError as exc:
                # La publication des métriques ne doit jamais interrompre le moteur : un avertissement suffit.
                if not self._warned:
                    print(f"aiobot : métriques non publiées dans {self.directory} ({exc})", file=sys.stderr)
                    self._warned = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)
