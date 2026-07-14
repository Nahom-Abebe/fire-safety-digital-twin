# set_camera.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

result = send_to_blender("""
import bpy
from mathutils import Vector

# Set viewport to isometric bird's-eye angle
# showing all 4 floors from the front-right
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                r3d = space.region_3d

                # Position camera above and to the right
                r3d.view_location   = Vector((16.0, -2.0, 8.0))
                r3d.view_distance   = 55.0
                r3d.view_rotation   = (0.7, 0.4, 0.15, 0.56)

                # Solid shading + object colour
                space.shading.type       = 'SOLID'
                space.shading.color_type = 'OBJECT'
                space.shading.background_type = 'THEME'

                # Hide overlay clutter
                space.overlay.show_floor        = False
                space.overlay.show_axis_x       = False
                space.overlay.show_axis_y       = False
                space.overlay.show_cursor       = False
                space.overlay.show_object_origins = False

        area.tag_redraw()

print('Camera set')
""")
print(result)