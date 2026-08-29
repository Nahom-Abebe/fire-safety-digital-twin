# bim/interior_signage.py
# Adds room labels at doors and digital signage panels in corridors.
# Labels are static — placed once at startup.
# Signage panels update live when act_update_sign fires.

import json, os
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE
from bim.room_geometry import load_room_centroids
from sensors.building_graph import BUILDING_GRAPH

# The complete, authoritative list of sign IDs that actually have a
# real Blender panel (SignPanel_/SignText_ objects) — must match
# SIGN_POSITIONS' keys inside create_corridor_signs() exactly.
#
# Fix applied: bim_query.py's SIGNS/get_all_signs() is loaded straight
# from global_ids_v2.json — the full IFC-derived sign registry, a
# completely separate set from the 5 signs this file actually renders
# a visual panel for. mcp_server/server.py's sense_building_state()
# was exposing get_all_signs()'s FULL list to the agent as
# available_sign_ids, meaning a sign that exists in the IFC data but
# was never given a Blender panel here was presented as equally valid
# to choose. Confirmed directly: the agent picked "SIGN_F0_EXIT_N" and
# "SIGN_F0_EXIT_S" — both wrote successfully to their IFC Pset (no
# error, correctly counted as genuine successes) but had no matching
# panel object for _update_blender_sign_panel() to find, so nothing
# changed on screen — a real write with zero visible effect. This
# constant is now imported by mcp_server/server.py to scope
# available_sign_ids (and list_signs()'s output) down to only signs
# that can actually be seen to change, so the agent can never again
# choose one that succeeds "for real" but shows nothing.
VISUAL_SIGN_IDS = [
    "SIGN_F0_CORRIDOR_N",
    "SIGN_F1_CORRIDOR",
    "SIGN_F2_CORRIDOR",
    "SIGN_F3_CORRIDOR",
    "SIGN_F3_STAIR",
]


def create_room_labels() -> dict:
    """
    Places a small text label near each room centroid showing the
    room number (and a richer name when available).

    Room membership is determined from BUILDING_GRAPH (node_type ==
    "room"), not from fields on the centroid dict — load_room_centroids()
    does not reliably carry node_type or ifc_long_name, and filtering
    on them silently produced zero labels every run.
    """
    centroids = load_room_centroids()

    room_labels = {d["label"] for _, d in BUILDING_GRAPH.nodes(data=True)
                   if d.get("node_type") == "room"}

    labels        = []
    missing_centroid = []

    for label in room_labels:
        c = centroids.get(label)
        if c is None:
            missing_centroid.append(label)
            continue
        # Use a richer name only if the centroid actually carries one —
        # never required, so one missing field can't zero out the batch.
        extra = c.get("ifc_long_name") or c.get("room_type")
        text  = f"{label}\n{extra}" if extra else label
        labels.append({
            "text": text,
            "x"   : c["x"],
            "y"   : c["y"],
            "z"   : c.get("z", 0) + 1.5,   # above floor at door height
        })

    if not labels:
        return {
            "status" : "error",
            "created": 0,
            "message": (
                f"No labels to create — {len(room_labels)} graph rooms, "
                f"0 had a matching centroid entry"
            ),
        }

    lj = json.dumps(labels)

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, json, traceback
try:
    labels = json.loads('{lj}')

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

    # Honest status — a run that intended to create labels but produced
    # none on the Blender side is an error, not "ok".
    report = ({{'status': 'ok', 'created': created}} if created > 0
              else {{'status': 'error', 'created': 0,
                     'message': 'Blender-side creation loop produced 0 objects'}})
except Exception as e:
    report = {{'status': 'error', 'message': str(e)}}

with open(r"{RESULT_FILE}", "w") as f:
    json.dump(report, f)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    send_to_blender(code)
    # Fix applied: this creates 80 separate text objects, each with
    # its own new material — bpy.ops.object.text_add() called 80
    # times is genuinely expensive, and 30s may not be enough for
    # Blender to finish before this gives up waiting. Confirmed
    # directly: a real phase1_setup.py run showed BOTH "created" and
    # "status" falling back to "?" (their .get() defaults), which
    # only happens if the returned dict has neither key — consistent
    # with a timeout fallback shape, not this function's own
    # success/error dict (which always has both). bake_animation()
    # elsewhere in this project does far more work per call and uses
    # timeout=300.0 for exactly this reason.
    result = _read_result(timeout=90.0)

    if missing_centroid:
        result["warning"] = (
            f"{len(missing_centroid)} graph rooms had no centroid: "
            f"{missing_centroid[:5]}{'...' if len(missing_centroid) > 5 else ''}"
        )
    return result


