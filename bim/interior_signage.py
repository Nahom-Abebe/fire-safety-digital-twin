# bim/interior_signage.py
# Adds room labels at doors and digital signage panels in corridors.
# Labels are static — placed once at startup.
# Signage panels update live when act_update_sign fires.

import json, os
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE
from bim.room_geometry import load_room_centroids


def create_room_labels() -> dict:
    """
    Places a small text label near each room centroid showing
    the room number and type (e.g. '0-4 Bedroom').
    Labels face the corridor (Y axis) for readability.
    """
    centroids = load_room_centroids()
    labels    = []

    for label, c in centroids.items():
        if c.get("node_type") not in ("room",):
            continue
        ifc_name = c.get("ifc_long_name", "")
        if not ifc_name:
            continue
        labels.append({
            "text": f"{label}\n{ifc_name}",
            "x"   : c["x"],
            "y"   : c["y"],
            "z"   : c["z"] + 1.5,   # above floor at door height
        })

    lj = json.dumps(labels)

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, json, traceback
try:
    labels = json.loads('{lj}')

    # Remove old labels
    for obj in list(bpy.data.objects):
        if obj.name.startswith('RoomLabel_'):
            bpy.data.objects.remove(obj, do_unlink=True)

    created = 0
    for i, item in enumerate(labels):
        bpy.ops.object.text_add(
            location=(item['x'], item['y'], item['z']))
        obj = bpy.context.object
        obj.name = f'RoomLabel_{{i:03d}}'
        obj.data.body        = item['text']
        obj.data.size        = 0.15
        obj.data.align_x     = 'CENTER'
        obj.rotation_euler   = (1.5708, 0, 0)

        # Yellow label material
        mat = bpy.data.materials.new(f'LabelMat_{{i:03d}}')
        mat.use_nodes = True
        n = mat.node_tree.nodes.get('Principled BSDF')
        if n:
            n.inputs['Base Color'].default_value     = (1.0, 0.9, 0.0, 1.0)
            n.inputs['Emission Color'].default_value  = (1.0, 0.9, 0.0, 1.0)
            n.inputs['Emission Strength'].default_value = 1.0
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        created += 1

    report = {{'status': 'ok', 'created': created}}
except Exception as e:
    report = {{'status': 'error', 'message': str(e)}}

with open(r"{RESULT_FILE}", "w") as f:
    json.dump(report, f)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    send_to_blender(code)
    return _read_result(timeout=30.0)


def create_corridor_signs() -> dict:
    """
    Places digital signage panels in each floor's corridor.
    Each panel has:
      - A rectangular screen (plane object, coloured by status)
      - Text overlay showing the current sign message
    Panels update when update_corridor_sign() is called.
    """
    # Corridor sign positions — one per floor, mounted on wall
    SIGN_POSITIONS = {
        "SIGN_F0_CORRIDOR_N": {"x": 0,  "y": -5,  "z": 2.2, "floor": "F0"},
        "SIGN_F1_CORRIDOR"  : {"x": 0,  "y": -5,  "z": 5.2, "floor": "F1"},
        "SIGN_F2_CORRIDOR"  : {"x": 0,  "y": -5,  "z": 8.2, "floor": "F2"},
        "SIGN_F3_CORRIDOR"  : {"x": 0,  "y": -5,  "z": 11.2,"floor": "F3"},
    }

    sj = json.dumps(SIGN_POSITIONS)

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, json, traceback
try:
    signs = json.loads('{sj}')

    # Remove old sign panels
    for obj in list(bpy.data.objects):
        if obj.name.startswith('SignPanel_') or obj.name.startswith('SignText_'):
            bpy.data.objects.remove(obj, do_unlink=True)

    created = 0
    for sign_id, pos in signs.items():
        x, y, z = pos['x'], pos['y'], pos['z']

        # Panel (plane)
        bpy.ops.mesh.primitive_plane_add(
            size=1, location=(x, y - 0.05, z))
        panel = bpy.context.object
        panel.name = f'SignPanel_{{sign_id}}'
        panel.scale = (1.2, 0.5, 0.4)
        panel.rotation_euler = (1.5708, 0, 0)

        mat = bpy.data.materials.new(f'SignMat_{{sign_id}}')
        mat.use_nodes = True
        n = mat.node_tree.nodes.get('Principled BSDF')
        if n:
            # Default green — all clear
            n.inputs['Base Color'].default_value     = (0.0, 0.7, 0.2, 1.0)
            n.inputs['Emission Color'].default_value  = (0.0, 0.7, 0.2, 1.0)
            n.inputs['Emission Strength'].default_value = 1.5
        panel.data.materials.clear()
        panel.data.materials.append(mat)

        # Sign text
        bpy.ops.object.text_add(location=(x, y - 0.1, z))
        txt = bpy.context.object
        txt.name = f'SignText_{{sign_id}}'
        txt.data.body        = f"{{pos['floor']}} CORRIDOR\\nStatus: CLEAR\\nAll routes open"
        txt.data.size        = 0.10
        txt.data.align_x     = 'CENTER'
        txt.rotation_euler   = (1.5708, 0, 0)

        tmat = bpy.data.materials.new(f'SignTextMat_{{sign_id}}')
        tmat.use_nodes = True
        tn = tmat.node_tree.nodes.get('Principled BSDF')
        if tn:
            tn.inputs['Base Color'].default_value     = (1.0, 1.0, 1.0, 1.0)
            tn.inputs['Emission Color'].default_value  = (1.0, 1.0, 1.0, 1.0)
            tn.inputs['Emission Strength'].default_value = 2.0
        txt.data.materials.clear()
        txt.data.materials.append(tmat)

        created += 1

    report = {{'status': 'ok', 'created': created}}
except Exception as e:
    report = {{'status': 'error', 'message': str(e)}}

with open(r"{RESULT_FILE}", "w") as f:
    json.dump(report, f)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    send_to_blender(code)
    return _read_result(timeout=30.0)


def update_corridor_sign(sign_id: str,
                          message: str,
                          status: str) -> dict:
    """
    Updates a digital signage panel in the corridor.
    status: 'ACTIVE'   → green panel
            'BLOCKED'  → red panel
            'ALTERNATE'→ amber panel
    Called automatically from bim/signage.py when a sign updates.
    """
    colours = {
        "ACTIVE"   : (0.05, 0.70, 0.15, 1.0),   # green
        "BLOCKED"  : (0.90, 0.05, 0.05, 1.0),   # red
        "ALTERNATE": (0.90, 0.45, 0.00, 1.0),   # amber
    }
    colour = colours.get(status, colours["ACTIVE"])

    # Truncate message for sign display
    lines = message[:100]

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, json
try:
    panel = bpy.data.objects.get('SignPanel_{sign_id}')
    txt   = bpy.data.objects.get('SignText_{sign_id}')

    if panel and panel.data.materials:
        mat  = panel.data.materials[0]
        node = mat.node_tree.nodes.get('Principled BSDF')
        if node:
            node.inputs['Base Color'].default_value     = {list(colour)}
            node.inputs['Emission Color'].default_value  = {list(colour)}
            node.inputs['Emission Strength'].default_value = 2.0

    if txt:
        txt.data.body = '''{lines}'''

    report = {{'status': 'ok', 'sign_id': '{sign_id}', 'status': '{status}'}}
except Exception as e:
    report = {{'status': 'error', 'message': str(e)}}

with open(r"{RESULT_FILE}", "w") as f:
    json.dump(report, f)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    send_to_blender(code)
    return _read_result(timeout=10.0)