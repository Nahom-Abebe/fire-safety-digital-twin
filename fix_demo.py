# fix_demo.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

result = send_to_blender("""
import bpy

# ── Move board to the LEFT of the building ──────────────────
board = bpy.data.objects.get('FireSafetyBoard')
if board:
    board.location       = (-25.0, 0.0, 12.0)
    board.rotation_euler = (1.5708, 0, 0)
    board.data.size      = 0.9

    mat = bpy.data.materials.get('BoardText')
    if not mat:
        mat = bpy.data.materials.new('BoardText')
        mat.use_nodes = True
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value    = (1.0, 0.85, 0.0, 1.0)
        node.inputs['Emission Color'].default_value = (1.0, 0.85, 0.0, 1.0)
        node.inputs['Emission Strength'].default_value = 3.0
    board.data.materials.clear()
    board.data.materials.append(mat)
    print('Board repositioned left of building')

# ── Set viewport to top-down isometric (bird's eye) ─────────
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                r3d = space.region_3d

                # Bird's eye — looking straight down
                import mathutils
                r3d.view_perspective = 'ORTHO'
                r3d.view_distance    = 80.0

                # Rotate to look straight down at the building
                r3d.view_rotation = mathutils.Quaternion((1,0,0,0))
                r3d.view_location = mathutils.Vector((8.0, -5.0, 0.0))

                # Clean solid shading
                space.shading.type       = 'SOLID'
                space.shading.color_type = 'OBJECT'
                space.shading.show_shadows = False
                space.shading.use_scene_lights = False

                # Hide all overlays that clutter the view
                space.overlay.show_floor            = False
                space.overlay.show_axis_x           = False
                space.overlay.show_axis_y           = False
                space.overlay.show_cursor           = False
                space.overlay.show_object_origins   = False
                space.overlay.show_extras           = False
                space.overlay.show_relationship_lines = False

        area.tag_redraw()

print('Camera set to bird eye view')
""")
print(result)