def create_corridor_signs() -> dict:
    """
    Places digital signage panels in each floor's corridor, plus the
    one remaining scenario-specific extra panel TS-04 depends on:
      - SIGN_F3_STAIR : stairwell accessibility sign (TS-04)

    SIGN_F0_CORRIDOR_S was removed (was previously placed at the
    centroid of graph corridor "0-B", but GRAPH_TO_IFC maps "0-B" to
    IFC space "0023" which is actually a small "Side lobby", not a
    real second corridor — that's why it rendered close to and
    inside the building near the north sign, not as a genuinely
    separate south corridor location. Ground floor now has a single
    corridor sign, consistent with every other floor. fix_and_bake.py's
    TS-02 no longer tries to update a south sign — the primary north
    sign's own text ("North Corridor BLOCKED / Use South Exit")
    already conveys the redirect without needing a second sign object.

    SIGN_F3_STAIR is anchored as an offset from the REAL "3-A"
    corridor centroid rather than a fully arbitrary guess, but
    remains approximate: the F3 stairwell node (B-L3) is not in
    GRAPH_TO_IFC at all, so no true centroid exists for it. A precise
    position needs room_geometry.py extended to map stair nodes to
    IFC objects too — out of scope here. Falls back to a fixed offset
    only if the expected centroid is missing from room_centroids.json.

    These five signs are the complete, authoritative visual set — see
    VISUAL_SIGN_IDS at module level, which MUST be kept in sync with
    this dict's keys.
    """
    centroids = load_room_centroids()

    def _offset(base_label, dx, dy, dz, fallback):
        c = centroids.get(base_label)
        if c is None:
            return fallback
        return {"x": c["x"] + dx, "y": c["y"] + dy, "z": c["z"] + dz}

    stair_pos = _offset("3-A", 4, 0, 0, {"x": 4, "y": -5, "z": 11.2})

    SIGN_POSITIONS = {
        "SIGN_F0_CORRIDOR_N": {"x": 0,  "y": -5,  "z": 2.2,  "floor": "F0",
                               "label": "F0 CORRIDOR"},
        "SIGN_F1_CORRIDOR"  : {"x": 0,  "y": -5,  "z": 5.2,  "floor": "F1",
                               "label": "F1 CORRIDOR"},
        "SIGN_F2_CORRIDOR"  : {"x": 0,  "y": -5,  "z": 8.2,  "floor": "F2",
                               "label": "F2 CORRIDOR"},
        "SIGN_F3_CORRIDOR"  : {"x": 0,  "y": -5,  "z": 11.2, "floor": "F3",
                               "label": "F3 CORRIDOR"},

        # Offset from the real "3-A" corridor centroid. Not a true
        # stairwell position — see docstring above.
        "SIGN_F3_STAIR"     : {**stair_pos, "floor": "F3",
                               "label": "F3 STAIRWELL"},
    }

    sj = json.dumps(SIGN_POSITIONS)

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, json, traceback
try:
    signs = json.loads('{sj}')

    for obj in list(bpy.data.objects):
        if obj.name.startswith('SignPanel_') or obj.name.startswith('SignText_'):
            bpy.data.objects.remove(obj, do_unlink=True)

    created = 0
    for sign_id, pos in signs.items():
        x, y, z = pos['x'], pos['y'], pos['z']

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
            n.inputs['Base Color'].default_value     = (0.0, 0.7, 0.2, 1.0)
            n.inputs['Emission Color'].default_value  = (0.0, 0.7, 0.2, 1.0)
            n.inputs['Emission Strength'].default_value = 1.5
        panel.data.materials.clear()
        panel.data.materials.append(mat)

        bpy.ops.object.text_add(location=(x, y - 0.1, z))
        txt = bpy.context.object
        txt.name = f'SignText_{{sign_id}}'
        txt.data.body        = f"{{pos['label']}}\\nStatus: CLEAR\\nAll routes open"
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

    report = ({{'status': 'ok', 'created': created,
               'expected': len(signs)}} if created == len(signs)
              else {{'status': 'error', 'created': created,
                     'expected': len(signs),
                     'message': 'Not all sign panels were created'}})
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

    Fixed: the returned dict previously had two 'status' keys —
    {'status': 'ok', ..., 'status': status} — so Python silently kept
    only the second one, meaning callers checking result['status'] for
    operation success were actually reading the sign's colour state.
    The operation outcome is now 'result'; the sign's own state stays
    under 'sign_status'.
    """
    colours = {
        "ACTIVE"   : (0.05, 0.70, 0.15, 1.0),   # green
        "BLOCKED"  : (0.90, 0.05, 0.05, 1.0),   # red
        "ALTERNATE": (0.90, 0.45, 0.00, 1.0),   # amber
    }
    colour = colours.get(status, colours["ACTIVE"])
    lines  = message[:100]

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, json
try:
    panel = bpy.data.objects.get('SignPanel_{sign_id}')
    txt   = bpy.data.objects.get('SignText_{sign_id}')

    found_panel = panel is not None
    found_text  = txt is not None

    if panel and panel.data.materials:
        mat  = panel.data.materials[0]
        node = mat.node_tree.nodes.get('Principled BSDF')
        if node:
            node.inputs['Base Color'].default_value     = {list(colour)}
            node.inputs['Emission Color'].default_value  = {list(colour)}
            node.inputs['Emission Strength'].default_value = 2.0

    if txt:
        txt.data.body = '''{lines}'''

    if found_panel and found_text:
        report = {{'result': 'ok', 'sign_id': '{sign_id}', 'sign_status': '{status}'}}
    else:
        report = {{'result': 'error', 'sign_id': '{sign_id}',
                   'message': f'panel_found={{found_panel}} text_found={{found_text}}'}}
except Exception as e:
    report = {{'result': 'error', 'message': str(e)}}

with open(r"{RESULT_FILE}", "w") as f:
    json.dump(report, f)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    send_to_blender(code)
    return _read_result(timeout=10.0)