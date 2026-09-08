from __future__ import annotations

import math
from pathlib import Path

import bpy
from mathutils import Vector


def _look_at(camera: bpy.types.Object, target: Vector) -> None:
    direction = target - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _bounds(meshes: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    minimum = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    maximum = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return minimum, maximum


def _available_render_engines(scene: bpy.types.Scene) -> set[str]:
    try:
        engine_property = scene.render.bl_rna.properties["engine"]
        return {item.identifier for item in engine_property.enum_items}
    except Exception as exc:
        print(f"Render-Engine-Liste konnte nicht gelesen werden: {exc}")
        return set()


def _select_render_engine(scene: bpy.types.Scene) -> str:
    available = _available_render_engines(scene)
    priorities = (
        "BLENDER_EEVEE",
        "BLENDER_EEVEE_NEXT",
        "BLENDER_WORKBENCH",
        "CYCLES",
    )
    for engine in priorities:
        if engine in available:
            scene.render.engine = engine
            print(f"Verwendete Render-Engine: {engine}")
            return engine

    # Manche Blender-Builds melden dynamische Engine-Enums erst beim Setzen.
    for engine in priorities:
        try:
            scene.render.engine = engine
            print(f"Verwendete Render-Engine: {engine} (Fallback-Test)")
            return engine
        except (TypeError, ValueError):
            continue
    raise RuntimeError(
        "Keine kompatible Render-Engine gefunden. "
        f"Gemeldete Engines: {sorted(available) or ['keine']}"
    )


def render_views(
    meshes: list[bpy.types.Object],
    destination: Path,
    filename_prefix: str = "",
) -> str:
    destination.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    render_engine = _select_render_engine(scene)
    scene.render.resolution_x = 768
    scene.render.resolution_y = 768
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    if scene.world is None:
        scene.world = bpy.data.worlds.new("TECH_PREVIEW_WORLD")
    scene.world.color = (0.055, 0.055, 0.055)

    minimum, maximum = _bounds(meshes)
    center = (minimum + maximum) / 2
    height = max(maximum.z - minimum.z, 0.1)

    camera_data = bpy.data.cameras.new("TECH_PREVIEW_CAMERA")
    camera = bpy.data.objects.new("TECH_PREVIEW_CAMERA", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera_data.lens = 65
    scene.camera = camera

    key_data = bpy.data.lights.new("TECH_PREVIEW_KEY", type="AREA")
    key_data.energy = 900
    key_data.shape = "DISK"
    key_data.size = height * 1.5
    key = bpy.data.objects.new("TECH_PREVIEW_KEY", key_data)
    bpy.context.scene.collection.objects.link(key)
    key.location = center + Vector((height * 1.5, -height * 1.5, height * 1.2))
    key.rotation_euler = ((center - key.location).to_track_quat("-Z", "Y").to_euler())

    fill_data = bpy.data.lights.new("TECH_PREVIEW_FILL", type="AREA")
    fill_data.energy = 450
    fill_data.size = height
    fill = bpy.data.objects.new("TECH_PREVIEW_FILL", fill_data)
    bpy.context.scene.collection.objects.link(fill)
    fill.location = center + Vector((-height, -height, height * 0.6))
    fill.rotation_euler = ((center - fill.location).to_track_quat("-Z", "Y").to_euler())

    distance = height * 2.3
    views = {
        "front": Vector((0, -distance, center.z)),
        "left": Vector((-distance, 0, center.z)),
        "back": Vector((0, distance, center.z)),
        "three_quarter": Vector((distance * math.sin(math.radians(35)), -distance, center.z)),
    }
    try:
        for name, location in views.items():
            camera.location = location
            _look_at(camera, center)
            scene.render.filepath = str(destination / f"{filename_prefix}{name}.png")
            bpy.ops.render.render(write_still=True)
    finally:
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.objects.remove(key, do_unlink=True)
        bpy.data.objects.remove(fill, do_unlink=True)
    return render_engine
