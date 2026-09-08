from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from PIL import Image

from meshy_client import MeshyClient, MeshyError


PIPELINE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PIPELINE_DIR / "pipeline_config.json"
WORKSPACE_NAME = "IsekaiAscension_Workspace"
REQUIRED_JOB_FIELDS = (
    "job_id",
    "character_id",
    "version",
    "task_type",
    "character_height_meters",
    "target_skeleton_reference",
    "allow_meshy_api_calls",
    "automatic_unreal_import",
    "unreal_ready_dir",
    "input_files",
)
MODE_LABELS = {
    "preflight": "PREFLIGHT",
    "execute": "EXECUTE",
    "resume-local": "LOCAL RESUME",
    "export-only": "EXPORT ONLY",
    "generate-only": "GENERATE ONLY",
    "plan-generate-only": "PLAN GENERATE ONLY",
}
GENERATION_REFERENCE_NAMES = (
    "character_front.png",
    "character_left.png",
    "character_back.png",
    "character_three_quarter.png",
)
GENERATE_ONLY_FIELDS = (
    "generation_provider",
    "meshy_ai_model",
    "meshy_should_texture",
    "meshy_enable_pbr",
    "meshy_texture_resolution",
    "meshy_pose_mode",
    "meshy_image_enhancement",
    "meshy_remove_lighting",
    "meshy_should_remesh",
    "meshy_topology",
    "meshy_target_polycount",
    "meshy_save_pre_remeshed_model",
    "meshy_target_formats",
    "meshy_auto_rigging_enabled",
    "stop_after_mesh_generation",
    "bind_to_target_skeleton_later",
)
RIGGING_BLOCK_MESSAGE = (
    "MESHY RIGGING BLOCKED\n"
    "Dieser Job verwendet später das Learning-Kit-Zielskelett.\n"
    "Es wurde kein Rigging-Task erstellt."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def desktop_from_dotnet() -> Path:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Environment]::GetFolderPath('Desktop')",
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    desktop = result.stdout.strip()
    if not desktop:
        raise RuntimeError("Der Desktop-Pfad konnte über .NET nicht ermittelt werden.")
    return Path(desktop)


def load_workspace() -> tuple[Path, dict[str, Any]]:
    configured_workspace = os.environ.get("CHARACTER_PIPELINE_WORKSPACE", "").strip()
    workspace_root = (
        Path(configured_workspace).expanduser().resolve()
        if configured_workspace
        else desktop_from_dotnet() / WORKSPACE_NAME
    )
    workspace_path = workspace_root / "00_START_HERE" / "workspace.json"
    if not workspace_path.is_file():
        raise RuntimeError(f"workspace.json fehlt: {workspace_path}")
    workspace = load_json(workspace_path)
    configured_root = Path(str(workspace.get("workspace_root", "")))
    if configured_root.resolve() != workspace_root.resolve():
        raise RuntimeError(
            "workspace_root in workspace.json stimmt nicht mit dem über den Desktop "
            f"ermittelten Pfad überein: {configured_root}"
        )
    configured_pipeline = Path(str(workspace.get("character_pipeline_dir", "")))
    if configured_pipeline.resolve() != PIPELINE_DIR.resolve():
        raise RuntimeError(
            "Diese Pipeline liegt nicht im in workspace.json eingetragenen Ordner: "
            f"{configured_pipeline}"
        )
    if workspace.get("automatic_unreal_import") is not False:
        raise RuntimeError("automatic_unreal_import muss in workspace.json false sein.")
    return workspace_root, workspace


