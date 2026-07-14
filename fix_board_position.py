# fix_board_position.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

result = send_to_blender("""
import bpy

board = bpy.data.objects.get('FireSafetyBoard')
if board:
    # Move board to the left of the building, facing camera
    board.location       = (-18.0, -5.0, 18.0)
    board.rotation_euler = (1.5708, 0, 0)
    board.data.size      = 1.2

    # Make text white so it reads clearly
    mat = bpy.data.materials.get('BoardMat')
    if not mat:
        mat = bpy.data.materials.new('BoardMat')
        mat.use_nodes = True
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        node.inputs['Emission Color'].default_value = (1.0, 0.9, 0.3, 1.0)
        node.inputs['Emission Strength'].default_value = 2.0
    board.data.materials.clear()
    board.data.materials.append(mat)
    print('Board repositioned')
else:
    print('Board not found')
""")
print(result)