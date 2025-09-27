from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict

from automation import AppConfig, AutomationState


class Persistence:
    """Simple JSON-backed persistence for automation settings/state."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._config = AppConfig()
        self._state = AutomationState()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        cfg = data.get("config") if isinstance(data, dict) else None
        st = data.get("state") if isinstance(data, dict) else None
        if cfg:
            self._config = AppConfig.from_dict(cfg)
        if st:
            self._state = AutomationState.from_dict(st)

    def _write(self) -> None:
        payload: Dict[str, object] = {
            "config": self._config.to_dict(),
            "state": self._state.to_dict(),
        }
        tmp_path = self._path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp_path.replace(self._path)

    def get_config(self) -> AppConfig:
        with self._lock:
            return AppConfig.from_dict(self._config.to_dict())

    def save_config(self, config: AppConfig) -> None:
        with self._lock:
            self._config = config
            self._write()

    def get_state(self) -> AutomationState:
        with self._lock:
            return AutomationState.from_dict(self._state.to_dict())

    def save_state(self, state: AutomationState) -> None:
        with self._lock:
            self._state = state
            self._write()

