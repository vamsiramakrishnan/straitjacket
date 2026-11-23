"""bake_contact.py: bake a soft contact shadow for each prop of the film, in Blender (Cycles ambient occlusion).

    blender -b --factory-startup -P tools/bake_contact.py -- SITE_ASSETS_DIR [--samples 256] [--size 512]

For every `<name>.glb` in SITE_ASSETS_DIR that the film stands on the floor, this imports the file, lays a ground
plane under it that reaches MARGIN metres beyond its footprint, bakes how much of the sky each point of that plane
can still see, and writes `contact_<name>.png` (8-bit grey; white = open floor, darker = closer under the object) and
`contact.json` (the plane's centre and size in the film's coordinates, so the page places each picture exactly).
The page multiplies the floor by these pictures; that is the soft dark seat under each object that a real-time light
cannot give. A realtime shadow from the key light is drawn on top by three.js.

Coordinates: glTF and three.js are Y-up; Blender is Z-up. Blender's importer turns glTF (x, y, z) into (x, -z, y),
so a plane centred at Blender (cx, cy) with size (sx, sy) is at three.js x = cx, z = -cy.
"""
import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
assets = Path(argv[0]).resolve()
samples = int(argv[argv.index("--samples") + 1]) if "--samples" in argv else 256
size = int(argv[argv.index("--size") + 1]) if "--size" in argv else 512
PROPS = ("console", "capture_gate", "context_frame", "evidence_store", "ledger_stand")
MARGIN, REACH = 0.75, 1.1          # metres of floor around the footprint; how far occlusion is looked for

scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = samples
scene.cycles.use_denoising = False
scene.render.bake.margin = 0
world = scene.world or bpy.data.worlds.new("world")
scene.world = world
world.light_settings.distance = REACH

record = {}
for name in PROPS:
    glb = assets / f"{name}.glb"
    if not glb.is_file():
        print("skip (no file):", glb)
        continue
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.import_scene.gltf(filepath=str(glb))
    meshes = [o for o in scene.objects if o.type == "MESH"]
    corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners)))
    centre, span = (lo + hi) / 2, (hi - lo) + Vector((2 * MARGIN, 2 * MARGIN))
    bpy.ops.mesh.primitive_plane_add(size=1, location=(centre.x, centre.y, 0.0))
    plane = bpy.context.active_object
    plane.scale = (span.x, span.y, 1)
    image = bpy.data.images.new(f"contact_{name}", width=size, height=max(8, round(size * span.y / span.x)), alpha=False)
    image.colorspace_settings.name = "Non-Color"
    material = bpy.data.materials.new("bake")
    material.use_nodes = True
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    material.node_tree.nodes.active = node
    plane.data.materials.append(material)
    bpy.ops.object.select_all(action="DESELECT")
    plane.select_set(True)
    bpy.context.view_layer.objects.active = plane
    bpy.ops.object.bake(type="AO")
    out = assets / f"contact_{name}.png"
    image.filepath_raw = str(out)
    image.file_format = "PNG"
    scene.render.image_settings.color_mode = "BW"
    image.save_render(str(out), scene=scene)
    record[name] = {"file": out.name, "centre_x": round(centre.x, 4), "centre_z": round(-centre.y, 4),
                    "size_x": round(span.x, 4), "size_z": round(span.y, 4), "pixels": [image.size[0], image.size[1]]}
    print("baked", out.name, record[name])

(assets / "contact.json").write_text(json.dumps({"margin_m": MARGIN, "reach_m": REACH, "samples": samples, "props": record}, indent=1) + "\n")
print("wrote", assets / "contact.json")
