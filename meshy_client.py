from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests


class MeshyError(RuntimeError):
    pass


class MeshyClient:
    BASE_URL = "https://api.meshy.ai/openapi/v1"

    def __init__(self, api_key: str, logs_dir: Path, timeout_seconds: int = 60):
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    @staticmethod
    def _safe_response(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"status_code": response.status_code, "message": response.text[:1000]}
        if not response.ok:
            message = payload.get("message") or payload.get("task_error") or payload
            raise MeshyError(f"Meshy API-Fehler {response.status_code}: {message}")
        return payload

    @staticmethod
    def write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def redact_urls(payload: Any) -> Any:
        if isinstance(payload, dict):
            return {
                key: MeshyClient.redact_urls(value)
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return [MeshyClient.redact_urls(value) for value in payload]
        if isinstance(payload, str) and payload.lower().startswith(("http://", "https://")):
            return "[SIGNED_URL_REDACTED]"
        return payload

    @staticmethod
    def _assert_network_allowed() -> None:
        if os.environ.get("MESHY_NETWORK_BLOCKED") == "1":
            raise MeshyError(
                "SICHERHEITSSPERRE: Netzwerkzugriffe zu Meshy sind in diesem Modus blockiert."
            )

    def create_task(self, endpoint: str, payload: dict[str, Any], log_name: str) -> str:
        normalized_endpoint = endpoint.strip("/").casefold()
        if normalized_endpoint == "rigging" and os.environ.get("MESHY_RIGGING_BLOCKED") == "1":
            raise MeshyError(
                "MESHY RIGGING BLOCKED\n"
                "Dieser Job verwendet später das Learning-Kit-Zielskelett.\n"
                "Es wurde kein Rigging-Task erstellt."
            )
        self._assert_network_allowed()
        if os.environ.get("MESHY_POSTS_BLOCKED") == "1":
            raise MeshyError(
                "SICHERHEITSSPERRE: Meshy-POST-Aufrufe sind im lokalen "
                "Fortsetzungsmodus blockiert."
            )
        response = self.session.post(
            f"{self.BASE_URL}/{endpoint}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        data = self._safe_response(response)
        self.write_json(self.logs_dir / log_name, data)
        task_id = data.get("result")
        if not isinstance(task_id, str) or not task_id:
            raise MeshyError("Meshy hat keine gültige Task-ID zurückgegeben.")
        return task_id

    def get_task(self, endpoint: str, task_id: str) -> dict[str, Any]:
        self._assert_network_allowed()
        response = self.session.get(
            f"{self.BASE_URL}/{endpoint}/{task_id}",
            timeout=self.timeout_seconds,
        )
        return self._safe_response(response)

    def get_balance(self, log_name: str) -> dict[str, Any]:
        self._assert_network_allowed()
        response = self.session.get(
            f"{self.BASE_URL}/balance",
            timeout=self.timeout_seconds,
        )
        data = self._safe_response(response)
        self.write_json(self.logs_dir / log_name, data)
        return data

    def poll_task(
        self,
        endpoint: str,
        task_id: str,
        timeout_minutes: int,
        result_path: Path,
        poll_seconds: int = 10,
        poll_log_path: Path | None = None,
        redact_urls_in_result: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_minutes * 60
        if poll_log_path is not None:
            poll_log_path.parent.mkdir(parents=True, exist_ok=True)
        while time.monotonic() < deadline:
            data = self.get_task(endpoint, task_id)
            stored_data = self.redact_urls(data) if redact_urls_in_result else data
            self.write_json(result_path, stored_data)
            status = str(data.get("status", "")).upper()
            progress = data.get("progress", 0)
            line = f"Meshy {endpoint}: {status or 'UNBEKANNT'} ({progress} %)"
            print(line)
            if poll_log_path is not None:
                with poll_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            if status == "SUCCEEDED":
                return data
            if status in {"FAILED", "EXPIRED", "CANCELED"}:
                error = data.get("task_error") or "keine Details"
                raise MeshyError(f"Meshy-Task {task_id} endete mit {status}: {error}")
            time.sleep(poll_seconds)
        raise MeshyError(
            f"Timeout: Meshy-Task {task_id} war nach {timeout_minutes} Minuten nicht fertig."
        )

    def download(self, url: str, destination: Path) -> None:
        self._assert_network_allowed()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=180) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise MeshyError(f"Download ist leer: {destination}")