def resolve_job(
    args: argparse.Namespace,
    workspace_root: Path,
    workspace: dict[str, Any],
) -> Path:
    jobs_root = Path(str(workspace["jobs_root"])).resolve()
    if args.latest_job:
        latest_path = workspace_root / "00_START_HERE" / "LATEST_JOB.txt"
        if not latest_path.is_file():
            raise RuntimeError(f"LATEST_JOB.txt fehlt: {latest_path}")
        lines = [
            line.strip()
            for line in latest_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
        if len(lines) != 1:
            raise RuntimeError(
                f"LATEST_JOB.txt muss genau einen vollständigen Jobpfad enthalten: {latest_path}"
            )
        job_dir = Path(lines[0])
    else:
        job_dir = Path(args.job)
        if not job_dir.is_absolute():
            raise RuntimeError("--job muss einen vollständigen absoluten Pfad enthalten.")

    job_dir = job_dir.resolve()
    if not is_relative_to(job_dir, jobs_root):
        raise RuntimeError(
            f"Der Job muss unter dem konfigurierten jobs_root liegen: {jobs_root}"
        )
    if not job_dir.is_dir():
        raise RuntimeError(f"Der vorhandene Jobordner fehlt: {job_dir}")
    return job_dir


def load_job(job_dir: Path) -> dict[str, Any]:
    job_path = job_dir / "job.json"
    if not job_path.is_file():
        raise RuntimeError(f"job.json fehlt: {job_path}")
    return load_json(job_path)


def job_directories(job_dir: Path, job: dict[str, Any]) -> dict[str, Path]:
    directories = {
        "input": job_dir / "00_INPUT_SNAPSHOT",
        "work": job_dir / "10_WORK",
        "blender": job_dir / "20_BLENDER",
        "exports": job_dir / "30_EXPORTS",
        "textures": job_dir / "30_EXPORTS" / "Textures",
        "previews": job_dir / "40_PREVIEWS",
        "reports": job_dir / "50_REPORTS",
        "logs": job_dir / "60_LOGS",
        "meshy_raw": job_dir / "10_WORK" / "MeshyRaw",
        "meshy_previews": job_dir / "40_PREVIEWS" / "Meshy",
        "unreal_ready": Path(str(job.get("unreal_ready_dir", ""))),
    }
    configured_mapping = {
        "input_snapshot": directories["input"],
        "work_dir": directories["work"],
        "blender_dir": directories["blender"],
        "exports_dir": directories["exports"],
        "previews_dir": directories["previews"],
        "reports_dir": directories["reports"],
        "logs_dir": directories["logs"],
    }
    mismatches: list[str] = []
    for field, expected in configured_mapping.items():
        configured = job.get(field)
        if configured and Path(str(configured)).resolve() != expected.resolve():
            mismatches.append(f"{field}: erwartet {expected}, eingetragen {configured}")
    if mismatches:
        raise RuntimeError(
            "Job-Verzeichniszuordnung ist inkonsistent:\n- " + "\n- ".join(mismatches)
        )
    for key in ("input", "work", "blender", "exports", "previews", "reports", "logs"):
        if not directories[key].is_dir():
            raise RuntimeError(f"Erwarteter Job-Unterordner fehlt: {directories[key]}")
    directories["textures"].mkdir(parents=True, exist_ok=True)
    return directories


def filename_base(job: dict[str, Any]) -> str:
    return f"{job['character_id']}_{job['version']}"


def validate_required_fields(job: dict[str, Any]) -> None:
    missing = [
        field
        for field in REQUIRED_JOB_FIELDS
        if field not in job or job[field] is None or job[field] == ""
    ]
    if missing:
        raise RuntimeError("Fehlende Pflichtfelder in job.json: " + ", ".join(missing))
    if str(job["task_type"]) != "character_model":
        raise RuntimeError(
            f"Nicht unterstützter task_type: {job['task_type']} (erwartet: character_model)"
        )
    if job["automatic_unreal_import"] is not False:
        raise RuntimeError("automatic_unreal_import muss in job.json false sein.")
    if not isinstance(job["allow_meshy_api_calls"], bool):
        raise RuntimeError("allow_meshy_api_calls muss ein boolescher Wert sein.")
    if not isinstance(job["input_files"], list) or not job["input_files"]:
        raise RuntimeError("input_files muss mindestens eine Eingabedatei enthalten.")
    try:
        height = float(job["character_height_meters"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("character_height_meters muss eine Zahl sein.") from exc
    if height <= 0:
        raise RuntimeError("character_height_meters muss größer als 0 sein.")


def validate_generate_only_config(job: dict[str, Any]) -> None:
    missing = [
        field
        for field in GENERATE_ONLY_FIELDS
        if field not in job or job[field] is None or job[field] == ""
    ]
    if missing:
        raise RuntimeError(
            "Fehlende Generate-only-Felder in job.json: " + ", ".join(missing)
        )
    expected = {
        "generation_provider": "meshy",
        "meshy_ai_model": "latest",
        "meshy_should_texture": True,
        "meshy_enable_pbr": True,
        "meshy_texture_resolution": "2k",
        "meshy_pose_mode": "a-pose",
        "meshy_image_enhancement": False,
        "meshy_remove_lighting": True,
        "meshy_should_remesh": True,
        "meshy_topology": "quad",
        "meshy_target_polycount": 60000,
        "meshy_save_pre_remeshed_model": True,
        "meshy_target_formats": ["glb", "fbx"],
        "meshy_auto_rigging_enabled": False,
        "stop_after_mesh_generation": True,
        "bind_to_target_skeleton_later": True,
    }
    mismatches = [
        f"{field}: erwartet {value!r}, eingetragen {job.get(field)!r}"
        for field, value in expected.items()
        if job.get(field) != value
    ]
    if mismatches:
        raise RuntimeError(
            "Unsichere oder abweichende Generate-only-Konfiguration:\n- "
            + "\n- ".join(mismatches)
        )


def generation_input_paths(
    job: dict[str, Any],
    directories: dict[str, Path],
) -> list[Path]:
    entries_by_name: dict[str, dict[str, Any]] = {}
    for entry in job["input_files"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or Path(str(entry.get("relative_path", ""))).name)
        if name:
            entries_by_name[name.casefold()] = entry
    actual_names = set(entries_by_name)
    expected_names = {name.casefold() for name in GENERATION_REFERENCE_NAMES}
    missing = [
        name for name in GENERATION_REFERENCE_NAMES if name.casefold() not in actual_names
    ]
    extras = sorted(actual_names - expected_names)
    if missing or extras:
        details: list[str] = []
        if missing:
            details.append("fehlend: " + ", ".join(missing))
        if extras:
            details.append("unerwartet: " + ", ".join(extras))
        raise RuntimeError(
            "Generate-only verlangt exakt die vier freigegebenen Eingabebilder ("
            + "; ".join(details)
            + ")."
        )
    ordered: list[Path] = []
    for name in GENERATION_REFERENCE_NAMES:
        entry = entries_by_name[name.casefold()]
        relative = str(entry.get("relative_path") or entry.get("name"))
        path = (directories["input"] / relative).resolve()
        if path.name.casefold() != name.casefold():
            raise RuntimeError(
                f"Input-Zuordnung ist nicht eindeutig: erwartet {name}, gefunden {path.name}"
            )
        ordered.append(path)
    return ordered


def generation_payload(job: dict[str, Any], inputs: list[Path]) -> dict[str, Any]:
    return {
        "image_urls": [image_data_uri(path) for path in inputs],
        "ai_model": job["meshy_ai_model"],
        "should_texture": job["meshy_should_texture"],
        "enable_pbr": job["meshy_enable_pbr"],
        "texture_resolution": job["meshy_texture_resolution"],
        "pose_mode": job["meshy_pose_mode"],
        "image_enhancement": job["meshy_image_enhancement"],
        "remove_lighting": job["meshy_remove_lighting"],
        "should_remesh": job["meshy_should_remesh"],
        "topology": job["meshy_topology"],
        "target_polycount": int(job["meshy_target_polycount"]),
        "save_pre_remeshed_model": job["meshy_save_pre_remeshed_model"],
        "target_formats": list(job["meshy_target_formats"]),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate_inputs(
    job: dict[str, Any],
    directories: dict[str, Path],
    config: dict[str, Any],
) -> tuple[list[Path], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    input_paths: list[Path] = []
    minimum_width = int(config.get("minimum_reference_width", 512))
    minimum_height = int(config.get("minimum_reference_height", 512))
    for entry in job["input_files"]:
        if not isinstance(entry, dict):
            errors.append(f"Ungültiger input_files-Eintrag: {entry!r}")
            continue
        relative = str(entry.get("relative_path") or entry.get("name") or "")
        if not relative:
            errors.append("Ein input_files-Eintrag besitzt keinen Dateinamen.")
            continue
        path = (directories["input"] / relative).resolve()
        if not is_relative_to(path, directories["input"]):
            errors.append(f"Eingabepfad verlässt 00_INPUT_SNAPSHOT: {relative}")
            continue
        input_paths.append(path)
        if not path.is_file():
            errors.append(f"Eingabedatei fehlt: {path}")
            continue
        if path.stat().st_size <= 0:
            errors.append(f"Eingabedatei ist leer: {path}")
            continue
        expected_bytes = entry.get("bytes")
        if expected_bytes is not None and int(expected_bytes) != path.stat().st_size:
            errors.append(
                f"Dateigröße verändert ({path.name}): "
                f"erwartet {expected_bytes}, gefunden {path.stat().st_size}"
            )
        expected_hash = str(entry.get("sha256", "")).upper()
        if not expected_hash:
            errors.append(f"SHA-256 fehlt in job.json: {path.name}")
        else:
            actual_hash = sha256_file(path)
            if actual_hash != expected_hash:
                errors.append(
                    f"SHA-256 stimmt nicht überein ({path.name}): "
                    f"erwartet {expected_hash}, gefunden {actual_hash}"
                )
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            errors.append(f"Nicht unterstütztes Referenzformat: {path.name}")
            continue
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format
            if width < minimum_width or height < minimum_height:
                errors.append(
                    f"{path.name} ist mit {width}×{height} px zu klein; "
                    f"mindestens {minimum_width}×{minimum_height} px erforderlich."
                )
            if image_format not in {"PNG", "JPEG"}:
                warnings.append(f"Ungewöhnliches Bildformat erkannt: {path.name} ({image_format})")
        except Exception as exc:
            errors.append(f"Referenzbild ist beschädigt ({path.name}): {exc}")

    skeleton = Path(str(job["target_skeleton_reference"]))
    if not skeleton.is_file():
        errors.append(f"Skelett-Referenzdatei fehlt: {skeleton}")
    elif skeleton.stat().st_size <= 0:
        errors.append(f"Skelett-Referenzdatei ist leer: {skeleton}")

    if errors:
        raise RuntimeError("Preflight fehlgeschlagen:\n- " + "\n- ".join(errors))
    return input_paths, warnings


def image_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def locate_blender(workspace: dict[str, Any]) -> Path | None:
    candidate = Path(str(workspace.get("blender_exe", "")))
    return candidate if candidate.is_file() else None


def state_path(directories: dict[str, Path]) -> Path:
    return directories["logs"] / "pipeline_state.json"


def load_pipeline_state(directories: dict[str, Path]) -> dict[str, Any]:
    path = state_path(directories)
    return load_json(path) if path.is_file() else {}


def save_pipeline_state(directories: dict[str, Path], state: dict[str, Any]) -> None:
    save_json(state_path(directories), state)


def recover_created_task(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        task_id = load_json(path).get("result")
    except Exception:
        return None
    return task_id if isinstance(task_id, str) and task_id else None


def status_data(job_dir: Path) -> dict[str, Any]:
    path = job_dir / "status.json"
    return load_json(path) if path.is_file() else {}


def update_status_data(job_dir: Path, values: dict[str, Any]) -> dict[str, Any]:
    payload = status_data(job_dir)
    payload.update(values)
    save_json(job_dir / "status.json", payload)
    return payload


def input_fingerprints(inputs: list[Path]) -> list[dict[str, Any]]:
    return [
        {
            "name": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in inputs
    ]


def expected_credit_text(job: dict[str, Any]) -> str:
    configured = job.get("meshy_expected_max_credits")
    if isinstance(configured, (int, float)) and configured >= 0:
        return str(configured)
    return (
        "nicht offline verifizierbar; maximal die zum Freigabezeitpunkt gültigen "
        "Kosten eines Multi-Image-to-3D-Tasks"
    )


def write_generate_only_plan(
    job: dict[str, Any],
    directories: dict[str, Path],
    inputs: list[Path],
) -> list[Path]:
    base = filename_base(job)
    fingerprints = input_fingerprints(inputs)
    targets = {
        "raw": str(directories["meshy_raw"]),
        "previews": str(directories["meshy_previews"]),
        "reports": str(directories["reports"]),
        "logs": str(directories["logs"]),
    }
    settings = {
        "ai_model": job["meshy_ai_model"],
        "should_texture": job["meshy_should_texture"],
        "enable_pbr": job["meshy_enable_pbr"],
        "texture_resolution": job["meshy_texture_resolution"],
        "pose_mode": job["meshy_pose_mode"],
        "image_enhancement": job["meshy_image_enhancement"],
        "remove_lighting": job["meshy_remove_lighting"],
        "should_remesh": job["meshy_should_remesh"],
        "topology": job["meshy_topology"],
        "target_polycount": job["meshy_target_polycount"],
        "save_pre_remeshed_model": job["meshy_save_pre_remeshed_model"],
        "target_formats": job["meshy_target_formats"],
        "image_transport": "lokale Base64-Data-URIs",
    }
    credit_text = expected_credit_text(job)
    payload = {
        "plan_created_at": utc_now(),
        "job_id": job["job_id"],
        "job_name": job.get("job_name", job["job_id"]),
        "character_id": job["character_id"],
        "version": job["version"],
        "inputs": fingerprints,
        "meshy_request": settings,
        "planned_meshy_tasks": 1,
        "planned_rigging_tasks": 0,
        "expected_maximum_credit_consumption": credit_text,
        "target_directories": targets,
        "rigging_blocked": job["meshy_auto_rigging_enabled"] is False,
        "stop_after_mesh_generation": job["stop_after_mesh_generation"],
        "unreal_import_disabled": job["automatic_unreal_import"] is False,
        "network_contacted": False,
        "tasks_created_in_plan": 0,
        "credits_consumed_in_plan": 0,
    }
    json_path = directories["reports"] / f"{base}_mesh_generation_plan.json"
    markdown_path = directories["reports"] / f"{base}_mesh_generation_plan.md"
    save_json(json_path, payload)
    lines = [
        f"# Meshy Generate-only-Plan – {base}",
        "",
        f"- Job-ID: `{job['job_id']}`",
        f"- Jobname: `{job.get('job_name', job['job_id'])}`",
        f"- Character-ID: `{job['character_id']}`",
        f"- Version: `{job['version']}`",
        "- Netzwerkverbindung zu Meshy: Nein",
        "- In diesem Plan erzeugte Tasks: 0",
        "- In diesem Plan verbrauchte Credits: 0",
        "",
        "## Eingaben",
        "",
    ]
    lines.extend(
        f"- `{item['name']}` — {item['bytes']} Byte — SHA-256 `{item['sha256']}`"
        for item in fingerprints
    )
    lines.extend(
        [
            "",
            "## Geplanter Request",
            "",
            f"- Modell: `{settings['ai_model']}`",
            f"- Texturauflösung: `{settings['texture_resolution']}`",
            f"- PBR: {'Ja' if settings['enable_pbr'] else 'Nein'}",
            f"- Pose Mode: `{settings['pose_mode']}`",
            f"- Remesh: {'Ja' if settings['should_remesh'] else 'Nein'}",
            f"- Ziel-Polycount: {settings['target_polycount']}",
            f"- Ausgabeformate: {', '.join(settings['target_formats'])}",
            "- Bildübertragung: lokale Base64-Data-URIs",
            "- Geplante Multi-Image-to-3D-Tasks: 1",
            "- Geplante Rigging-Tasks: 0",
            f"- Erwarteter maximaler Creditverbrauch: {credit_text}",
            "- Rigging ausdrücklich blockiert: Ja",
            "- Unreal-Import deaktiviert: Ja",
            "",
            "## Zielordner",
            "",
        ]
    )
    lines.extend(f"- {key}: `{value}`" for key, value in targets.items())
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Job-ID: {job['job_id']}")
    print(f"Character-ID: {job['character_id']}")
    print(f"Version: {job['version']}")
    print("Verwendete Eingabebilder:")
    for item in fingerprints:
        print(f"- {item['name']} | {item['bytes']} Byte | SHA-256 {item['sha256']}")
    print(f"Geplantes Meshy-Modell: {settings['ai_model']}")
    print(f"Texturauflösung: {settings['texture_resolution']}")
    print(f"PBR aktiviert: {'Ja' if settings['enable_pbr'] else 'Nein'}")
    print(f"Pose Mode: {settings['pose_mode']}")
    print(
        f"Remesh: {'Ja' if settings['should_remesh'] else 'Nein'} | "
        f"Ziel-Polycount: {settings['target_polycount']}"
    )
    print(f"Geplante Ausgabeformate: {', '.join(settings['target_formats'])}")
    print("Geplante Meshy-Tasks: exakt 1")
    print("Geplante Rigging-Tasks: 0")
    print(f"Erwarteter maximaler Creditverbrauch: {credit_text}")
    print(f"Zielordner Mesh: {targets['raw']}")
    print(f"Zielordner Previews: {targets['previews']}")
    print("Rigging ausdrücklich blockiert: Ja")
    print("Unreal-Import deaktiviert: Ja")
    print("Neue Meshy-Tasks in diesem Plan: 0")
    print("Zusätzliche Meshy-Credits in diesem Plan: 0")
    return [json_path, markdown_path]


def result_payload(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def balance_value(payload: dict[str, Any]) -> float:
    candidates: list[Any] = [
        payload.get("balance"),
        payload.get("credits"),
        payload.get("credit_balance"),
    ]
    nested = payload.get("result")
    if isinstance(nested, dict):
        candidates.extend(
            [
                nested.get("balance"),
                nested.get("credits"),
                nested.get("credit_balance"),
            ]
        )
    else:
        candidates.append(nested)
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return float(candidate)
        if isinstance(candidate, str):
            try:
                return float(candidate)
            except ValueError:
                continue
    raise RuntimeError(
        "Die Meshy-Balance-Antwort enthält keinen auswertbaren Creditwert. "
        "Es wurde kein kostenpflichtiger POST freigegeben."
    )


def current_hashes_match_job(job: dict[str, Any], inputs: list[Path]) -> None:
    by_name = {
        str(entry.get("name") or Path(str(entry.get("relative_path", ""))).name).casefold(): entry
        for entry in job["input_files"]
        if isinstance(entry, dict)
    }
    errors: list[str] = []
    for path in inputs:
        entry = by_name.get(path.name.casefold())
        if entry is None:
            errors.append(f"Kein Hash-Eintrag in job.json: {path.name}")
            continue
        expected_bytes = int(entry.get("bytes", -1))
        expected_hash = str(entry.get("sha256", "")).upper()
        actual_bytes = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_bytes != expected_bytes or actual_hash != expected_hash:
            errors.append(
                f"{path.name}: erwartet {expected_bytes} Byte / {expected_hash}, "
                f"gefunden {actual_bytes} Byte / {actual_hash}"
            )
    if errors:
        raise RuntimeError(
            "Eingabebilder wurden verändert. Es wurde kein POST ausgeführt; "
            "eine neue ausdrückliche Freigabe ist erforderlich:\n- "
            + "\n- ".join(errors)
        )


def download_generate_only_models(
    client: MeshyClient,
    result: dict[str, Any],
    directories: dict[str, Path],
    base: str,
) -> tuple[list[Path], list[str]]:
    payload = result_payload(result)
    model_urls = payload.get("model_urls") or result.get("model_urls") or {}
    if not isinstance(model_urls, dict):
        model_urls = {}
    sources = {
        "glb": model_urls.get("glb") or payload.get("glb_url"),
        "fbx": model_urls.get("fbx") or payload.get("fbx_url"),
        "pre_remeshed": (
            model_urls.get("pre_remeshed_glb")
            or model_urls.get("pre_remeshed")
            or model_urls.get("pre_remeshed_model")
            or payload.get("pre_remeshed_glb_url")
            or payload.get("pre_remeshed_model_url")
        ),
    }
    destinations = {
        "glb": directories["meshy_raw"] / f"{base}_meshy_raw.glb",
        "fbx": directories["meshy_raw"] / f"{base}_meshy_raw.fbx",
        "pre_remeshed": directories["meshy_raw"] / f"{base}_pre_remeshed.glb",
    }
    outputs: list[Path] = []
    warnings: list[str] = []
    for key, url in sources.items():
        if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
            client.download(url, destinations[key])
            outputs.append(destinations[key])
    for key in ("glb", "fbx"):
        if not destinations[key].is_file() or destinations[key].stat().st_size <= 0:
            warnings.append(f"Meshy lieferte kein separates {key.upper()}-Modell.")
    if not any(
        destinations[key].is_file() and destinations[key].stat().st_size > 0
        for key in ("glb", "fbx")
    ):
        raise MeshyError(
            "Meshy lieferte weder ein GLB- noch ein FBX-Rohmodell."
        )
    if not destinations["pre_remeshed"].is_file():
        warnings.append("Meshy lieferte kein separates Pre-Remeshed-GLB.")
    return outputs, warnings


def texture_url_entries(result: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    texture_tokens = (
        "texture",
        "base_color",
        "basecolor",
        "albedo",
        "normal",
        "roughness",
        "metallic",
        "metalness",
        "emissive",
        "opacity",
        "ao",
        "ambient_occlusion",
    )

    def walk(value: Any, path: list[str], texture_context: bool = False) -> None:
        label = "_".join(path).casefold()
        context = texture_context or any(token in label for token in texture_tokens)
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + [str(key)], context)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + [str(index + 1)], context)
        elif (
            context
            and isinstance(value, str)
            and value.lower().startswith(("http://", "https://"))
            and value not in seen_urls
        ):
            seen_urls.add(value)
            raw_name = "_".join(path[-3:]) or f"texture_{len(found) + 1}"
            safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_name).strip("_").lower()
            found.append((safe_name or f"texture_{len(found) + 1}", value))

    walk(result, [])
    return found


def download_generate_only_textures(
    client: MeshyClient,
    result: dict[str, Any],
    directories: dict[str, Path],
    base: str,
) -> tuple[list[Path], list[str]]:
    entries = texture_url_entries(result)
    outputs: list[Path] = []
    warnings: list[str] = []
    texture_dir = directories["meshy_raw"] / "Textures"
    used_names: set[str] = set()
    for index, (label, url) in enumerate(entries, start=1):
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".tga", ".exr"}:
            suffix = ".png"
        stem = f"{base}_{label}"
        if stem.casefold() in used_names:
            stem = f"{stem}_{index:02d}"
        used_names.add(stem.casefold())
        destination = texture_dir / f"{stem}{suffix}"
        client.download(url, destination)
        outputs.append(destination)
    if not outputs:
        warnings.append(
            "Meshy lieferte keine separaten PBR-Textur-URLs; Texturen können im GLB eingebettet sein."
        )
    return outputs, warnings


def preview_url_map(result: dict[str, Any]) -> dict[str, str]:
    payload = result_payload(result)
    containers = [
        result,
        payload,
        result.get("preview_urls"),
        payload.get("preview_urls"),
        result.get("rendered_images"),
        payload.get("rendered_images"),
    ]
    aliases = {
        "front": ("front", "front_url", "front_image_url"),
        "right": ("right", "right_url", "right_image_url"),
        "back": ("back", "back_url", "back_image_url"),
        "left": ("left", "left_url", "left_image_url"),
        "main": ("main", "main_url", "main_image_url", "thumbnail_url"),
    }
    found: dict[str, str] = {}
    for container in containers:
        if not isinstance(container, dict):
            continue
        for target, keys in aliases.items():
            if target in found:
                continue
            for key in keys:
                value = container.get(key)
                if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
                    found[target] = value
                    break
    return found


def download_generate_only_previews(
    client: MeshyClient,
    result: dict[str, Any],
    directories: dict[str, Path],
) -> tuple[list[Path], list[str]]:
    urls = preview_url_map(result)
    outputs: list[Path] = []
    warnings: list[str] = []
    for name in ("front", "right", "back", "left", "main"):
        url = urls.get(name)
        if not url:
            warnings.append(f"Meshy lieferte keine {name}-Vorschau.")
            continue
        destination = directories["meshy_previews"] / f"{name}.png"
        client.download(url, destination)
        try:
            with Image.open(destination) as image:
                converted = image.convert("RGBA")
                converted.save(destination, format="PNG")
        except Exception as exc:
            raise MeshyError(
                f"Meshy-Vorschau konnte nicht als PNG normalisiert werden ({name}): {exc}"
            ) from exc
        outputs.append(destination)
    return outputs, warnings


def write_mesh_generation_report(
    job: dict[str, Any],
    directories: dict[str, Path],
    task_id: str,
    inputs: list[Path],
    outputs: list[Path],
    warnings: list[str],
    balance_before: float,
    balance_after: float,
    consumed_credits: float,
    credit_limit: float,
) -> list[Path]:
    base = filename_base(job)
    input_data = input_fingerprints(inputs)
    output_data = [
        {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in outputs
        if path.is_file()
    ]
    payload = {
        "created_at": utc_now(),
        "job_id": job["job_id"],
        "job_name": job.get("job_name", job["job_id"]),
        "character_id": job["character_id"],
        "version": job["version"],
        "mode": "generate-only",
        "meshy_generation_task_id": task_id,
        "meshy_tasks_created_maximum": 1,
        "rigging_tasks_created": 0,
        "automatic_retries": 0,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "consumed_credits": consumed_credits,
        "authorized_credit_limit": credit_limit,
        "rigging_blocked": True,
        "stop_after_mesh_generation": True,
        "input_files": input_data,
        "request_configuration": {
            key: value
            for key, value in generation_payload(job, []).items()
            if key != "image_urls"
        },
        "image_transport": "lokale Base64-Data-URIs",
        "output_files": output_data,
        "warnings": warnings,
        "signed_urls_stored": False,
        "blender_started": False,
        "unreal_started": False,
    }
    json_path = directories["reports"] / f"{base}_mesh_generation.json"
    markdown_path = directories["reports"] / f"{base}_mesh_generation.md"
    save_json(json_path, payload)
    lines = [
        f"# Meshy Mesh-Generierung – {base}",
        "",
        f"- Job-ID: `{job['job_id']}`",
        f"- Jobname: `{job.get('job_name', job['job_id'])}`",
        f"- Task-ID: `{task_id}`",
        "- Modus: Generate-only",
        "- Rigging-Tasks: 0",
        "- Automatische Wiederholungen: 0",
        f"- Balance vorher: {balance_before:g}",
        f"- Balance nachher: {balance_after:g}",
        f"- Tatsächlich verbrauchte Credits: {consumed_credits:g}",
        f"- Freigegebenes Creditlimit: {credit_limit:g}",
        "- Rigging blockiert: Ja",
        "- Blender gestartet: Nein",
        "- Unreal gestartet: Nein",
        "- Signierte URLs gespeichert: Nein",
        "",
        "## Eingaben",
        "",
    ]
    lines.extend(
        f"- `{item['name']}` — {item['bytes']} Byte — SHA-256 `{item['sha256']}`"
        for item in input_data
    )
    lines.extend(["", "## Lokale Ausgaben", ""])
    lines.extend(
        f"- `{item['path']}` — {item['bytes']} Byte — SHA-256 `{item['sha256']}`"
        for item in output_data
    )
    lines.extend(["", "## Warnungen", ""])
    lines.extend([f"- {warning}" for warning in warnings] or ["- keine"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [json_path, markdown_path]


def run_generate_only(
    job_dir: Path,
    job: dict[str, Any],
    directories: dict[str, Path],
    inputs: list[Path],
) -> tuple[list[Path], int, list[str]]:
    if job["allow_meshy_api_calls"] is not True:
        raise RuntimeError(
            "Generate-only ist vorbereitet, aber nicht freigegeben: "
            "allow_meshy_api_calls=false."
        )
    if job["meshy_auto_rigging_enabled"] is not False:
        raise RuntimeError("Generate-only verlangt meshy_auto_rigging_enabled=false.")
    validate_generate_only_config(job)
    try:
        credit_limit = float(job["meshy_max_credit_budget"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "meshy_max_credit_budget fehlt oder ist ungültig; "
            "ohne numerisches Creditlimit wird kein POST ausgeführt."
        ) from exc
    if credit_limit <= 0 or credit_limit > 30:
        raise RuntimeError(
            f"Unsicheres Creditlimit: {credit_limit:g}. "
            "Für diesen Lauf sind höchstens 30 Credits freigegeben."
        )

    fingerprints = input_fingerprints(inputs)
    status = status_data(job_dir)
    stored_fingerprints = status.get("meshy_generation_input_files")
    if stored_fingerprints and stored_fingerprints != fingerprints:
        raise RuntimeError(
            "Die Eingabebilder haben sich seit der Task-Freigabe verändert. "
            "Es wurde kein POST ausgeführt; eine neue ausdrückliche Freigabe ist erforderlich."
        )

    directories["meshy_raw"].mkdir(parents=True, exist_ok=True)
    directories["meshy_previews"].mkdir(parents=True, exist_ok=True)
    create_log = directories["logs"] / "meshy_generation_create.json"
    poll_log = directories["logs"] / "meshy_generation_poll.log"
    task_id = status.get("meshy_generation_task_id") or recover_created_task(create_log)
    tasks_created = 0
    if task_id:
        update_status_data(
            job_dir,
            {
                "meshy_generation_task_id": task_id,
                "meshy_generation_input_files": fingerprints,
            },
        )

    base = filename_base(job)
    raw_result_path = directories["meshy_raw"] / "meshy_generation_result.json"
    local_models = [
        directories["meshy_raw"] / f"{base}_meshy_raw.glb",
        directories["meshy_raw"] / f"{base}_meshy_raw.fbx",
    ]

    os.environ["MESHY_NETWORK_BLOCKED"] = "0"
    os.environ["MESHY_POSTS_BLOCKED"] = "0"
    os.environ["MESHY_RIGGING_BLOCKED"] = "1"
    load_dotenv(PIPELINE_DIR / ".env")
    api_key = os.getenv("MESHY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"MESHY_API_KEY fehlt in {PIPELINE_DIR / '.env'}")
    client = MeshyClient(api_key, directories["logs"])
    balance_before_payload = client.get_balance("meshy_balance_before.json")
    balance_before = balance_value(balance_before_payload)
    update_status_data(
        job_dir,
        {
            "job_id": job["job_id"],
            "mode": "generate-only",
            "status": "MESHY_GENERATION_AUTHORIZED",
            "authorized_credit_limit": credit_limit,
            "balance_before": balance_before,
            "meshy_generation_task_id": task_id,
            "meshy_generation_tasks_created": int(
                status.get("meshy_generation_tasks_created", 0)
            ),
            "meshy_generation_input_files": fingerprints,
            "meshy_rigging_tasks_created": 0,
            "automatic_retries": 0,
            "started_at": utc_now(),
            "errors": [],
        },
    )

    outputs: list[Path] = []
    warnings: list[str] = []
    operation_error: Exception | None = None
    try:
        if not task_id and balance_before < credit_limit:
            raise RuntimeError(
                f"Unzureichendes Meshy-Guthaben: {balance_before:g} Credits verfügbar, "
                f"{credit_limit:g} Credits als Sicherheitsreserve erforderlich. "
                "Es wurde kein Generierungs-Task erstellt."
            )

        if task_id and all(
            path.is_file() and path.stat().st_size > 0 for path in local_models
        ):
            outputs.extend(local_models)
            warnings.append(
                "Vorhandene lokale Meshy-Ausgaben wurden weiterverwendet; "
                "kein neuer POST wurde ausgeführt."
            )
        else:
            if not task_id:
                if int(status.get("meshy_generation_tasks_created", 0)) >= 1:
                    raise RuntimeError(
                        "Für diese Freigabe wurde bereits ein Multi-Image-to-3D-Task "
                        "erstellt. Ohne gespeicherte Task-ID wird kein neuer POST ausgeführt."
                    )
                # Unmittelbare letzte Prüfung vor dem einzigen kostenpflichtigen POST.
                current_hashes_match_job(job, inputs)
                fingerprints = input_fingerprints(inputs)
                task_id = client.create_task(
                    "multi-image-to-3d",
                    generation_payload(job, inputs),
                    create_log.name,
                )
                tasks_created = 1
                update_status_data(
                    job_dir,
                    {
                        "job_id": job["job_id"],
                        "mode": "generate-only",
                        "status": "MESHY_GENERATION_RUNNING",
                        "authorized_credit_limit": credit_limit,
                        "balance_before": balance_before,
                        "meshy_generation_task_id": task_id,
                        "meshy_generation_tasks_created": 1,
                        "meshy_generation_input_files": fingerprints,
                        "meshy_rigging_tasks_created": 0,
                        "automatic_retries": 0,
                        "started_at": utc_now(),
                        "errors": [],
                    },
                )

            result = client.poll_task(
                "multi-image-to-3d",
                str(task_id),
                45,
                raw_result_path,
                poll_log_path=poll_log,
                redact_urls_in_result=True,
            )
            model_outputs, model_warnings = download_generate_only_models(
                client, result, directories, base
            )
            outputs.extend(model_outputs)
            warnings.extend(model_warnings)
            texture_outputs, texture_warnings = download_generate_only_textures(
                client, result, directories, base
            )
            outputs.extend(texture_outputs)
            warnings.extend(texture_warnings)
            preview_outputs, preview_warnings = download_generate_only_previews(
                client, result, directories
            )
            outputs.extend(preview_outputs)
            warnings.extend(preview_warnings)
            outputs.append(raw_result_path)
    except Exception as exc:
        operation_error = exc

    balance_after = balance_before
    try:
        balance_after_payload = client.get_balance("meshy_balance_after.json")
        balance_after = balance_value(balance_after_payload)
    except Exception as balance_exc:
        if operation_error is None:
            operation_error = RuntimeError(
                f"Balance-Abfrage nach dem Lauf fehlgeschlagen: {balance_exc}"
            )
        else:
            warnings.append(f"Balance-Abfrage nach dem Fehler fehlgeschlagen: {balance_exc}")
    consumed_credits = round(max(0.0, balance_before - balance_after), 6)
    if consumed_credits > credit_limit:
        warnings.append(
            f"Der gemessene Verbrauch von {consumed_credits:g} Credits liegt über "
            f"dem freigegebenen Limit von {credit_limit:g}."
        )

    if operation_error is not None:
        update_status_data(
            job_dir,
            {
                "job_id": job["job_id"],
                "mode": "generate-only",
                "status": "MESH_GENERATION_FAILED",
                "finished_at": utc_now(),
                "authorized_credit_limit": credit_limit,
                "balance_before": balance_before,
                "balance_after": balance_after,
                "consumed_credits": consumed_credits,
                "meshy_generation_task_id": task_id,
                "meshy_generation_tasks_created": int(
                    status.get("meshy_generation_tasks_created", 0)
                )
                + tasks_created,
                "meshy_rigging_tasks_created": 0,
                "automatic_retries": 0,
                "meshy_generation_input_files": fingerprints,
                "output_files": [str(path) for path in outputs],
                "warnings": warnings,
                "errors": [str(operation_error)],
            },
        )
        raise operation_error

    reports = write_mesh_generation_report(
        job,
        directories,
        str(task_id),
        inputs,
        outputs,
        warnings,
        balance_before,
        balance_after,
        consumed_credits,
        credit_limit,
    )
    outputs.extend(reports)
    update_status_data(
        job_dir,
        {
            "job_id": job["job_id"],
            "mode": "generate-only",
            "status": "MESH_GENERATED_AWAITING_REVIEW",
            "finished_at": utc_now(),
            "authorized_credit_limit": credit_limit,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "consumed_credits": consumed_credits,
            "meshy_generation_task_id": task_id,
            "meshy_generation_tasks_created": int(
                status.get("meshy_generation_tasks_created", 0)
            )
            + tasks_created,
            "meshy_rigging_tasks_created": 0,
            "automatic_retries": 0,
            "meshy_generation_input_files": fingerprints,
            "output_files": [str(path) for path in outputs],
            "warnings": warnings,
            "errors": [],
        },
    )
    return outputs, tasks_created, warnings


def assert_rigging_allowed(job: dict[str, Any]) -> None:
    if job.get("meshy_auto_rigging_enabled") is False:
        raise RuntimeError(RIGGING_BLOCK_MESSAGE)


def download_generation(
    client: MeshyClient,
    result: dict[str, Any],
    directories: dict[str, Path],
    base: str,
) -> None:
    urls = result.get("model_urls") or {}
    destinations = {
        "glb": directories["work"] / f"{base}_generated.glb",
        "fbx": directories["work"] / f"{base}_generated.fbx",
    }
    for format_name, destination in destinations.items():
        if urls.get(format_name):
            client.download(urls[format_name], destination)
    if not any(path.is_file() and path.stat().st_size > 0 for path in destinations.values()):
        raise MeshyError("Meshy-Ergebnis enthält weder eine lokale GLB- noch FBX-Datei.")


def download_rigging(
    client: MeshyClient,
    result: dict[str, Any],
    directories: dict[str, Path],
    base: str,
) -> Path:
    payload = result.get("result") or {}
    destinations = {
        "rigged_character_glb_url": directories["work"] / f"{base}_rigged_source.glb",
        "rigged_character_fbx_url": directories["work"] / f"{base}_rigged_source.fbx",
    }
    for key, destination in destinations.items():
        if payload.get(key):
            client.download(payload[key], destination)
    for key in ("rigged_character_glb_url", "rigged_character_fbx_url"):
        path = destinations[key]
        if path.is_file() and path.stat().st_size > 0:
            return path
    raise MeshyError("Rigging-Ergebnis enthält weder eine rigged GLB- noch FBX-Datei.")


def execute_meshy(
    job: dict[str, Any],
    directories: dict[str, Path],
    inputs: list[Path],
) -> tuple[Path, int]:
    assert_rigging_allowed(job)
    if job["allow_meshy_api_calls"] is not True:
        raise RuntimeError(
            "Meshy-Aufrufe sind für diesen Job deaktiviert "
            "(allow_meshy_api_calls=false)."
        )
    os.environ["MESHY_NETWORK_BLOCKED"] = "0"
    os.environ["MESHY_POSTS_BLOCKED"] = "0"
    load_dotenv(PIPELINE_DIR / ".env")
    api_key = os.getenv("MESHY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"MESHY_API_KEY fehlt in {PIPELINE_DIR / '.env'}")

    base = filename_base(job)
    state = load_pipeline_state(directories)
    client = MeshyClient(api_key, directories["logs"])
    tasks_created = 0

    generation_log = directories["logs"] / f"{base}_meshy_generation_create.json"
    generation_id = state.get("generation_task_id") or recover_created_task(generation_log)
    if not generation_id:
        if int(state.get("generation_tasks_created", 0)) >= 1:
            raise RuntimeError("Für diesen Job wurde bereits ein Generierungs-Task erstellt.")
        payload = {
            "image_urls": [image_data_uri(path) for path in inputs],
            "ai_model": "latest",
            "should_texture": True,
            "enable_pbr": True,
            "texture_resolution": "2k",
            "texture_prompt": str(
                job.get(
                    "texture_prompt",
                    "Game-ready character materials matching the supplied reference images.",
                )
            ),
            "should_remesh": True,
            "topology": "quad",
            "target_polycount": int(job.get("target_polycount", 60000)),
            "pose_mode": str(job.get("pose_mode", "a-pose")),
            "remove_lighting": True,
            "target_formats": ["glb", "fbx"],
        }
        generation_id = client.create_task(
            "multi-image-to-3d",
            payload,
            generation_log.name,
        )
        state["generation_task_id"] = generation_id
        state["generation_tasks_created"] = 1
        tasks_created += 1
        save_pipeline_state(directories, state)
    else:
        state["generation_task_id"] = generation_id
        save_pipeline_state(directories, state)

    generation_result = client.poll_task(
        "multi-image-to-3d",
        generation_id,
        45,
        directories["work"] / f"{base}_meshy_generation_result.json",
    )
    download_generation(client, generation_result, directories, base)

    rigging_log = directories["logs"] / f"{base}_meshy_rigging_create.json"
    rigging_id = state.get("rigging_task_id") or recover_created_task(rigging_log)
    if not rigging_id:
        if int(state.get("rigging_tasks_created", 0)) >= 1:
            raise RuntimeError("Für diesen Job wurde bereits ein Rigging-Task erstellt.")
        rigging_id = client.create_task(
            "rigging",
            {
                "input_task_id": generation_id,
                "height_meters": float(job["character_height_meters"]),
            },
            rigging_log.name,
        )
        state["rigging_task_id"] = rigging_id
        state["rigging_tasks_created"] = 1
        tasks_created += 1
        save_pipeline_state(directories, state)
    else:
        state["rigging_task_id"] = rigging_id
        save_pipeline_state(directories, state)

    rigging_result = client.poll_task(
        "rigging",
        rigging_id,
        30,
        directories["work"] / f"{base}_meshy_rigging_result.json",
    )
    return download_rigging(client, rigging_result, directories, base), tasks_created


def find_local_model(job: dict[str, Any], directories: dict[str, Path]) -> Path:
    base = filename_base(job)
    candidates: list[Path] = []
    if job.get("local_model_path"):
        candidates.append(Path(str(job["local_model_path"])))
    candidates.extend(
        [
            directories["work"] / f"{base}_rigged_source.glb",
            directories["work"] / f"{base}_rigged_source.fbx",
            directories["work"] / f"{job['character_id']}_rigged_source.glb",
            directories["work"] / f"{job['character_id']}_rigged_source.fbx",
            directories["exports"] / f"{base}_rigged.glb",
            directories["input"] / f"{base}_rigged.glb",
            directories["input"] / f"{base}_rigged.fbx",
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    checked = "\n- ".join(str(path) for path in candidates)
    raise RuntimeError(f"Kein vorhandenes lokales Modell gefunden. Geprüft:\n- {checked}")


def run_blender(
    blender: Path,
    source: Path,
    job: dict[str, Any],
    directories: dict[str, Path],
) -> None:
    script = PIPELINE_DIR / "blender" / "process_character.py"
    command = [
        str(blender),
        "--background",
        "--python",
        str(script),
        "--",
        "--source",
        str(source),
        "--character-id",
        str(job["character_id"]),
        "--version",
        str(job["version"]),
        "--height",
        str(job["character_height_meters"]),
        "--blender-dir",
        str(directories["blender"]),
        "--exports-dir",
        str(directories["exports"]),
        "--previews-dir",
        str(directories["previews"]),
        "--reports-dir",
        str(directories["reports"]),
    ]
    log_path = directories["logs"] / f"{filename_base(job)}_blender.log"
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        result = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=60 * 60,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"Blender-Verarbeitung fehlgeschlagen (Exit-Code {result.returncode}). "
            f"Log: {log_path}"
        )


def validate_exports(job: dict[str, Any], directories: dict[str, Path]) -> list[Path]:
    base = filename_base(job)
    required = [
        directories["blender"] / f"{base}.blend",
        directories["exports"] / f"SK_{base}.fbx",
        directories["exports"] / f"{base}_rigged.glb",
        *(directories["previews"] / f"{base}_{view}.png"
          for view in ("front", "left", "back", "three_quarter")),
        directories["reports"] / f"{base}_character_validation.json",
        directories["reports"] / f"{base}_character_validation.md",
    ]
    missing = [
        str(path)
        for path in required
        if not path.is_file() or path.stat().st_size <= 0
    ]
    if missing:
        raise RuntimeError("Lokale Ausgabe fehlt oder ist leer:\n- " + "\n- ".join(missing))
    report = load_json(directories["reports"] / f"{base}_character_validation.json")
    if report.get("export_successful") is not True:
        raise RuntimeError("Blender-Validierungsbericht meldet export_successful=false.")
    return required


def copy_unreal_ready(
    job: dict[str, Any],
    directories: dict[str, Path],
) -> list[Path]:
    base = filename_base(job)
    ready = directories["unreal_ready"]
    ready.mkdir(parents=True, exist_ok=True)
    ready_textures = ready / "Textures"
    ready_textures.mkdir(parents=True, exist_ok=True)
    sources = [
        directories["exports"] / f"SK_{base}.fbx",
        directories["exports"] / f"{base}_rigged.glb",
    ]
    outputs: list[Path] = []
    for source in sources:
        destination = ready / source.name
        shutil.copy2(source, destination)
        outputs.append(destination)
    for source in sorted(directories["textures"].iterdir()):
        if source.is_file():
            destination = ready_textures / source.name
            shutil.copy2(source, destination)
            outputs.append(destination)

    guide = ready / "IMPORT_IN_UNREAL.md"
    guide.write_text(
        "\n".join(
            [
                f"# Manueller Unreal-Import – {base}",
                "",
                "Der automatische Unreal-Import ist deaktiviert.",
                "",
                f"1. Importiere `SK_{base}.fbx` im Unreal Content Browser.",
                "2. Wähle beim Import das gewünschte Ziel-Skeleton manuell.",
                f"3. Skelett-Referenz: `{job['target_skeleton_reference']}`",
                "4. Prüfe Materialien und Texturen aus `Textures`.",
                f"5. Optional steht `{base}_rigged.glb` als Kontrollformat bereit.",
                "",
                "Diese Pipeline verändert keine Unreal-Assets.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    outputs.append(guide)

    manifest = ready / "manifest.json"
    manifest_payload = {
        "job_id": job["job_id"],
        "character_id": job["character_id"],
        "version": job["version"],
        "automatic_unreal_import": False,
        "target_skeleton_reference": job["target_skeleton_reference"],
        "files": [
            {
                "relative_path": str(path.relative_to(ready)),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in outputs
        ],
        "created_at": utc_now(),
    }
    save_json(manifest, manifest_payload)
    outputs.append(manifest)
    return outputs


def report_path(
    job: dict[str, Any],
    directories: dict[str, Path],
    mode: str,
) -> Path:
    safe_mode = mode.replace("-", "_")
    path = directories["reports"] / f"{filename_base(job)}_{safe_mode}_abschlussbericht.md"
    if path.exists():
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = path.with_name(f"{path.stem}_{timestamp}{path.suffix}")
    return path


def write_report(
    job: dict[str, Any],
    directories: dict[str, Path],
    mode: str,
    status: str,
    input_paths: list[Path],
    output_paths: list[Path],
    errors: list[str],
    warnings: list[str],
    meshy_tasks_created: int,
) -> Path:
    path = report_path(job, directories, mode)
    content = [
        f"# Job-Abschlussbericht – {job.get('job_id', directories['reports'].parent.name)}",
        "",
        f"- Modus: {MODE_LABELS[mode]}",
        f"- Status: {status}",
        f"- Charakter: {job.get('character_id', 'FEHLT')}",
        f"- Version: {job.get('version', 'FEHLT')}",
        f"- Meshy-Tasks in diesem Lauf: {meshy_tasks_created}",
        "- Automatischer Unreal-Import: deaktiviert",
        "",
        "## Eingaben",
        "",
    ]
    content.extend(f"- `{path}`" for path in input_paths)
    content.extend(["", "## Ausgaben", ""])
    content.extend([f"- `{path}`" for path in output_paths] or ["- keine"])
    content.extend(["", "## Warnungen", ""])
    content.extend([f"- {item}" for item in warnings] or ["- keine"])
    content.extend(["", "## Fehler", ""])
    content.extend([f"- {item}" for item in errors] or ["- keine"])
    path.write_text("\n".join(content) + "\n", encoding="utf-8")
    return path


def update_job_status(job_dir: Path, job: dict[str, Any], status: str, mode: str) -> None:
    job["status"] = status
    job["last_mode"] = mode
    job["last_finished_at"] = utc_now()
    save_json(job_dir / "job.json", job)


def write_status(
    job_dir: Path,
    job: dict[str, Any],
    mode: str,
    started_at: str,
    finished_at: str | None,
    status: str,
    meshy_tasks_created: int,
    input_paths: list[Path],
    output_paths: list[Path],
    errors: list[str],
    warnings: list[str],
) -> None:
    payload = {
        "job_id": job.get("job_id", job_dir.name),
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "meshy_tasks_created": meshy_tasks_created,
        "consumed_credits": 0 if meshy_tasks_created == 0 else "nicht lokal ermittelbar",
        "input_files": [str(path) for path in input_paths],
        "output_files": [str(path) for path in output_paths],
        "errors": errors,
        "warnings": warnings,
    }
    save_json(job_dir / "status.json", payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Isekai Ascension Workspace Character Pipeline")
    job_group = parser.add_mutually_exclusive_group(required=True)
    job_group.add_argument("--job", help="Vollständiger Pfad eines vorhandenen Jobordners.")
    job_group.add_argument(
        "--latest-job",
        action="store_true",
        help="Jobpfad ausschließlich aus 00_START_HERE\\LATEST_JOB.txt lesen.",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--preflight", action="store_true")
    mode_group.add_argument("--execute", action="store_true")
    mode_group.add_argument("--resume-local", action="store_true")
    mode_group.add_argument("--export-only", action="store_true")
    mode_group.add_argument("--generate-only", action="store_true")
    mode_group.add_argument("--plan-generate-only", action="store_true")
    args = parser.parse_args()

    if args.preflight:
        mode = "preflight"
    elif args.execute:
        mode = "execute"
    elif args.resume_local:
        mode = "resume-local"
    elif args.export_only:
        mode = "export-only"
    elif args.generate_only:
        mode = "generate-only"
    else:
        mode = "plan-generate-only"
    started_at = utc_now()
    os.environ["MESHY_POSTS_BLOCKED"] = "1"
    os.environ["MESHY_NETWORK_BLOCKED"] = "1"
    os.environ["MESHY_RIGGING_BLOCKED"] = "1"
    job_dir: Path | None = None
    job: dict[str, Any] = {}
    directories: dict[str, Path] | None = None
    input_paths: list[Path] = []
    output_paths: list[Path] = []
    warnings: list[str] = []
    errors: list[str] = []
    meshy_tasks_created = 0

    print(f"{MODE_LABELS[mode]} MODE")
    if mode not in {"execute", "generate-only"}:
        print("Meshy-Aufrufe blockiert.")
        print("Zusätzliche Meshy-Credits: 0")
    print("Automatischer Unreal-Import: deaktiviert")

    try:
        workspace_root, workspace = load_workspace()
        config = load_json(CONFIG_PATH) if CONFIG_PATH.is_file() else {}
        job_dir = resolve_job(args, workspace_root, workspace)
        job = load_job(job_dir)
        if job.get("pipeline_version") == "geometry-remesh-retexture-v1":
            raise RuntimeError(
                "Dieser neue Job verwendet geometry-remesh-retexture-v1. "
                "Legacy-Modi mit Blender-Baking, Sculpting, automatischer "
                "Texturprojektion, Merge by Distance oder alten Meshy-Texturen "
                "sind gesperrt. Verwende --plan-final-mesh oder --build-final-mesh."
            )
        validate_required_fields(job)
        directories = job_directories(job_dir, job)
        input_paths, warnings = validate_inputs(job, directories, config)
        if job.get("meshy_auto_rigging_enabled") is False:
            os.environ["MESHY_RIGGING_BLOCKED"] = "1"
        if mode in {"generate-only", "plan-generate-only"}:
            validate_generate_only_config(job)
            input_paths = generation_input_paths(job, directories)

        if mode == "plan-generate-only":
            os.environ["MESHY_POSTS_BLOCKED"] = "1"
            os.environ["MESHY_NETWORK_BLOCKED"] = "1"
            os.environ["MESHY_RIGGING_BLOCKED"] = "1"
            plan_paths = write_generate_only_plan(job, directories, input_paths)
            print(f"Plan JSON: {plan_paths[0]}")
            print(f"Plan Markdown: {plan_paths[1]}")
            return 0

        if mode == "generate-only":
            generated_paths, meshy_tasks_created, generation_warnings = run_generate_only(
                job_dir,
                job,
                directories,
                input_paths,
            )
            warnings.extend(generation_warnings)
            status = "MESH_GENERATED_AWAITING_REVIEW"
            update_job_status(job_dir, job, status, mode)
            print(f"Job: {job_dir}")
            print(f"Status: {status}")
            print(f"Meshy-Tasks erstellt: {meshy_tasks_created}")
            print("Rigging-Tasks erstellt: 0")
            print("Blender gestartet: Nein")
            print("Unreal gestartet: Nein")
            for path in generated_paths:
                print(f"Ausgabe: {path}")
            return 0

        write_status(
            job_dir,
            job,
            mode,
            started_at,
            None,
            "RUNNING",
            0,
            input_paths,
            [],
            [],
            warnings,
        )

        if mode == "preflight":
            if job["allow_meshy_api_calls"] is False:
                warnings.append("Meshy ist für diesen Job ausdrücklich deaktiviert.")
            if locate_blender(workspace) is None:
                warnings.append(
                    f"Blender wurde am konfigurierten Pfad nicht gefunden: "
                    f"{workspace.get('blender_exe', '')}"
                )
            status = "PREFLIGHT_OK"
            update_job_status(job_dir, job, status, mode)
        else:
            blender = locate_blender(workspace)
            if blender is None:
                raise RuntimeError(
                    f"Blender wurde am konfigurierten Pfad nicht gefunden: "
                    f"{workspace.get('blender_exe', '')}"
                )
            if mode == "execute":
                source, meshy_tasks_created = execute_meshy(
                    job,
                    directories,
                    input_paths,
                )
            else:
                source = find_local_model(job, directories)
            run_blender(blender, source, job, directories)
            output_paths.extend(validate_exports(job, directories))
            output_paths.extend(copy_unreal_ready(job, directories))
            status = "COMPLETED"
            update_job_status(job_dir, job, status, mode)

        report = write_report(
            job,
            directories,
            mode,
            status,
            input_paths,
            output_paths,
            errors,
            warnings,
            meshy_tasks_created,
        )
        output_paths.append(report)
        finished_at = utc_now()
        write_status(
            job_dir,
            job,
            mode,
            started_at,
            finished_at,
            status,
            meshy_tasks_created,
            input_paths,
            output_paths,
            errors,
            warnings,
        )
        print(f"Job: {job_dir}")
        print(f"Status: {status}")
        print(f"Bericht: {report}")
        print(f"Meshy-Tasks erstellt: {meshy_tasks_created}")
        print("Automatischer Unreal-Import ausgeführt: Nein")
        return 0
    except Exception as exc:
        message = str(exc)
        errors.append(message)
        print(message, file=sys.stderr)
        if job_dir is not None and job:
            if mode == "generate-only":
                try:
                    update_job_status(job_dir, job, "FAILED", mode)
                    current_status = status_data(job_dir)
                    current_errors = list(current_status.get("errors") or [])
                    if message not in current_errors:
                        current_errors.append(message)
                    update_status_data(
                        job_dir,
                        {
                            "job_id": job.get("job_id", job_dir.name),
                            "mode": mode,
                            "status": "MESH_GENERATION_FAILED",
                            "finished_at": utc_now(),
                            "meshy_rigging_tasks_created": 0,
                            "automatic_retries": 0,
                            "errors": current_errors,
                        },
                    )
                except Exception as status_exc:
                    print(
                        f"Generate-only-Fehlerstatus konnte nicht geschrieben werden: "
                        f"{status_exc}",
                        file=sys.stderr,
                    )
                return 1
            try:
                update_job_status(job_dir, job, "FAILED", mode)
            except Exception as status_exc:
                errors.append(f"job.json konnte nicht auf FAILED gesetzt werden: {status_exc}")
            if directories is None:
                try:
                    directories = job_directories(job_dir, job)
                except Exception:
                    directories = None
            if directories is not None:
                try:
                    report = write_report(
                        job,
                        directories,
                        mode,
                        "FAILED",
                        input_paths,
                        output_paths,
                        errors,
                        warnings,
                        meshy_tasks_created,
                    )
                    output_paths.append(report)
                except Exception as report_exc:
                    errors.append(f"Abschlussbericht konnte nicht geschrieben werden: {report_exc}")
            try:
                write_status(
                    job_dir,
                    job,
                    mode,
                    started_at,
                    utc_now(),
                    "FAILED",
                    meshy_tasks_created,
                    input_paths,
                    output_paths,
                    errors,
                    warnings,
                )
            except Exception as status_exc:
                print(f"status.json konnte nicht geschrieben werden: {status_exc}", file=sys.stderr)
        return 1
    finally:
        if mode == "generate-only" and job_dir is not None and job:
            try:
                job["allow_meshy_api_calls"] = False
                save_json(job_dir / "job.json", job)
            except Exception as permission_exc:
                print(
                    f"KRITISCH: allow_meshy_api_calls konnte nicht auf false "
                    f"zurückgesetzt werden: {permission_exc}",
                    file=sys.stderr,
                )
        os.environ["MESHY_POSTS_BLOCKED"] = "1"
        os.environ["MESHY_NETWORK_BLOCKED"] = "1"
        os.environ["MESHY_RIGGING_BLOCKED"] = "1"


if __name__ == "__main__":
    if any(
        argument in {"--plan-final-mesh", "--build-final-mesh"}
        for argument in sys.argv[1:]
    ):
        from final_mesh_pipeline import main as final_mesh_main

        raise SystemExit(final_mesh_main())
    raise SystemExit(main())
