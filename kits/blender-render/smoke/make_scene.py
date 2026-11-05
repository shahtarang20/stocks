"""Create the smoke-test scene. Run with:  blender -b --python make_scene.py -- /path/to/scene.blend
Deterministic, tiny (a cube, a plane, one light, 3 frames of rotation) so the smoke test is quick on any GPU."""

import sys

import bpy

out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add(size=10, location=(0, 0, 0))
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
cube = bpy.context.active_object
bpy.ops.object.light_add(type="AREA", location=(3, -3, 6))
bpy.context.active_object.data.energy = 800
bpy.ops.object.camera_add(location=(6, -6, 4), rotation=(1.1, 0, 0.8))
bpy.context.scene.camera = bpy.context.active_object
for f, rot in ((1, 0.0), (2, 0.4), (3, 0.8)):
    cube.rotation_euler[2] = rot
    cube.keyframe_insert("rotation_euler", frame=f)
sc = bpy.context.scene
sc.frame_start, sc.frame_end = 1, 3
sc.render.resolution_x, sc.render.resolution_y = 320, 240
bpy.ops.wm.save_as_mainfile(filepath=out)
