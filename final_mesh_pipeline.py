from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps


PIPELINE_DIR = Path(__file__).resolve().parent
WORKSPACE_NAME = "IsekaiAscension_Workspace"
WORKSPACE_ROOT = Path(
    os.environ.get(
        "CHARACTER_PIPELINE_WORKSPACE",
        str(Path.home() / "Desktop" / WORKSPACE_NAME),
    )
).expanduser().resolve()
PIPELINE_VERSION = "geometry-remesh-retexture-v1"
REFERENCE_NAMES = (
    "character_front.png",
    "character_left.png",
    "character_back.png",
    "character_three_quarter.png",
)
PHASE_CREDIT_BUDGETS = {"geometry": 20.0, "remesh": 5.0, "retexture": 10.0}
APPROVED_BUILD_CAP = sum(PHASE_CREDIT_BUDGETS.values())
MESHY_BASE_URL = "https://api.meshy.ai/openapi/v1"
POLL_SECONDS = 10
TASK_TIMEOUT_SECONDS = 30 * 60

DEFAULT_CONFIG: dict[str, Any] = {
    "final_mesh_profile": "direct-character-35-v1",
    "pipeline_version": PIPELINE_VERSION,
    "direct_full_build": True,
    "stop_after_remesh": False,
    "require_manual_remesh_approval": False,
    "automatic_continue_to_retexture": True,
    "allow_meshy_api_calls": False,
    "approved_credit_cap": 0,
    "planned_credit_costs": {
        "geometry": 20,
        "remesh": 5,
        "retexture": 10,
        "total": 35,
    },
    "geometry_generation": {
        "enabled": True,
        "provider": "meshy",
        "endpoint": "multi-image-to-3d",
        "ai_model": "latest",
        "pose_mode": "a-pose",
        "should_texture": False,
        "should_remesh": False,
        "image_enhancement": False,
        "target_formats": ["glb"],
        "expected_credits": 20,
    },
    "final_remesh": {
        "enabled": True,
        "topology": "quad",
        "target_polycount": 60000,
        "target_formats": ["glb"],
        "expected_credits": 5,
    },
    "final_retexture": {
        "enabled": True,
        "ai_model": "latest",
        "enable_original_uv": False,
        "enable_pbr": True,
        "texture_resolution": "4k",
        "remove_lighting": True,
        "target_formats": ["glb", "fbx"],
        "expected_credits": 10,
    },
    "automatic_blender_baking": False,
    "automatic_sculpting": False,
    "automatic_rigging": False,
    "automatic_unreal_import": False,
}


