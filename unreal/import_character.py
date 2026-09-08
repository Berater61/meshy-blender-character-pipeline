from __future__ import annotations

import json
import os
from pathlib import Path

import unreal


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Umgebungsvariable fehlt: {name}")
    return value


def main() -> None:
    source_fbx = Path(require_env("CHARACTER_FBX"))
    character_id = require_env("CHARACTER_ID")
    destination_root = require_env("UNREAL_CONTENT_DESTINATION").rstrip("/")
    report_path = Path(require_env("UNREAL_IMPORT_REPORT"))
    destination_mesh = f"{destination_root}/Mesh"

    if not source_fbx.is_file():
        raise RuntimeError(f"FBX-Datei fehlt: {source_fbx}")

    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    editor_library = unreal.EditorAssetLibrary
    for path in [
        destination_root,
        destination_mesh,
        f"{destination_root}/Materials",
        f"{destination_root}/Textures",
        f"{destination_root}/Physics",
    ]:
        editor_library.make_directory(path)

    import_data = unreal.FbxSkeletalMeshImportData()
    import_data.set_editor_property("import_mesh_lods", False)
    import_data.set_editor_property("use_t0_as_ref_pose", False)

    options = unreal.FbxImportUI()
    options.set_editor_property("import_mesh", True)
    options.set_editor_property("import_as_skeletal", True)
    options.set_editor_property("import_animations", False)
    options.set_editor_property("import_materials", True)
    options.set_editor_property("import_textures", True)
    options.set_editor_property("create_physics_asset", True)
    options.set_editor_property("skeleton", None)
    options.set_editor_property("skeletal_mesh_import_data", import_data)

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", str(source_fbx))
    task.set_editor_property("destination_path", destination_mesh)
    task.set_editor_property("destination_name", f"SK_{character_id}")
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", False)
    task.set_editor_property("save", True)
    task.set_editor_property("options", options)

    asset_tools.import_asset_tasks([task])
    imported = list(task.get_editor_property("imported_object_paths"))
    if not imported:
        raise RuntimeError("Unreal hat keine Assets importiert.")
    for path in imported:
        editor_library.save_asset(path, only_if_is_dirty=False)

    report = {
        "status": "Erfolgreich",
        "source_fbx": str(source_fbx),
        "destination": destination_mesh,
        "imported_assets": imported,
        "existing_assets_replaced": False,
        "animations_imported": False,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    unreal.log(f"Character-Import erfolgreich: {imported}")


try:
    main()
except Exception as exc:
    unreal.log_error(f"Character-Import fehlgeschlagen: {exc}")
    raise
