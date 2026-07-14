# bim/animation_baker.py
# Bakes per-agent occupancy management timeline into Blender keyframes.
#
# This is the OCCUPANCY MANAGEMENT version — no fire alarm, no evacuation.
# Peter Lawrence's model:
#   - Occupants move naturally between rooms (probabilistic random walk)
#   - When a room exceeds ADB capacity, its attractiveness drops
#   - Affected room and floor turn RED (overcrowded)
#   - All other rooms stay GREEN (compliant)
#   - Board shows "REDIRECTED" not "ALARM"
#   - Signs updated with specific ADB clause citations
#   - FireAlarmStatus is NOT written — ComplianceStatus FAIL is written instead

import json, os, random
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE
from bim.room_geometry import load_room_centroids
from bim.signage import update_sign
from sensors.agent_walk import simulate_agent_timeline
from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy

# ── Marker colours ────────────────────────────────────────────────────────────
COLOUR_NORMAL    = (0.15, 0.45, 0.90, 1.0)   # blue   — compliant room
COLOUR_REDIRECTED = (0.95, 0.55, 0.10, 1.0)  # orange — adjacent to violation
COLOUR_OVER      = (0.90, 0.10, 0.10, 1.0)   # red    — in overcrowded room

# ── Floor collections ─────────────────────────────────────────────────────────
FLOOR_COLLECTIONS = {
    "F0 Ground Floor" : "IfcBuildingStorey/F0 Ground Floor",
    "F1 First Floor"  : "IfcBuildingStorey/F1 First Floor",
    "F2 Second Floor" : "IfcBuildingStorey/F2 Second Floor",
    "F3 Third Floor"  : "IfcBuildingStorey/F3 Third Floor",
}

FLOOR_ORDER = [
    "F0 Ground Floor",
    "F1 First Floor",
    "F2 Second Floor",
    "F3 Third Floor",
]

SKIP_PREFIXES = (
    'IfcFurnishing', 'IfcBuildingElementProxy', 'IfcFlowTerminal',
    'IfcSanitaryTerminal', 'IfcLightFixture', 'IfcAlarm', 'IfcSign',
    'IfcDoor', 'IfcWindow',
)

BIM_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(BIM_DIR, "_bake_input.json").replace("\\", "/")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _jitter(centroid, marker_id):
    rng = random.Random(marker_id)
    jx  = (rng.random() - 0.5) * centroid.get("dx", 1.0) * 0.28
    jy  = (rng.random() - 0.5) * centroid.get("dy", 1.0) * 0.28
    return centroid["x"] + jx, centroid["y"] + jy, centroid["z"]


def _make_resolver(centroids):
    """Resolves any graph node to a centroid — BFS fallback for unmapped nodes."""
    G = BUILDING_GRAPH
    node_to_label = {n: d["label"] for n, d in G.nodes(data=True)}
    cache = {}

    def resolve(node_id):
        if node_id in cache:
            return cache[node_id]
        label = node_to_label[node_id]
        if label in centroids:
            cache[node_id] = centroids[label]
            return cache[node_id]
        visited, queue = {node_id}, [node_id]
        while queue:
            cur = queue.pop(0)
            for nb in G.neighbors(cur):
                if nb in visited:
                    continue
                visited.add(nb)
                nb_label = node_to_label[nb]
                if nb_label in centroids:
                    cache[node_id] = centroids[nb_label]
                    return cache[node_id]
                queue.append(nb)
        cache[node_id] = {"x": 0.0, "y": 0.0, "z": 0.0, "dx": 1.0, "dy": 1.0}
        return cache[node_id]

    return resolve


