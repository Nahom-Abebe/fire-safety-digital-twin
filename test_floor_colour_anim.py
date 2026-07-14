# test_floor_colour_anim.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

result = send_to_blender("""
import bpy

SKIP = ('IfcFurnishing','IfcBuildingElementProxy','IfcFlowTerminal',
        'IfcSanitaryTerminal','IfcLightFixture','IfcAlarm','IfcSign',
        'IfcDoor','IfcWindow')

FLOORS = {
    'IfcBuildingStorey/F0 Ground Floor': ('FS_F0', 0.05, 0.7, 0.15),
    'IfcBuildingStorey/F1 First Floor' : ('FS_F1', 0.05, 0.7, 0.15),
    'IfcBuildingStorey/F2 Second Floor': ('FS_F2', 0.05, 0.7, 0.15),
    'IfcBuildingStorey/F3 Third Floor' : ('FS_F3', 0.05, 0.7, 0.15),
}

for col_name, (mat_name, r, g, b) in FLOORS.items():
    col = bpy.data.collections.get(col_name)
    if not col:
        print(f'NOT FOUND: {col_name}')
        continue
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value = (r, g, b, 1.0)
    count = 0
    for obj in col.objects:
        if obj.type == 'MESH' and not any(obj.name.startswith(p) for p in SKIP):
            if obj.data:
                obj.data.materials.clear()
                obj.data.materials.append(mat)
                count += 1
    print(f'{col_name.split("/")[-1]}: {count} objects coloured GREEN')

# Now set F0 to RED (fire zone simulation)
col = bpy.data.collections.get('IfcBuildingStorey/F0 Ground Floor')
mat = bpy.data.materials.get('FS_F0')
if col and mat:
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value = (0.9, 0.05, 0.05, 1.0)
    print('F0 set to RED (fire zone)')

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print('Done')
""")
print(result)