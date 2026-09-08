from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector


def parse_args() -> argparse.Namespace:
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--character-id", required=True)
    parser.add_argument("--version")
    parser.add_argument("--height", required=True, type=float)
    parser.add_argument("--output-root")
    parser.add_argument("--blender-dir")
    parser.add_argument("--exports-dir")
    parser.add_argument("--previews-dir")
    parser.add_argument("--reports-dir")
    return parser.parse_args(arguments)


def world_bounds(meshes: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    if not points:
        raise RuntimeError("Das importierte Modell besitzt keine auswertbare Bounding Box.")
    minimum = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    maximum = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return minimum, maximum


def imported_roots() -> list[bpy.types.Object]:
    return [obj for obj in bpy.context.scene.objects if obj.parent is None]


def remove_known_helper_meshes() -> list[str]:
    removed: list[str] = []
    for obj in list(bpy.context.scene.objects):
        if obj.type != "MESH" or not obj.name.casefold().startswith("icosphere"):
            continue
        has_armature = any(modifier.type == "ARMATURE" for modifier in obj.modifiers)
        has_armature_parent = obj.parent is not None and obj.parent.type == "ARMATURE"
        if (
            not has_armature
            and not has_armature_parent
            and not obj.vertex_groups
            and not obj.material_slots
        ):
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    return removed


def scale_and_ground(meshes: list[bpy.types.Object], target_height: float) -> None:
    minimum, maximum = world_bounds(meshes)
    height = maximum.z - minimum.z
    if height <= 0:
        raise RuntimeError("Charakterhöhe konnte nicht bestimmt werden.")
    factor = target_height / height
    for obj in imported_roots():
        obj.scale *= factor
    bpy.context.view_layer.update()
    minimum, maximum = world_bounds(meshes)
    center_x = (minimum.x + maximum.x) / 2
    center_y = (minimum.y + maximum.y) / 2
    offset = Vector((-center_x, -center_y, -minimum.z))
    for obj in imported_roots():
        obj.location += offset
    bpy.context.view_layer.update()


def normalize_weights(obj: bpy.types.Object) -> int:
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="OBJECT")
    if obj.vertex_groups:
        bpy.ops.object.vertex_group_normalize_all(lock_active=False)
        bpy.ops.object.vertex_group_limit_total(limit=4)
        used = {group.group for vertex in obj.data.vertices for group in vertex.groups}
        for group in list(obj.vertex_groups):
            if group.index not in used:
                obj.vertex_groups.remove(group)
    maximum = 0
    for vertex in obj.data.vertices:
        maximum = max(maximum, len(vertex.groups))
    obj.select_set(False)
    return maximum


def inspect_and_clean_mesh(obj: bpy.types.Object) -> dict:
    mesh = obj.data
    mesh.validate(verbose=False, clean_customdata=False)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.normal_update()
    non_manifold = sum(1 for edge in bm.edges if not edge.is_manifold)
    loose_vertices = sum(1 for vertex in bm.verts if not vertex.link_edges)
    bm.to_mesh(mesh)
    bm.free()

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        bpy.ops.object.material_slot_remove_unused()
    except RuntimeError:
        pass
    obj.select_set(False)
    maximum_influences = normalize_weights(obj)
    triangles = sum(max(0, len(polygon.vertices) - 2) for polygon in mesh.polygons)
    return {
        "vertices": len(mesh.vertices),
        "triangles": triangles,
        "material_slots": len(obj.material_slots),
        "uv_maps": len(mesh.uv_layers),
        "non_manifold_edges": non_manifold,
        "loose_vertices": loose_vertices,
        "maximum_bone_influences": maximum_influences,
        "armature_modifier": any(mod.type == "ARMATURE" for mod in obj.modifiers),
    }