def _build_floor_colour_timeline(timeline: list,
                                  frames_per_tick: int,
                                  violation_room: str) -> dict:
    """
    Builds floor colour keyframes — pure capacity-based logic:
      RED   = this floor has at least one room over its ADB max occupancy
      GREEN = all rooms on this floor are within safe capacity
    No fire alarm, no amber, no whole-building state changes.
    """
    G = BUILDING_GRAPH
    label_to_floor = {d["label"]: d["floor"] for _, d in G.nodes(data=True)}
    floor_keys     = {fl: [] for fl in FLOOR_COLLECTIONS}

    for record in timeline:
        tick    = record["tick"]
        frame   = tick * frames_per_tick
        alerts  = record["snapshot"].get("alerts", [])

        # Which floors have at least one OVER-capacity room right now?
        over_floors = set()
        for a in alerts:
            fl = label_to_floor.get(a.get("label", ""))
            if fl:
                over_floors.add(fl)

        for floor_name in FLOOR_COLLECTIONS:
            if floor_name in over_floors:
                r, g, b = 0.90, 0.05, 0.05   # RED — overcrowded room on this floor
            else:
                r, g, b = 0.05, 0.70, 0.15   # GREEN — all rooms compliant

            floor_keys[floor_name].append((frame, r, g, b))

    return floor_keys


def _build_board_text(snap: dict, violation: dict,
                       violation_room: str) -> str:
    """
    Builds board text for one tick.
    Uses occupancy management language — no fire alarm, no evacuation.
    """
    lines = [
        "FIRE SAFETY DIGITAL TWIN",
        f"Tick: {snap['tick']}",
        "",
        f"Total Occupants: {snap['total_occ']}",
        "",
        "FLOOR OCCUPANCY:",
    ]

    for fl in FLOOR_ORDER:
        cnt = snap["by_floor"].get(fl, 0)
        if cnt > 0:
            lines.append(f"  {fl:20s}: {cnt}")

    lines.append("")

    if violation and snap.get("alerts"):
        # Show which rooms are actually over capacity right now
        over_rooms = [a for a in snap["alerts"] if a["severity"] == "OVER"]
        if over_rooms:
            lines.append(f"⚠ OCCUPANCY ALERT ({len(over_rooms)} room(s)):")
            for a in over_rooms[:4]:
                lines.append(
                    f"  {a['label']}: {a['current']}/{a['max']} "
                    f"— REDIRECTED (ADB Cl.2.43)"
                )
        else:
            lines.append(f"Monitoring: {violation_room} redirected")
            lines.append("Status: Returning to compliance")
    elif violation:
        lines.append(f"Monitoring: {violation['node_label']}")
        lines.append("Status: Occupancy within limits")
    else:
        lines.append("Status: NORMAL — all rooms compliant")

    return "\n".join(lines)


# ── IFC Pset write-back ───────────────────────────────────────────────────────

def _write_compliance_pset(violation_room: str):
    """
    Writes ComplianceStatus=FAIL to Pset_FireSafetyStatus on the
    violation room's IFC space. This is the correct write-back for
    occupancy management — NOT FireAlarmStatus (which implies fire).
    """
    try:
        from bim.room_geometry import GRAPH_TO_IFC
        from bim.bim_query import SPACES
        from bim.ifc_bridge import update_ifc_pset_properties

        ifc_name = GRAPH_TO_IFC.get(violation_room)
        if not ifc_name:
            return {"error": f"No IFC mapping for {violation_room}"}

        gid = next((gid for gid, s in SPACES.items()
                    if s.get("name") == ifc_name), None)
        if not gid:
            return {"error": f"No GlobalId for {ifc_name}"}

        return update_ifc_pset_properties(gid, "Pset_FireSafetyStatus", {
            "ComplianceStatus": "FAIL",
            "LastUpdatedBy"   : "OccupancyManagementAgent",
        })
    except Exception as e:
        return {"error": str(e)}


# ── Sign updates ──────────────────────────────────────────────────────────────

# Floor → corridor sign mapping
FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}


def _update_signs_for_violation(violation_room: str, violation_floor: str):
    """
    Updates the corridor sign for the affected floor with an ADB-cited
    occupancy management message — not an evacuation/fire message.
    """
    sign_id = FLOOR_SIGNS.get(violation_floor)
    if not sign_id:
        return

    update_sign(
        sign_id,
        f"Room {violation_room} at capacity — "
        f"please use alternative areas (ADB Cl.2.43)",
        "ALTERNATE",
        "ADB Vol2 Clause 2.43 — residential care home bedroom occupancy"
    )


