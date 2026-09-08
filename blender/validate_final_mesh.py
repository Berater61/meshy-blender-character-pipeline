from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def parse_args() -> argparse.Namespace:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--textures", required=True)
    parser.add_argument("--previews", required=True)
    return parser.parse_args(values)


def bounds(objects: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    points = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    return (
        Vector(tuple(min(point[index] for point in points) for index in range(3))),
        Vector(tuple(max(point[index] for point in points) for index in range(3))),
    )


def look_at(camera: bpy.types.Object, target: Vector) -> None:
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()


def add_area(name: str, location: tuple[float, float, float], energy: float, size: float, target: Vector) -> None:
    data = bpy.data.lights.new(name, "AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (target - obj.location).to_track_quat("-Z", "Y").to_euler()


def setup_render(meshes: list[bpy.types.Object], output_dir: Path) -> tuple[bpy.types.Scene, bpy.types.Object, Vector, float, float]:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    scene.world.color = (0.055, 0.055, 0.055)
    minimum, maximum = bounds(meshes)
    center = (minimum + maximum) * 0.5
    dimensions = maximum - minimum
    height = max(dimensions.z, 0.1)
    width = max(dimensions.x, 0.1)
    camera_data = bpy.data.cameras.new("FINAL_MESH_VALIDATION_CAMERA")
    camera_data.type = "ORTHO"
    camera = bpy.data.objects.new("FINAL_MESH_VALIDATION_CAMERA", camera_data)
    bpy.context.collection.objects.link(camera)
    scene.camera = camera
    add_area("Key", (center.x - width, center.y - height, center.z + height), 700, height, center)
    add_area("Fill", (center.x + width, center.y - height * 0.5, center.z + height * 0.25), 400, height, center)
    add_area("Rim", (center.x, center.y + height, center.z + height * 0.5), 550, height, center)
    output_dir.mkdir(parents=True, exist_ok=True)
    return scene, camera, center, height, max(width, dimensions.y)


def render(scene: bpy.types.Scene, camera: bpy.types.Object, target: Vector, direction: Vector, distance: float, scale: float, destination: Path) -> None:
    camera.location = target + direction.normalized() * distance
    camera.data.ortho_scale = scale
    look_at(camera, target)
    scene.render.filepath = str(destination)
    bpy.ops.render.render(write_still=True)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError(f"Preview fehlt oder ist leer: {destination}")


def image_color_stats(images: list[bpy.types.Image]) -> tuple[bool, int]:
    colors: set[tuple[int, int, int]] = set()
    for image in images:
        try:
            pixels = image.pixels
            total = len(pixels) // 4
            if total <= 0:
                continue
            step = max(1, total // 4096)
            for index in range(0, total, step):
                offset = index * 4
                colors.add(tuple(int(max(0.0, min(1.0, pixels[offset + channel])) * 31) for channel in range(3)))
                if len(colors) > 64:
                    return True, len(colors)
        except Exception:
            continue
    non_empty = len(colors) >= 4 and not all(color == (31, 31, 31) for color in colors) and not all(color == (0, 0, 0) for color in colors)
    return non_empty, len(colors)


def main() -> None:
    args = parse_args()
    source = Path(args.source)
    texture_dir = Path(args.textures)
    previews = Path(args.previews)
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError(f"Finales GLB fehlt oder ist leer: {source}")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.gltf(filepath=str(source))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError("GLB enthält kein Mesh-Objekt.")
    materials = {material for obj in meshes for material in obj.data.materials if material is not None}
    uv_maps = sum(len(obj.data.uv_layers) for obj in meshes)
    images: set[bpy.types.Image] = set()
    node_names: list[str] = []
    for material in materials:
        if not material.use_nodes or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            node_names.append(f"{node.name} {node.label}".casefold())
            if node.type == "TEX_IMAGE" and node.image is not None:
                images.add(node.image)
    base_color_present = bool(images) and any(
        node.type == "BSDF_PRINCIPLED" and node.inputs.get("Base Color") and node.inputs["Base Color"].is_linked
        for material in materials if material.use_nodes and material.node_tree
        for node in material.node_tree.nodes
    )
    joined_names = " ".join(node_names + [image.name.casefold() for image in images])
    principled_nodes = [
        node for material in materials if material.use_nodes and material.node_tree
        for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
    ]
    pbr = {
        "normal": any(node.inputs.get("Normal") and node.inputs["Normal"].is_linked for node in principled_nodes),
        "roughness": any(node.inputs.get("Roughness") and node.inputs["Roughness"].is_linked for node in principled_nodes),
        "metallic": any(node.inputs.get("Metallic IOR Level") and node.inputs["Metallic IOR Level"].is_linked for node in principled_nodes) or any(node.inputs.get("Metallic") and node.inputs["Metallic"].is_linked for node in principled_nodes),
    }
    required_names = ("BaseColor.png", "Normal.png", "Roughness.png", "Metallic.png")
    missing_external = [name for name in required_names if not any(path.is_file() and path.stat().st_size > 0 for path in texture_dir.glob(f"*{name}"))]
    color_non_empty, unique_colors = image_color_stats(list(images))
    minimum, maximum = bounds(meshes)
    dimensions = maximum - minimum
    largest = max(meshes, key=lambda obj: len(obj.data.polygons))
    largest_min, largest_max = bounds([largest])
    platform_candidates = []
    for obj in meshes:
        if obj == largest:
            continue
        obj_min, obj_max = bounds([obj])
        obj_dim = obj_max - obj_min
        if obj_max.z <= largest_min.z + max(dimensions.z * 0.03, 0.01) and obj_dim.x > dimensions.x * 0.35 and obj_dim.y > dimensions.y * 0.35:
            platform_candidates.append(obj.name)
    visible_eye_objects = [obj.name for obj in meshes if any(token in obj.name.casefold() for token in ("eye", "iris", "pupil"))]
    complete_character = dimensions.z > 0.5 and dimensions.x > 0.2 and dimensions.z > dimensions.y

    scene, camera, center, height, width = setup_render(meshes, previews)
    scale = max(height * 1.12, width * 1.18)
    distance = max(height * 2.2, 3.0)
    views = {
        "front.png": Vector((0, -1, 0)),
        "left.png": Vector((-1, 0, 0)),
        "back.png": Vector((0, 1, 0)),
        "three_quarter.png": Vector((-1, -1, 0)),
        "hands_and_feet.png": Vector((0, -1, -0.03)),
    }
    for name, direction in views.items():
        render(scene, camera, center, direction, distance, scale, previews / name)
    problems: list[str] = []
    checks = {
        "glb_importable": True,
        "mesh_count": len(meshes),
        "material_present": bool(materials),
        "uv_map_present": uv_maps > 0,
        "base_color_present": base_color_present,
        "pbr_maps": pbr,
        "texture_not_empty_or_uniform_white_black": color_non_empty,
        "sampled_unique_colors": unique_colors,
        "external_required_textures_present": not missing_external,
        "manual_review_required": {
            "eyes_fully_covered": True,
            "no_platform": True,
            "hands_and_feet_usable": True,
            "body_complete": True,
            "clothing_correct": True,
            "proportions_matching": True
        },
        "visual_heuristics_only": {"platform_candidates": platform_candidates, "approximately_complete_bounds": complete_character, "separate_eye_named_objects": visible_eye_objects},
        "bounds_xyz": [round(dimensions.x, 6), round(dimensions.y, 6), round(dimensions.z, 6)],
        "previews_created": all((previews / name).is_file() and (previews / name).stat().st_size > 0 for name in views),
    }
    if not materials: problems.append("Kein Material vorhanden.")
    if uv_maps <= 0: problems.append("Keine UV-Map vorhanden.")
    if not base_color_present: problems.append("Keine Base-Color-Bildtextur am Principled BSDF erkannt.")
    if not all(pbr.values()): problems.append("Nicht alle PBR-Verknüpfungen (Normal/Roughness/Metallic) wurden erkannt.")
    if missing_external: problems.append("Fehlende externe Pflichttexturen: " + ", ".join(missing_external))
    if not color_non_empty: problems.append("Textur ist leer, einfarbig weiß/schwarz oder besitzt zu wenig Farbvariation.")
    if not checks["previews_created"]: problems.append("Nicht alle fünf Vorschauen wurden erzeugt.")
    checks["problems"] = problems
    checks["valid"] = not problems
    print("FINAL_MESH_VALIDATION=" + json.dumps(checks, ensure_ascii=False))


if __name__ == "__main__":
    main()