def export_packed_textures(
    meshes: list[bpy.types.Object], destination: Path, filename_prefix: str
) -> tuple[list[str], list[str]]:
    destination.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    material_issues: list[str] = []
    seen: set[str] = set()
    for obj in meshes:
        for slot_index, slot in enumerate(obj.material_slots):
            material = getattr(slot, "material", None)
            slot_label = f"{obj.name}: Material-Slot {slot_index}"
            if material is None:
                material_issues.append(f"{slot_label} ist leer.")
                continue
            node_tree = getattr(material, "node_tree", None)
            if node_tree is None:
                material_issues.append(
                    f"{material.name}: Nodes sind deaktiviert oder der Node-Tree fehlt."
                )
                continue
            nodes = list(getattr(node_tree, "nodes", []))
            if not any(getattr(node, "type", "") == "BSDF_PRINCIPLED" for node in nodes):
                material_issues.append(f"{material.name}: Principled-BSDF-Node fehlt.")
            image_nodes = [
                node
                for node in nodes
                if getattr(node, "type", "") == "TEX_IMAGE"
                and getattr(node, "image", None) is not None
            ]
            if not image_nodes:
                material_issues.append(f"{material.name}: Keine Bildtextur gefunden.")
                continue
            for node in image_nodes:
                image = getattr(node, "image", None)
                if image is None:
                    continue
                if image.name in seen:
                    continue
                seen.add(image.name)
                image_path = getattr(image, "filepath", "") or ""
                suffix = Path(image_path).suffix or ".png"
                target = destination / f"{filename_prefix}_{Path(image.name).stem}{suffix}"
                try:
                    if getattr(image, "packed_file", None):
                        image.save_render(str(target))
                    elif image_path:
                        source = Path(bpy.path.abspath(image_path))
                        if source.is_file():
                            target.write_bytes(source.read_bytes())
                            image.filepath = str(target)
                        else:
                            missing.append(image.name)
                    else:
                        missing.append(image.name)
                except Exception:
                    missing.append(image.name)
    return sorted(set(missing)), sorted(set(material_issues))


