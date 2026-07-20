# reset_building_opacity.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

result = send_to_blender("""
import bpy
mat = bpy.data.materials.get('SHELL_TRANSPARENT')
if mat:
    bpy.data.materials.remove(mat)
count = 0
for obj in bpy.data.objects:
    if obj.type == 'MESH' and obj.data:
        obj.data.materials.clear()
        count += 1
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()
print(f'Reset {count} objects')
""")
print(result)