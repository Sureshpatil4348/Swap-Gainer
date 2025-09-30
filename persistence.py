from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional

from automation import AppConfig, AutomationState


class Persistence:
    """Simple JSON-backed persistence for automation settings/state."""

    def __init__(self, state_path: Path, config_path: Optional[Path] = None) -> None:
        self._state_path = state_path
        self._config_path = config_path or state_path.with_name("automation_config.json")
        self._lock = threading.Lock()
        self._config = AppConfig()
        self._state = AutomationState()
        self._load()
        self._ensure_files_exist()

    def _load(self) -> None:
        combined_data: Optional[object] = None
        if self._state_path.exists():
            try:
                with self._state_path.open("r", encoding="utf-8") as fh:
                    combined_data = json.load(fh)
            except Exception:
                combined_data = None

        config_loaded = self._load_config()
        state_loaded = self._load_state(combined_data)

        if not config_loaded and isinstance(combined_data, dict):
            cfg = combined_data.get("config")
            if cfg:
                try:
                    self._config = AppConfig.from_dict(cfg)
                    config_loaded = True
                except Exception:
                    pass

        if not config_loaded:
            self._config = AppConfig()
        if not state_loaded:
            state_loaded = self._load_state()
            if not state_loaded:
                self._state = AutomationState()

    def _load_config(self) -> bool:
        if not self._config_path.exists():
            return False
        try:
            with self._config_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return False
        if isinstance(data, dict):
            if "config" in data:
                cfg_data = data.get("config")
            else:
                cfg_data = data
            try:
                self._config = AppConfig.from_dict(cfg_data)
            except Exception:
                return False
            return True
        return False

    def _load_state(self, data: Optional[object] = None) -> bool:
        if data is None:
            if not self._state_path.exists():
                return False
            try:
                with self._state_path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                return False
        payload = None
        if isinstance(data, dict) and "state" in data:
            payload = data.get("state")
        elif isinstance(data, dict):
            payload = data
        if isinstance(payload, dict):
            try:
                self._state = AutomationState.from_dict(payload)
            except Exception:
                return False
            return True
        return False

    def _ensure_files_exist(self) -> None:
        if not self._config_path.exists():
            self._write_config()
        if not self._state_path.exists():
            self._write_state()

    def _write_config(self) -> None:
        payload: Dict[str, object] = self._config.to_dict()
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._config_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp_path.replace(self._config_path)

    def _write_state(self) -> None:
        payload: Dict[str, object] = {"state": self._state.to_dict()}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp_path.replace(self._state_path)

    def get_config(self) -> AppConfig:
        with self._lock:
            return AppConfig.from_dict(self._config.to_dict())

    def save_config(self, config: AppConfig) -> None:
        with self._lock:
            self._config = config
            self._write_config()

    def get_state(self) -> AutomationState:
        with self._lock:
            return AutomationState.from_dict(self._state.to_dict())

    def save_state(self, state: AutomationState) -> None:
        with self._lock:
            self._state = state
            self._write_state()

