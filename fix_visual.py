# fix_visual.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

result = send_to_blender("""
import bpy

# Reset ALL object colours to white/grey
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        obj.color = (0.8, 0.8, 0.8, 1.0)

# Remove FS_ materials
for mat in list(bpy.data.materials):
    if mat.name.startswith('FS_') or mat.name.startswith('TEST_'):
        bpy.data.materials.remove(mat)

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()

print('Visual reset done')
""")
print(result)