class PipelineError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def as_data_uri(path: Path, mime: str | None = None) -> str:
    media_type = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def nested_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def effective_config(job: dict[str, Any]) -> dict[str, Any]:
    result = nested_copy(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in job:
            result[key] = nested_copy(job[key])
    return result


def validate_exact_config(job: dict[str, Any]) -> None:
    errors: list[str] = []
    for key, expected in DEFAULT_CONFIG.items():
        if key in {"allow_meshy_api_calls", "approved_credit_cap"}:
            continue
        if job.get(key) != expected:
            errors.append(f"{key}: erwartet {expected!r}, eingetragen {job.get(key)!r}")
    if errors:
        raise PipelineError(
            "Build-Konfiguration ist unvollständig oder unsicher:\n- " + "\n- ".join(errors)
        )


def resolve_job(args: argparse.Namespace) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    workspace_path = WORKSPACE_ROOT / "00_START_HERE" / "workspace.json"
    if not workspace_path.is_file():
        raise PipelineError(f"workspace.json fehlt: {workspace_path}")
    workspace = load_json(workspace_path)
    if Path(str(workspace.get("workspace_root", ""))).resolve() != WORKSPACE_ROOT.resolve():
        raise PipelineError("workspace_root stimmt nicht mit dem freigegebenen Workspace überein.")
    if Path(str(workspace.get("character_pipeline_dir", ""))).resolve() != PIPELINE_DIR.resolve():
        raise PipelineError("character_pipeline_dir stimmt nicht mit dieser Pipeline überein.")
    if workspace.get("automatic_unreal_import") is not False:
        raise PipelineError("automatic_unreal_import muss im Workspace false sein.")

    jobs_root = Path(str(workspace["jobs_root"])).resolve()
    if args.latest_job:
        latest = WORKSPACE_ROOT / "00_START_HERE" / "LATEST_JOB.txt"
        lines = [line.strip() for line in latest.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if len(lines) != 1:
            raise PipelineError(f"LATEST_JOB.txt muss genau einen Jobpfad enthalten: {latest}")
        job_dir = Path(lines[0]).resolve()
    else:
        job_dir = Path(args.job).resolve()
    try:
        job_dir.relative_to(jobs_root)
    except ValueError as exc:
        raise PipelineError(f"Job liegt nicht unter jobs_root: {job_dir}") from exc
    if not job_dir.is_dir():
        raise PipelineError(f"Jobordner fehlt: {job_dir}")
    job_path = job_dir / "job.json"
    if not job_path.is_file():
        raise PipelineError(f"job.json fehlt: {job_path}")
    return job_dir, load_json(job_path), workspace


def job_paths(job_dir: Path, job: dict[str, Any]) -> dict[str, Path]:
    character = str(job.get("character_id", "")).strip()
    version = str(job.get("version", "")).strip()
    if not character or not version:
        raise PipelineError("character_id oder version fehlt in job.json.")
    match = __import__("re").fullmatch(r"v(\d+)", version, flags=__import__("re").IGNORECASE)
    if not match:
        raise PipelineError(f"Ungültige Version, erwartet vNNN: {version}")
    final_version = f"v{int(match.group(1)) + 1:03d}"
    base = f"{character}_{final_version}"
    paths = {
        "input": job_dir / "00_INPUT_SNAPSHOT",
        "work": job_dir / "10_WORK",
        "geometry": job_dir / "10_WORK" / "Geometry",
        "remesh": job_dir / "10_WORK" / "FinalRemesh",
        "guide": job_dir / "10_WORK" / "TextureGuide",
        "exports": job_dir / "30_EXPORTS",
        "textures": job_dir / "30_EXPORTS" / "Textures",
        "previews": job_dir / "40_PREVIEWS" / "FinalMesh",
        "reports": job_dir / "50_REPORTS",
        "status": job_dir / "status.json",
        "summary": job_dir / "50_REPORTS" / "final_mesh_summary.md",
        "geometry_glb": job_dir / "10_WORK" / "Geometry" / f"{base}_geometry.glb",
        "remesh_glb": job_dir / "10_WORK" / "FinalRemesh" / f"{base}_remeshed.glb",
        "guide_png": job_dir / "10_WORK" / "TextureGuide" / f"{base}_texture_guide.png",
        "final_glb": job_dir / "30_EXPORTS" / f"{base}_final_textured.glb",
        "final_fbx": job_dir / "30_EXPORTS" / f"{base}_final_textured.fbx",
    }
    for key in ("input", "work", "exports", "reports"):
        if not paths[key].is_dir():
            raise PipelineError(f"Erwarteter Jobordner fehlt: {paths[key]}")
    paths["base"] = Path(base)
    return paths


def validate_inputs(job: dict[str, Any], input_dir: Path) -> list[Path]:
    entries: dict[str, dict[str, Any]] = {}
    for entry in job.get("input_files", []):
        if isinstance(entry, dict):
            name = str(entry.get("name") or Path(str(entry.get("relative_path", ""))).name)
            entries[name.casefold()] = entry
    inputs: list[Path] = []
    errors: list[str] = []
    for name in REFERENCE_NAMES:
        entry = entries.get(name.casefold())
        if entry is None:
            errors.append(f"job.json enthält keinen Eintrag für {name}")
            continue
        path = input_dir / str(entry.get("relative_path") or entry.get("name"))
        if not path.is_file() or path.stat().st_size <= 0:
            errors.append(f"Eingabebild fehlt oder ist leer: {path}")
            continue
        actual = sha256_file(path)
        expected = str(entry.get("sha256", "")).upper()
        if not expected or actual != expected:
            errors.append(f"SHA-256 abweichend ({name}): erwartet {expected}, gefunden {actual}")
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            errors.append(f"Bild ungültig ({name}): {exc}")
        inputs.append(path)
    if errors:
        raise PipelineError("Eingabeprüfung fehlgeschlagen:\n- " + "\n- ".join(errors))
    return inputs


def blank_status(job: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    compatible = previous.get("pipeline_version") == PIPELINE_VERSION
    return {
        "job_id": job.get("job_id"),
        "pipeline_version": PIPELINE_VERSION,
        "final_mesh_profile": "direct-character-35-v1",
        "phase": "PLAN",
        "status": "FINAL_MESH_PLAN_READY",
        "geometry_task_id": previous.get("geometry_task_id") if compatible else None,
        "remesh_task_id": previous.get("remesh_task_id") if compatible else None,
        "retexture_task_id": previous.get("retexture_task_id") if compatible else None,
        "credits_before": previous.get("credits_before") if compatible else None,
        "credits_after": previous.get("credits_after") if compatible else None,
        "consumed_credits": previous.get("consumed_credits", 0) if compatible else 0,
        "approved_credit_cap": job.get("approved_credit_cap", 0),
        "phase_costs": previous.get("phase_costs", {"geometry": None, "remesh": None, "retexture": None}) if compatible else {"geometry": None, "remesh": None, "retexture": None},
        "phase_status": previous.get("phase_status", {"geometry": "NOT_STARTED", "remesh": "NOT_STARTED", "retexture": "NOT_STARTED"}) if compatible else {"geometry": "NOT_STARTED", "remesh": "NOT_STARTED", "retexture": "NOT_STARTED"},
        "outputs": previous.get("outputs", {}) if compatible else {},
        "validation": previous.get("validation", {}) if compatible else {},
        "error": None,
    }


def write_summary(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def persist_safe_plan_config(job_dir: Path, job: dict[str, Any]) -> None:
    """Installiert nur die offline-sicheren v1-Felder; eine Build-Freigabe bleibt aus."""
    for key, value in DEFAULT_CONFIG.items():
        job[key] = nested_copy(value)
    job["allow_meshy_api_calls"] = False
    job["approved_credit_cap"] = 0
    save_json(job_dir / "job.json", job)


def plan_mode(job_dir: Path, job: dict[str, Any], paths: dict[str, Path], inputs: list[Path]) -> int:
    os.environ["MESHY_NETWORK_BLOCKED"] = "1"
    os.environ["MESHY_POSTS_BLOCKED"] = "1"
    persist_safe_plan_config(job_dir, job)
    current = load_json(paths["status"]) if paths["status"].is_file() else {}
    status = blank_status(job, current)
    planned_outputs = [
        paths["geometry_glb"], paths["remesh_glb"],
        paths["guide_png"], paths["final_glb"], paths["final_fbx"],
        paths["textures"] / "BaseColor.png", paths["textures"] / "Normal.png",
        paths["textures"] / "Roughness.png", paths["textures"] / "Metallic.png",
        *(paths["previews"] / name for name in ("front.png", "left.png", "back.png", "three_quarter.png", "hands_and_feet.png")),
    ]
    status["outputs"] = {"planned": [str(path) for path in planned_outputs]}
    save_json(paths["status"], status)
    effective = effective_config(job)
    missing = [key for key in DEFAULT_CONFIG if key not in job]
    fingerprints = [(path.name, path.stat().st_size, sha256_file(path)) for path in inputs]
    lines = [
        f"# Final-Mesh-Plan – {job.get('job_id', job_dir.name)}", "",
        f"- Pipeline-Version: `{PIPELINE_VERSION}`",
        "- Modus: vollständig offline",
        "- Meshy-Verbindung: blockiert",
        "- Neue Meshy-Tasks in diesem Lauf: 0",
        "- Verbrauchte Credits in diesem Lauf: 0",
        "- Blender gestartet: Nein",
        "- Unreal gestartet: Nein", "",
        "## Geplante Phasen", "",
        "1. Multi-Image-to-3D ohne Textur – maximal 1 POST, lokales GLB.",
        "2. Meshy Remesh – maximal 1 POST, Quad, 60.000 Polygone, nur GLB.",
        "3. Meshy Retexture – maximal 1 POST, neue UVs, PBR 4K, GLB und FBX.",
        "4. Ein lokaler Blender-Validierungslauf – fünf kleine Previews, keine Reparatur.", "",
        "## Credit-Sicherheitsrahmen", "",
        f"- Build nur bei `allow_meshy_api_calls=true` und `approved_credit_cap={APPROVED_BUILD_CAP:g}`.",
        f"- Geometry-Reserve: {PHASE_CREDIT_BUDGETS['geometry']:g}",
        f"- Remesh-Reserve: {PHASE_CREDIT_BUDGETS['remesh']:g}",
        f"- Retexture-Reserve: {PHASE_CREDIT_BUDGETS['retexture']:g}",
        f"- Aktuell allow_meshy_api_calls: `{str(bool(job.get('allow_meshy_api_calls', False))).lower()}`",
        f"- Aktuell approved_credit_cap: `{job.get('approved_credit_cap', 0)}`", "",
        "## Eingaben", "",
    ]
    lines.extend(f"- `{name}` – {size} Byte – SHA-256 `{digest}`" for name, size, digest in fingerprints)
    lines.extend(["", "## Effektive Konfiguration", "", "```json", json.dumps(effective, indent=2, ensure_ascii=False), "```", "", "## Geplante Ausgaben", ""])
    lines.extend(f"- `{path}`" for path in planned_outputs)
    lines.extend(["", "## Sicherheitsregeln", "", "- Bestehende Task-ID wird immer wiederverwendet.", "- Pro Phase höchstens ein POST; keine automatische Wiederholung.", "- Bei Fehler oder Timeout sofortiger Stopp.", "- Polling-Abstand mindestens 10 Sekunden; Task-Zeitlimit 30 Minuten.", "- Keine signierten Download-URLs in Status oder Bericht.", "- Automatisches Baking, Sculpting, Rigging und Unreal-Import sind deaktiviert."])
    if missing:
        lines.extend(["", "## Hinweis", "", "Der kostenlose Plan verwendet sichere Standardwerte. Vor einem späteren Build müssen diese Felder ausdrücklich in job.json stehen:", "", *[f"- `{key}`" for key in missing]])
    write_summary(paths["summary"], lines)
    print("PLAN FINAL MESH MODE")
    print("Meshy-Netzwerk: blockiert")
    print("Geplante Tasks: Geometry 1, Remesh 1, Retexture 1, Rigging 0")
    print(f"Creditlimit für späteren Build: {APPROVED_BUILD_CAP:g}")
    print("Neue Meshy-Tasks: 0")
    print("Verbrauchte Credits: 0")
    print(f"Status: {paths['status']}")
    print(f"Bericht: {paths['summary']}")
    return 0


class MeshySession:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        # Asset URLs can point to a separate storage/CDN host. Keep downloads on
        # an unauthenticated session so the Meshy bearer token is never forwarded
        # outside the API host.
        self.download_session = requests.Session()

    @staticmethod
    def payload(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise PipelineError(f"Meshy lieferte kein JSON (HTTP {response.status_code}).") from exc
        if not response.ok:
            raise PipelineError(f"Meshy API-Fehler {response.status_code}: {data.get('message') or data.get('task_error') or 'ohne Details'}")
        return data

    def balance(self) -> float:
        if os.environ.get("MESHY_NETWORK_BLOCKED") == "1":
            raise PipelineError("Meshy-Netzwerk ist blockiert.")
        response = self.session.get(f"{MESHY_BASE_URL}/balance", timeout=60)
        data = self.payload(response)
        for key in ("balance", "credits", "credit_balance"):
            value = data.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        nested = data.get("result")
        if isinstance(nested, dict):
            for key in ("balance", "credits", "credit_balance"):
                value = nested.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
        raise PipelineError("Balance-Antwort enthält keinen numerischen Creditwert.")

    def create(self, endpoint: str, payload: dict[str, Any]) -> str:
        if os.environ.get("MESHY_POSTS_BLOCKED") == "1":
            raise PipelineError("Meshy-POST ist blockiert.")
        response = self.session.post(f"{MESHY_BASE_URL}/{endpoint.strip('/')}", json=payload, timeout=60)
        data = self.payload(response)
        task_id = data.get("result")
        if isinstance(task_id, dict):
            task_id = task_id.get("id") or task_id.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PipelineError("Meshy lieferte keine gültige Task-ID.")
        return task_id

    @staticmethod
    def task_state(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        task = data.get("result") if isinstance(data.get("result"), dict) and "status" in data["result"] else data
        return str(task.get("status", "")).upper(), task

    def _stream_once(self, endpoint: str, task_id: str, deadline: float) -> dict[str, Any] | None:
        url = f"{MESHY_BASE_URL}/{endpoint.strip('/')}/{task_id}/stream"
        with self.session.get(url, headers={"Accept": "text/event-stream"}, stream=True, timeout=(30, 180)) as response:
            if response.status_code >= 400:
                return None
            for raw_line in response.iter_lines(decode_unicode=True):
                if time.monotonic() >= deadline:
                    raise PipelineError(f"Timeout nach 30 Minuten für Task {task_id}; Task-ID bleibt gespeichert.")
                line = (raw_line or "").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                try:
                    data = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                state, task = self.task_state(data)
                print(f"Meshy {endpoint} SSE: {state or 'UNBEKANNT'} ({task.get('progress', 0)} %)")
                if state == "SUCCEEDED":
                    return data
                if state in {"FAILED", "EXPIRED", "CANCELED"}:
                    raise PipelineError(f"Meshy-Task {task_id} endete mit {state}: {task.get('task_error') or 'ohne Details'}")
        return None

    def watch(self, endpoint: str, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
        for attempt in range(2):
            try:
                streamed = self._stream_once(endpoint, task_id, deadline)
                if streamed is not None:
                    return streamed
            except PipelineError:
                raise
            except requests.RequestException as exc:
                print(f"SSE-Verbindung {attempt + 1}/2 unterbrochen; gleiche Task-ID bleibt aktiv: {exc}")
            if time.monotonic() >= deadline:
                raise PipelineError(f"Timeout nach 30 Minuten für Task {task_id}; Task-ID bleibt gespeichert.")
        print("SSE nicht verfügbar; GET-Polling-Fallback für dieselbe Task-ID.")
        while time.monotonic() < deadline:
            response = self.session.get(f"{MESHY_BASE_URL}/{endpoint.strip('/')}/{task_id}", timeout=60)
            data = self.payload(response)
            state, task = self.task_state(data)
            print(f"Meshy {endpoint}: {state or 'UNBEKANNT'} ({task.get('progress', 0)} %)")
            if state == "SUCCEEDED":
                return data
            if state in {"FAILED", "EXPIRED", "CANCELED"}:
                raise PipelineError(f"Meshy-Task {task_id} endete mit {state}: {task.get('task_error') or 'ohne Details'}")
            time.sleep(POLL_SECONDS)
        raise PipelineError(f"Timeout nach 30 Minuten für Task {task_id}; Task-ID bleibt gespeichert.")

    def download(self, url: str, destination: Path, kind: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        if temporary.exists():
            temporary.unlink()
        with self.download_session.get(url, stream=True, timeout=180) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise PipelineError(f"Download fehlt oder ist leer: {destination}")
        header = temporary.read_bytes()[:64].lstrip().lower()
        if header.startswith((b"<html", b"<!doctype html")):
            raise PipelineError(f"Download ist HTML statt einer Asset-Datei: {destination}")
        if kind == "glb" and temporary.read_bytes()[:4] != b"glTF":
            raise PipelineError(f"Ungültiger GLB-Header: {destination}")
        if kind == "fbx":
            raw = temporary.read_bytes()[:32]
            if not (raw.startswith(b"Kaydara FBX Binary") or b"FBX" in raw.upper()):
                raise PipelineError(f"Ungültiger FBX-Header: {destination}")
        if kind == "image":
            normalized = temporary.with_name(temporary.name + ".png")
            with Image.open(temporary) as image:
                image.verify()
            with Image.open(temporary) as image:
                image.convert("RGBA").save(normalized, format="PNG")
            temporary.unlink()
            temporary = normalized
        temporary.replace(destination)


def result_payload(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def model_urls(result: dict[str, Any]) -> dict[str, str]:
    payload = result_payload(result)
    urls = payload.get("model_urls") or result.get("model_urls") or {}
    found: dict[str, str] = {}
    if isinstance(urls, dict):
        for key in ("glb", "fbx"):
            value = urls.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                found[key] = value
    for key, alias in (("glb", "glb_url"), ("fbx", "fbx_url")):
        value = payload.get(alias)
        if key not in found and isinstance(value, str) and value.startswith(("http://", "https://")):
            found[key] = value
    return found


def texture_urls(result: dict[str, Any]) -> list[tuple[str, str]]:
    aliases = {
        "BaseColor.png": ("base_color", "basecolor", "albedo"),
        "Normal.png": ("normal",), "Roughness.png": ("roughness",),
        "Metallic.png": ("metallic", "metalness"), "Emission.png": ("emission", "emissive"),
    }
    found: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    seen: set[str] = set()
    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}_{key}".casefold())
        elif isinstance(value, list):
            for child in value:
                walk(child, path)
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            for filename, tokens in aliases.items():
                if value not in seen and any(token in path for token in tokens):
                    seen.add(value)
                    stem = Path(filename).stem
                    counts[stem] = counts.get(stem, 0) + 1
                    output = filename if counts[stem] == 1 else f"Material_{counts[stem] - 1:02d}_{filename}"
                    found.append((output, value))
                    break
    walk(result)
    for stem, count in counts.items():
        if count > 1:
            canonical = f"{stem}.png"
            found = [(f"Material_00_{name}" if name == canonical else name, url) for name, url in found]
    return found


def create_texture_guide(inputs: list[Path], destination: Path) -> None:
    canvas = Image.new("RGB", (2048, 2048), (230, 230, 230))
    for index, source in enumerate(inputs):
        with Image.open(source) as image:
            fitted = ImageOps.contain(image.convert("RGB"), (1000, 1000), Image.Resampling.LANCZOS)
        x0 = (index % 2) * 1024 + (1024 - fitted.width) // 2
        y0 = (index // 2) * 1024 + (1024 - fitted.height) // 2
        canvas.paste(fitted, (x0, y0))
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="PNG", optimize=True)


def ensure_credit_room(status: dict[str, Any], phase: str) -> None:
    spent = float(status.get("consumed_credits") or 0)
    needed = PHASE_CREDIT_BUDGETS[phase]
    if spent + needed > APPROVED_BUILD_CAP:
        raise PipelineError(f"Creditlimit vor {phase}-POST nicht ausreichend: verbraucht {spent:g}, nächste Phase {needed:g}, Cap {APPROVED_BUILD_CAP:g}.")


def run_or_resume_phase(client: MeshySession, status: dict[str, Any], status_path: Path, phase: str, endpoint: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any], int]:
    field = f"{phase}_task_id"
    task_id = status.get(field)
    created = 0
    if not task_id:
        ensure_credit_room(status, phase)
        status["phase"] = phase.upper()
        status["status"] = f"CREATING_{phase.upper()}_TASK"
        save_json(status_path, status)
        task_id = client.create(endpoint, payload)
        status[field] = task_id
        status["status"] = f"{phase.upper()}_TASK_SAVED"
        save_json(status_path, status)
        created = 1
    result = client.watch(endpoint, str(task_id))
    previous_balance = status.get("credits_after")
    if previous_balance is None:
        previous_balance = status.get("credits_before")
    current_balance = client.balance()
    if previous_balance is None:
        raise PipelineError("Credits vorher fehlen; tatsächliche Phasenkosten sind nicht zuverlässig bestimmbar.")
    known_cost = status.get("phase_costs", {}).get(phase)
    phase_cost = float(known_cost) if known_cost is not None else max(0.0, float(previous_balance) - current_balance)
    status["phase_costs"][phase] = phase_cost
    status["phase_status"][phase] = "SUCCEEDED"
    status["credits_after"] = current_balance
    status["consumed_credits"] = max(0.0, float(status["credits_before"]) - current_balance)
    status["status"] = f"{phase.upper()}_SUCCEEDED"
    save_json(status_path, status)
    if float(status["consumed_credits"]) > APPROVED_BUILD_CAP:
        raise PipelineError(f"Creditlimit überschritten: {status['consumed_credits']:g} > {APPROVED_BUILD_CAP:g}.")
    return str(task_id), result, created


def run_blender_validation(workspace: dict[str, Any], source: Path, textures: Path, previews: Path) -> dict[str, Any]:
    blender = Path(str(workspace.get("blender_exe", "")))
    script = PIPELINE_DIR / "blender" / "validate_final_mesh.py"
    if not blender.is_file() or not script.is_file():
        raise PipelineError(f"Blender oder Validator fehlt: {blender} / {script}")
    previews.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(blender), "--background", "--factory-startup", "--python", str(script), "--", "--source", str(source), "--textures", str(textures), "--previews", str(previews)],
        capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace",
    )
    marker = "FINAL_MESH_VALIDATION="
    payload = None
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            payload = json.loads(line[len(marker):])
    if result.returncode != 0 or not isinstance(payload, dict):
        tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-20:])
        raise PipelineError(f"Blender-Validierung fehlgeschlagen (Exit {result.returncode}):\n{tail}")
    return payload


def assert_no_unreal() -> None:
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "@(Get-Process UnrealEditor,UnrealEditor-Cmd -ErrorAction SilentlyContinue).Count"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or result.stdout.strip() != "0":
        raise PipelineError("Ein Unreal-Prozess läuft; der Final-Mesh-Build wurde vor dem ersten POST gestoppt.")


def assert_writable(directories: list[Path]) -> None:
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".final_mesh_write_test.tmp"
        probe.write_bytes(b"ok")
        probe.unlink()


def build_mode(job_dir: Path, job: dict[str, Any], workspace: dict[str, Any], paths: dict[str, Path], inputs: list[Path]) -> int:
    validate_exact_config(job)
    if job.get("allow_meshy_api_calls") is not True or float(job.get("approved_credit_cap", 0)) != APPROVED_BUILD_CAP or job.get("direct_full_build") is not True:
        raise PipelineError(f"Build nicht freigegeben: erforderlich sind allow_meshy_api_calls=true, approved_credit_cap={APPROVED_BUILD_CAP:g} und direct_full_build=true.")
    if job.get("status") not in {"MESH_GENERATED_AWAITING_REVIEW", "FAILED", "FINAL_MESH_STOPPED"}:
        raise PipelineError(f"Unzulässiger Jobstatus für den Build: {job.get('status')}")

    # Freigabe atomar verbrauchen, bevor Netzwerkzugriff möglich wird.
    stored_job = nested_copy(job)
    stored_job["allow_meshy_api_calls"] = False
    save_json(job_dir / "job.json", stored_job)

    previous = load_json(paths["status"]) if paths["status"].is_file() else {}
    status = blank_status(job, previous)
    status.update({"phase": "PREFLIGHT", "status": "RUNNING", "approved_credit_cap": APPROVED_BUILD_CAP, "error": None})
    save_json(paths["status"], status)
    directories = [paths["geometry"], paths["remesh"], paths["guide"], paths["textures"], paths["previews"]]
    assert_writable(directories)
    assert_no_unreal()
    blender = Path(str(workspace.get("blender_exe", "")))
    if not blender.is_file():
        raise PipelineError(f"Blender fehlt vor dem ersten POST: {blender}")
    load_dotenv(PIPELINE_DIR / ".env")
    api_key = os.getenv("MESHY_API_KEY", "").strip()
    if not api_key:
        raise PipelineError(f"MESHY_API_KEY fehlt in {PIPELINE_DIR / '.env'}")

    source_hashes = {path.name: sha256_file(path) for path in inputs}
    if not (paths["guide_png"].is_file() and paths["guide_png"].stat().st_size > 0 and status.get("texture_guide_source_hashes") == source_hashes):
        create_texture_guide(inputs, paths["guide_png"])
    status["texture_guide_source_hashes"] = source_hashes
    input_uris = [as_data_uri(path) for path in inputs]
    guide_uri = as_data_uri(paths["guide_png"], "image/png")
    geometry_cfg = job["geometry_generation"]
    geometry_payload = {"image_urls": input_uris, "ai_model": geometry_cfg["ai_model"], "pose_mode": geometry_cfg["pose_mode"], "should_texture": False, "should_remesh": False, "image_enhancement": False, "target_formats": ["glb"]}
    retexture_cfg = job["final_retexture"]
    retexture_template = {"image_style_url": guide_uri, "ai_model": retexture_cfg["ai_model"], "enable_original_uv": False, "enable_pbr": True, "texture_resolution": "4k", "remove_lighting": True, "target_formats": ["glb", "fbx"]}
    save_json(paths["status"], status)

    os.environ["MESHY_NETWORK_BLOCKED"] = "0"
    os.environ["MESHY_POSTS_BLOCKED"] = "0"
    client = MeshySession(api_key)
    created = {"geometry": 0, "remesh": 0, "retexture": 0}
    current_balance = client.balance()
    if status["credits_before"] is None:
        status["credits_before"] = current_balance
        status["credits_after"] = current_balance
    if not any(status.get(field) for field in ("geometry_task_id", "remesh_task_id", "retexture_task_id")) and current_balance < APPROVED_BUILD_CAP:
        raise PipelineError(f"Zu wenig Meshy-Guthaben: {current_balance:g}; mindestens 35 erforderlich.")
    save_json(paths["status"], status)

    _, geometry_result, created["geometry"] = run_or_resume_phase(client, status, paths["status"], "geometry", geometry_cfg["endpoint"], geometry_payload)
    geometry_url = model_urls(geometry_result).get("glb")
    if not geometry_url:
        raise PipelineError("Geometry-Task lieferte keine GLB-URL.")
    if not paths["geometry_glb"].is_file():
        client.download(geometry_url, paths["geometry_glb"], "glb")

    remesh_cfg = job["final_remesh"]
    remesh_payload = {"model_url": as_data_uri(paths["geometry_glb"], "application/octet-stream"), "topology": remesh_cfg["topology"], "target_polycount": int(remesh_cfg["target_polycount"]), "target_formats": ["glb"]}
    _, remesh_result, created["remesh"] = run_or_resume_phase(client, status, paths["status"], "remesh", "remesh", remesh_payload)
    remesh_url = model_urls(remesh_result).get("glb")
    if not remesh_url:
        raise PipelineError("Remesh-Task lieferte kein GLB.")
    if not paths["remesh_glb"].is_file():
        client.download(remesh_url, paths["remesh_glb"], "glb")

    retexture_payload = {"input_task_id": status["remesh_task_id"], **retexture_template}
    _, retexture_result, created["retexture"] = run_or_resume_phase(client, status, paths["status"], "retexture", "retexture", retexture_payload)
    final_urls = model_urls(retexture_result)
    if "glb" not in final_urls or "fbx" not in final_urls:
        raise PipelineError("Retexture-Task lieferte nicht beide Pflichtmodelle GLB und FBX.")
    texture_entries = texture_urls(retexture_result)
    required_texture_names = {"BaseColor.png", "Normal.png", "Roughness.png", "Metallic.png"}
    missing_textures = sorted(name for name in required_texture_names if not any(filename.endswith(name) for filename, _ in texture_entries))
    if missing_textures:
        raise PipelineError("Retexture-Task lieferte fehlende Pflichttexturen: " + ", ".join(missing_textures))
    download_jobs: list[tuple[str, Path, str]] = [
        (final_urls["glb"], paths["final_glb"], "glb"),
        (final_urls["fbx"], paths["final_fbx"], "fbx"),
        *((url, paths["textures"] / filename, "image") for filename, url in texture_entries),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(client.download, url, destination, kind) for url, destination, kind in download_jobs]
        for future in futures:
            future.result()
    downloaded_textures = [paths["textures"] / filename for filename, _ in texture_entries]
    validation = run_blender_validation(workspace, paths["final_glb"], paths["textures"], paths["previews"])
    if validation.get("valid") is not True:
        raise PipelineError("Minimale Blender-Validierung meldet Fehler: " + "; ".join(validation.get("problems", [])))
    after = client.balance()
    consumed = max(0.0, float(status["credits_before"]) - after)
    if consumed > APPROVED_BUILD_CAP:
        raise PipelineError(f"Creditlimit überschritten: {consumed:g} > {APPROVED_BUILD_CAP:g}.")
    preview_paths = [paths["previews"] / name for name in ("front.png", "left.png", "back.png", "three_quarter.png", "hands_and_feet.png")]
    output_map = {"geometry_glb": str(paths["geometry_glb"]), "remesh_glb": str(paths["remesh_glb"]), "final_glb": str(paths["final_glb"]), "final_fbx": str(paths["final_fbx"]), "texture_guide": str(paths["guide_png"]), "textures": [str(path) for path in downloaded_textures], "previews": [str(path) for path in preview_paths]}
    status.update({"phase": "COMPLETE", "status": "FINAL_MESH_COMPLETE", "credits_after": after, "consumed_credits": consumed, "outputs": output_map, "validation": validation, "error": None})
    save_json(paths["status"], status)
    stored_job["status"] = "COMPLETED"
    stored_job["last_mode"] = "build-final-mesh"
    stored_job["last_finished_at"] = utc_now()
    save_json(job_dir / "job.json", stored_job)
    all_outputs = [paths["geometry_glb"], paths["remesh_glb"], paths["guide_png"], paths["final_glb"], paths["final_fbx"], *downloaded_textures, *preview_paths]
    write_summary(paths["summary"], [f"# Final-Mesh-Ergebnis – {job['job_id']}", "", "- Status: FINAL_MESH_COMPLETE", "- Profil: `direct-character-35-v1`", f"- Geometry-Task: `{status['geometry_task_id']}`", f"- Remesh-Task: `{status['remesh_task_id']}`", f"- Retexture-Task: `{status['retexture_task_id']}`", f"- Geometry-Credits: {status['phase_costs']['geometry']}", f"- Remesh-Credits: {status['phase_costs']['remesh']}", f"- Retexture-Credits: {status['phase_costs']['retexture']}", f"- Credits vorher/nachher/verbraucht: {status['credits_before']:g} / {after:g} / {consumed:g}", f"- Neue Tasks: Geometry {created['geometry']}, Remesh {created['remesh']}, Retexture {created['retexture']}", "- Rigging: 0", "- Unreal: nicht gestartet", "- Signierte URLs gespeichert: Nein", "", "## Ausgaben", "", *[f"- `{path}`" for path in all_outputs], "", "## Blender-Validierung", "", "```json", json.dumps(validation, indent=2, ensure_ascii=False), "```"])
    print(f"FINAL_MESH_COMPLETE: {paths['final_glb']}")
    return 0


def disable_api(job_dir: Path) -> None:
    job_path = job_dir / "job.json"
    current = load_json(job_path)
    if current.get("allow_meshy_api_calls") is not False:
        current["allow_meshy_api_calls"] = False
        save_json(job_path, current)


def main() -> int:
    parser = argparse.ArgumentParser(description="Geometry - Remesh - Retexture Final-Mesh-Pipeline")
    jobs = parser.add_mutually_exclusive_group(required=True)
    jobs.add_argument("--latest-job", action="store_true")
    jobs.add_argument("--job")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan-final-mesh", action="store_true")
    modes.add_argument("--build-final-mesh", action="store_true")
    args = parser.parse_args()
    job_dir: Path | None = None
    job: dict[str, Any] = {}
    paths: dict[str, Path] | None = None
    try:
        job_dir, job, workspace = resolve_job(args)
        paths = job_paths(job_dir, job)
        inputs = validate_inputs(job, paths["input"])
        if args.plan_final_mesh:
            return plan_mode(job_dir, job, paths, inputs)
        return build_mode(job_dir, job, workspace, paths, inputs)
    except Exception as exc:
        message = str(exc)
        print(message, file=sys.stderr)
        if paths is not None:
            previous = load_json(paths["status"]) if paths["status"].is_file() else {}
            status = blank_status(job, previous)
            status.update({"phase": previous.get("phase") or "ERROR", "status": "FINAL_MESH_STOPPED", "error": message})
            save_json(paths["status"], status)
            write_summary(paths["summary"], [f"# Final-Mesh-Pipeline – {job.get('job_id', 'unbekannt')}", "", "- Status: FINAL_MESH_STOPPED", f"- Fehler: {message}", "- Automatische Wiederholung: Nein", "- Task-IDs bleiben gespeichert", "- Rigging: 0", "- Unreal: nicht gestartet"])
        if job_dir is not None and (job_dir / "job.json").is_file():
            failed_job = load_json(job_dir / "job.json")
            failed_job["allow_meshy_api_calls"] = False
            failed_job["status"] = "FAILED"
            failed_job["last_mode"] = "build-final-mesh"
            failed_job["last_finished_at"] = utc_now()
            save_json(job_dir / "job.json", failed_job)
        return 1
    finally:
        if args.build_final_mesh and job_dir is not None and job:
            try:
                disable_api(job_dir)
            except Exception as exc:
                print(f"KRITISCH: allow_meshy_api_calls konnte nicht auf false gesetzt werden: {exc}", file=sys.stderr)
        os.environ["MESHY_NETWORK_BLOCKED"] = "1"
        os.environ["MESHY_POSTS_BLOCKED"] = "1"


if __name__ == "__main__":
    raise SystemExit(main())
