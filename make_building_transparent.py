# make_building_transparent.py
# Makes the building shell semi-transparent so occupant markers
# are visible inside rooms from any camera angle.


import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

ALPHA = 0.25  

result = send_to_blender(f"""
import bpy

# Objects to make transparent (exterior shell only)
TRANSPARENT_PREFIXES = (
    'IfcWall',
    'IfcSlab',
    'IfcRoof',
    'IfcColumn',
    'IfcCurtainWall',
)

# Objects to keep opaque (doors, windows stay visible)
KEEP_OPAQUE_PREFIXES = (
    'IfcDoor',
    'IfcWindow',
    'IfcStair',
    'IfcRailing',
    'IfcFurnishing',
)

count = 0
for obj in bpy.data.objects:
    if obj.type != 'MESH':
        continue
    if any(obj.name.startswith(p) for p in KEEP_OPAQUE_PREFIXES):
        continue
    if not any(obj.name.startswith(p) for p in TRANSPARENT_PREFIXES):
        continue

    mat_name = 'SHELL_TRANSPARENT'
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get('Principled BSDF')
        if bsdf:
            bsdf.inputs['Alpha'].default_value = {ALPHA}
            bsdf.inputs['Base Color'].default_value = (0.8, 0.8, 0.8, 1.0)
        mat.blend_method  = 'BLEND'
        mat.shadow_method = 'NONE'

    obj.data.materials.clear()
    obj.data.materials.append(mat)
    count += 1

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()

print(f'Made {{count}} objects transparent (alpha={ALPHA})')
""")
print(result)