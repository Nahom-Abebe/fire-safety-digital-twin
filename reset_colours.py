# reset_colours.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

result = send_to_blender("""
import bpy
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        obj.color = (1.0, 1.0, 1.0, 1.0)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()
print('Reset done')
""")
print(result)