def select_character(meshes: list[bpy.types.Object], armatures: list[bpy.types.Object]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes + armatures:
        obj.select_set(True)
    if armatures:
        bpy.context.view_layer.objects.active = armatures[0]


def load_preview_module() -> object:
    module_path = Path(__file__).with_name("render_validation_views.py")
    spec = importlib.util.spec_from_file_location("render_validation_views", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Preview-Modul konnte nicht geladen werden: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    args = parse_args()
    source = Path(args.source)
    filename_base = (
        f"{args.character_id}_{args.version}" if args.version else args.character_id
    )
    if args.output_root:
        output_root = Path(args.output_root)
        directories = {
            "blender": output_root / "Blender",
            "exports": output_root / "Exports",
            "textures": output_root / "Exports" / "Textures",
            "preview": output_root / "Preview",
            "reports": output_root / "Reports",
        }
    else:
        missing_arguments = [
            name
            for name, value in (
                ("--blender-dir", args.blender_dir),
                ("--exports-dir", args.exports_dir),
                ("--previews-dir", args.previews_dir),
                ("--reports-dir", args.reports_dir),
            )
            if not value
        ]
        if missing_arguments:
            raise RuntimeError(
                "Fehlende Workspace-Ausgabeordner: " + ", ".join(missing_arguments)
            )
        directories = {
            "blender": Path(args.blender_dir),
            "exports": Path(args.exports_dir),
            "textures": Path(args.exports_dir) / "Textures",
            "preview": Path(args.previews_dir),
            "reports": Path(args.reports_dir),
        }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    suffix = source.suffix.lower()
    if suffix == ".glb":
        bpy.ops.import_scene.gltf(filepath=str(source))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(source), automatic_bone_orientation=False)
    else:
        raise RuntimeError(f"Nicht unterstütztes Rigging-Format: {source}")

    for obj in list(bpy.context.scene.objects):
        if obj.type in {"CAMERA", "LIGHT"}:
            bpy.data.objects.remove(obj, do_unlink=True)

    removed_helper_meshes = remove_known_helper_meshes()
    if removed_helper_meshes:
        print(f"Entfernte ungebundene Helper-Meshes: {removed_helper_meshes}")

    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if not meshes:
        raise RuntimeError("Im Rigging-Ergebnis wurde kein Mesh gefunden.")
    if not armatures:
        raise RuntimeError("Im Rigging-Ergebnis wurde keine Armature gefunden.")

    armatures[0].name = f"ARM_{args.character_id}"
    for index, obj in enumerate(meshes):
        obj.name = f"SK_{args.character_id}" if index == 0 else f"SK_{args.character_id}_{index + 1:02d}"
    material_index = 1
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material:
                slot.material.name = f"M_{args.character_id}_{material_index:02d}"
                material_index += 1

    scale_and_ground(meshes, args.height)
    metrics = [inspect_and_clean_mesh(obj) for obj in meshes]
    minimum, maximum = world_bounds(meshes)
    roots = [bone for armature in armatures for bone in armature.data.bones if bone.parent is None]
    missing_textures, material_issues = export_packed_textures(
        meshes, directories["textures"], filename_base
    )

    preview = load_preview_module()
    render_engine = preview.render_views(
        meshes,
        directories["preview"],
        filename_prefix=f"{filename_base}_",
    )

    select_character(meshes, armatures)
    blend_path = directories["blender"] / f"{filename_base}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))

    glb_path = directories["exports"] / f"{filename_base}_rigged.glb"
    bpy.ops.export_scene.gltf(
        filepath=str(glb_path),
        export_format="GLB",
        use_selection=True,
        export_animations=False,
    )

    select_character(meshes, armatures)
    fbx_path = directories["exports"] / f"SK_{filename_base}.fbx"
    bpy.ops.export_scene.fbx(
        filepath=str(fbx_path),
        use_selection=True,
        object_types={"ARMATURE", "MESH"},
        axis_forward="-Y",
        axis_up="Z",
        add_leaf_bones=False,
        bake_anim=False,
        path_mode="COPY",
        embed_textures=False,
    )

    preview_paths = [
        directories["preview"] / f"{filename_base}_{name}.png"
        for name in ("front", "left", "back", "three_quarter")
    ]
    report = {
        "blender_version": bpy.app.version_string,
        "render_engine": render_engine,
        "removed_helper_meshes": removed_helper_meshes,
        "vertices": sum(item["vertices"] for item in metrics),
        "triangles": sum(item["triangles"] for item in metrics),
        "mesh_objects": len(meshes),
        "material_slots": sum(item["material_slots"] for item in metrics),
        "bones": sum(len(armature.data.bones) for armature in armatures),
        "root_bone_present": bool(roots),
        "root_bones": [bone.name for bone in roots],
        "maximum_bone_influences": max(item["maximum_bone_influences"] for item in metrics),
        "character_height_meters": maximum.z - minimum.z,
        "lowest_z": minimum.z,
        "highest_z": maximum.z,
        "uv_map_present": all(item["uv_maps"] > 0 for item in metrics),
        "missing_textures": missing_textures,
        "material_issues": material_issues,
        "non_manifold_edges": sum(item["non_manifold_edges"] for item in metrics),
        "loose_vertices": sum(item["loose_vertices"] for item in metrics),
        "armature_modifier_present": all(item["armature_modifier"] for item in metrics),
        "visible_eye_geometry": "unbekannt/manuelle Prüfung nötig",
        "visible_eyes_intentionally_added": False,
        "blend_file_nonempty": blend_path.is_file() and blend_path.stat().st_size > 0,
        "fbx_file_nonempty": fbx_path.is_file() and fbx_path.stat().st_size > 0,
        "glb_file_nonempty": glb_path.is_file() and glb_path.stat().st_size > 0,
        "preview_files_nonempty": all(
            path.is_file() and path.stat().st_size > 0 for path in preview_paths
        ),
        "export_successful": (
            fbx_path.is_file()
            and fbx_path.stat().st_size > 0
            and glb_path.is_file()
            and glb_path.stat().st_size > 0
        ),
    }
    json_path = directories["reports"] / f"{filename_base}_character_validation.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown = ["# Charakter-Validierung", ""]
    markdown.extend(f"- **{key}:** {value}" for key, value in report.items())
    (directories["reports"] / f"{filename_base}_character_validation.md").write_text(
        "\n".join(markdown) + "\n",
        encoding="utf-8",
    )
    print(f"Blender-Verarbeitung abgeschlossen: {fbx_path}")


if __name__ == "__main__":
    main()