# ── Main bake function ────────────────────────────────────────────────────────

def bake_animation(total_occupants: int = 80,
                   total_ticks: int = 25,
                   violation_tick: int = 5,
                   violation_room: str = "0-4",
                   frames_per_tick: int = 24,
                   seed: int = 42) -> dict:
    """
    Full occupancy management animation bake:
      1. Simulate per-agent timeline (attractiveness-based redirection)
      2. Build occupant marker keyframes (position + colour)
      3. Build floor colour keyframes (RED = over capacity, GREEN = compliant)
      4. Build board text list (occupancy management language)
      5. Write ComplianceStatus FAIL to IFC for violation room
      6. Update corridor sign with ADB Cl.2.43 citation
      7. Send everything to Blender in one round trip
    """
    centroids = load_room_centroids()
    resolve   = _make_resolver(centroids)

    print("  Simulating agent timeline (occupancy management)...")
    timeline = simulate_agent_timeline(
        total_occupants = total_occupants,
        total_ticks     = total_ticks,
        violation_tick  = violation_tick,
        violation_room  = violation_room,
        seed            = seed,
    )

    G             = BUILDING_GRAPH
    node_to_label = {n: d["label"] for n, d in G.nodes(data=True)}
    node_to_floor = {n: d["floor"]  for n, d in G.nodes(data=True)}

    # Find violation room node and floor for colour coding
    v_node  = next((n for n, d in G.nodes(data=True)
                    if d["label"] == violation_room), None)
    v_floor = G.nodes[v_node]["floor"] if v_node else None

    # ── Occupant marker keyframes ─────────────────────────────────────────
    print("  Building occupant keyframes...")
    marker_keys = {i: [] for i in range(total_occupants)}
    board_texts = []

    for record in timeline:
        tick      = record["tick"]
        violation = record["violation"]
        nodes     = record["agent_nodes"]
        frame     = tick * frames_per_tick

        for marker_id, node_id in enumerate(nodes):
            label    = node_to_label[node_id]
            floor    = node_to_floor[node_id]
            centroid = resolve(node_id)
            x, y, z  = _jitter(centroid, marker_id)

            # Colour logic — occupancy management, not fire alarm
            if violation and label == violation_room:
                colour = COLOUR_OVER         # red — in overcrowded room
            elif violation and floor == v_floor and label != violation_room:
                colour = COLOUR_REDIRECTED   # orange — on same floor, being redirected
            else:
                colour = COLOUR_NORMAL       # blue — normal

            marker_keys[marker_id].append([frame, x, y, z, *colour])

        board_texts.append(
            _build_board_text(record["snapshot"], violation, violation_room))

    # ── Floor colour keyframes ────────────────────────────────────────────
    print("  Building floor colour keyframes...")
    floor_keys = _build_floor_colour_timeline(
        timeline, frames_per_tick, violation_room)

    # ── IFC Pset write-back ───────────────────────────────────────────────
    print(f"  Writing ComplianceStatus=FAIL to IFC for {violation_room}...")
    pset_result = _write_compliance_pset(violation_room)
    print(f"  Pset result: {pset_result.get('status', pset_result)}")

    # ── Sign update with ADB citation ─────────────────────────────────────
    if v_floor:
        print(f"  Updating corridor sign for {v_floor} (ADB Cl.2.43)...")
        _update_signs_for_violation(violation_room, v_floor)

    total_frames = total_ticks * frames_per_tick

    # ── Write payload to disk ─────────────────────────────────────────────
    with open(INPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "marker_keys"      : marker_keys,
            "board_texts"      : board_texts,
            "floor_keys"       : floor_keys,
            "floor_collections": FLOOR_COLLECTIONS,
            "skip_prefixes"    : list(SKIP_PREFIXES),
            "frames_per_tick"  : frames_per_tick,
            "total_frames"     : total_frames,
        }, f)

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    # ── Blender bake script ───────────────────────────────────────────────
    code = f"""
import bpy, bmesh, json, traceback

try:
    with open(r"{INPUT_FILE}", "r", encoding="utf-8") as f:
        payload = json.load(f)

    marker_keys     = payload["marker_keys"]
    board_texts     = payload["board_texts"]
    floor_keys      = payload["floor_keys"]
    floor_colls     = payload["floor_collections"]
    skip            = tuple(payload["skip_prefixes"])
    frames_per_tick = payload["frames_per_tick"]
    total_frames    = payload["total_frames"]

    scene = bpy.context.scene
    scene.frame_start   = 0
    scene.frame_end     = total_frames
    scene.frame_current = 0

    # Material Preview so both markers and floor colours are visible
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.type = 'MATERIAL'

    # ════════════════════════════════════════════════════════════════════
    # PART 1 — Occupant cone markers
    # ════════════════════════════════════════════════════════════════════
    sc  = scene.collection
    col = bpy.data.collections.get('OccupantMarkers')
    if col is None:
        col = bpy.data.collections.new('OccupantMarkers')
    if col.name not in sc.children.keys():
        sc.children.link(col)
    col.hide_viewport = False
    col.hide_render   = False

    def _unhide(lc):
        if lc.collection.name == 'OccupantMarkers':
            lc.hide_viewport = False
            lc.exclude = False
        for c in lc.children:
            _unhide(c)
    _unhide(bpy.context.view_layer.layer_collection)

    for obj in list(bpy.data.objects):
        if obj.name.startswith('Occupant_'):
            bpy.data.objects.remove(obj, do_unlink=True)

    def _make_cone(name):
        mesh = bpy.data.meshes.new(name + '_m')
        bm   = bmesh.new()
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False,
                               segments=8, radius1=0.40,
                               radius2=0.08, depth=2.5)
        bm.to_mesh(mesh)
        bm.free()
        mat = bpy.data.materials.new(name=name + '_mat')
        mat.use_nodes = True
        mesh.materials.append(mat)
        return bpy.data.objects.new(name, mesh)

    print('Baking occupant markers...')
    created = 0
    for marker_id_str, keys in marker_keys.items():
        marker_id = int(marker_id_str)
        name = f'Occupant_{{marker_id:03d}}'
        obj  = _make_cone(name)
        col.objects.link(obj)
        obj.hide_viewport = False
        obj.hide_render   = False
        mat = obj.data.materials[0] if obj.data.materials else None

        for frame, x, y, z, r, g, b, a in keys:
            obj.location = (x, y, z + 0.85)
            obj.keyframe_insert(data_path='location', frame=frame)
            if mat and mat.use_nodes:
                node = mat.node_tree.nodes.get('Principled BSDF')
                if node:
                    bpy.context.scene.frame_set(frame)
                    node.inputs['Base Color'].default_value = (r, g, b, 1.0)
                    node.inputs['Base Color'].keyframe_insert(
                        data_path='default_value', frame=frame)

        for fc_owner in [
            obj.animation_data.action if (
                obj.animation_data and obj.animation_data.action) else None,
            mat.node_tree.animation_data.action if (
                mat and mat.node_tree and mat.node_tree.animation_data
                and mat.node_tree.animation_data.action) else None,
        ]:
            if fc_owner:
                for fc in fc_owner.fcurves:
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'LINEAR'

        created += 1

    print(f'Occupant markers baked: {{created}}')

    # ════════════════════════════════════════════════════════════════════
    # PART 2 — Floor colour keyframes
    # ════════════════════════════════════════════════════════════════════
    print('Baking floor colours...')
    floor_mats = {{}}

    for floor_name, col_name in floor_colls.items():
        mat_name = f'FS_FLOOR_{{floor_name[:2]}}'
        mat = bpy.data.materials.get(mat_name)
        if not mat:
            mat = bpy.data.materials.new(name=mat_name)
            mat.use_nodes = True
        floor_mats[floor_name] = mat

        floor_col = bpy.data.collections.get(col_name)
        if not floor_col:
            print(f'  NOT FOUND: {{col_name}}')
            continue

        count = 0
        for obj in floor_col.objects:
            if obj.type != 'MESH':
                continue
            if any(obj.name.startswith(p) for p in skip):
                continue
            if obj.data:
                obj.data.materials.clear()
                obj.data.materials.append(mat)
                count += 1
        print(f'  {{floor_name}}: {{count}} objects assigned')

    for floor_name, keys in floor_keys.items():
        mat  = floor_mats.get(floor_name)
        if not mat:
            continue
        node = mat.node_tree.nodes.get('Principled BSDF')
        if not node:
            continue
        for frame, r, g, b in keys:
            bpy.context.scene.frame_set(frame)
            node.inputs['Base Color'].default_value = (r, g, b, 1.0)
            node.inputs['Base Color'].keyframe_insert(
                data_path='default_value', frame=frame)
        if (mat.node_tree.animation_data and
                mat.node_tree.animation_data.action):
            for fc in mat.node_tree.animation_data.action.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'
        print(f'  {{floor_name}}: {{len(keys)}} colour keyframes baked')

    # ════════════════════════════════════════════════════════════════════
    # PART 3 — Board text (frame change handler)
    # ════════════════════════════════════════════════════════════════════
    print('Setting up board text handler...')
    if 'FireSafetyBoard' not in bpy.data.objects:
        bpy.ops.object.text_add(location=(-25.0, 0.0, 12.0))
        board = bpy.context.object
        board.name = 'FireSafetyBoard'
        board.data.size = 0.75
        board.data.align_x = 'LEFT'
        board.rotation_euler = (1.5708, 0, 0)
        bmat = bpy.data.materials.new('BoardText')
        bmat.use_nodes = True
        bnode = bmat.node_tree.nodes.get('Principled BSDF')
        if bnode:
            bnode.inputs['Base Color'].default_value      = (1.0, 0.85, 0.0, 1.0)
            bnode.inputs['Emission Color'].default_value   = (1.0, 0.85, 0.0, 1.0)
            bnode.inputs['Emission Strength'].default_value = 2.0
        board.data.materials.clear()
        board.data.materials.append(bmat)
    else:
        board = bpy.data.objects['FireSafetyBoard']
        board.location    = (-25.0, 0.0, 12.0)
        board.data.size   = 0.75

    scene['fs_board_texts']     = json.dumps(board_texts)
    scene['fs_frames_per_tick'] = frames_per_tick

    def _board_handler(scn):
        texts = scn.get('fs_board_texts')
        fpt   = scn.get('fs_frames_per_tick', 24)
        if not texts:
            return
        data = json.loads(texts)
        idx  = max(0, min(scn.frame_current // fpt, len(data) - 1))
        b    = bpy.data.objects.get('FireSafetyBoard')
        if b:
            b.data.body = data[idx]

    existing = [fn for fn in bpy.app.handlers.frame_change_pre
                if fn.__name__ == '_board_handler']
    for fn in existing:
        bpy.app.handlers.frame_change_pre.remove(fn)
    bpy.app.handlers.frame_change_pre.append(_board_handler)
    _board_handler(scene)

    # ════════════════════════════════════════════════════════════════════
    # PART 4 — Reset to frame 0 and redraw
    # ════════════════════════════════════════════════════════════════════
    bpy.context.scene.frame_set(0)
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()

    report = {{
        'status'       : 'ok',
        'markers_baked': created,
        'floors_baked' : len(floor_mats),
        'total_frames' : total_frames,
    }}

except Exception as e:
    report = {{
        'status' : 'error',
        'message': str(e),
        'trace'  : traceback.format_exc()
    }}

with open(r"{RESULT_FILE}", 'w', encoding='utf-8') as f:
    json.dump(report, f)
print('Bake complete:', report.get('status'))
"""
    print("  Sending to Blender (30–90s)...")
    send_to_blender(code)
    result = _read_result(timeout=180.0)
    result["total_ticks"]      = total_ticks
    result["frames_per_tick"]  = frames_per_tick
    result["total_frames"]     = total_frames
    result["violation_room"]   = violation_room
    result["violation_tick"]   = violation_tick
    return result