# bim/animation_baker.py
# Bakes per-agent occupancy management timeline into Blender keyframes.
#
# Fixes applied: 
# 1. Floor colour logic utilizes the violation event matrix and ignores initial 
#    warm-up clustering noise by gating checking behind the scenario trigger tick.
# 2. Agent occupant cones are similarly gated, keeping them uniformly blue until 
#    the official scenario event occurs.
# 3. Viewport optimization builds dynamic node trees using the 'Object Info' node 
#    to cleanly render real-time color transitions natively in MATERIAL Preview mode.

import json, os, random
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE
from bim.room_geometry import load_room_centroids
from bim.signage import update_sign
from sensors.agent_walk import simulate_agent_timeline
from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy

# ── Marker colours ────────────────────────────────────────────────────────────
COLOUR_NORMAL     = (0.15, 0.45, 0.90, 1.0)   # blue   — compliant
COLOUR_REDIRECTED = (0.95, 0.55, 0.10, 1.0)   # orange — same floor as violation
COLOUR_OVER       = (0.90, 0.10, 0.10, 1.0)   # red    — in violation room

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
    Floor colour logic with monitoring window:
      GREEN = normal operation
      RED   = violation detected (stays red for MONITOR_TICKS after detection)
      GREEN = monitoring window expired, system confirms resolved
    """
    G = BUILDING_GRAPH
    label_to_floor  = {d["label"]: d["floor"] for _, d in G.nodes(data=True)}
    violation_floor = label_to_floor.get(violation_room, "")

    # How many ticks to keep floor RED after violation triggers
    MONITOR_TICKS = 8

    # Find which tick the violation first triggers
    violation_start_tick = None
    for record in timeline:
        if record["violation"] is not None:
            violation_start_tick = record["tick"]
            break

    floor_keys = {fl: [] for fl in FLOOR_COLLECTIONS}

    for record in timeline:
        tick   = record["tick"]
        frame  = tick * frames_per_tick
        alerts = record["snapshot"].get("alerts", [])

        # Floors with genuine OVER alerts — filtered to ignore warm-up noise
        over_floors = set()
        if violation_start_tick is not None and tick >= violation_start_tick:
            for a in alerts:
                if a.get("severity") == "OVER":
                    fl = label_to_floor.get(a.get("label", ""))
                    if fl:
                        over_floors.add(fl)

        # Is the monitoring window still active?
        in_monitor_window = (
            violation_start_tick is not None and
            tick >= violation_start_tick and
            tick < violation_start_tick + MONITOR_TICKS
        )

        for floor_name in FLOOR_COLLECTIONS:
            if in_monitor_window and floor_name == violation_floor:
                # Monitoring window active — RED
                r, g, b = 0.90, 0.05, 0.05
            elif floor_name in over_floors:
                # Genuine overcrowding — RED
                r, g, b = 0.90, 0.05, 0.05
            else:
                # Compliant or resolved — GREEN
                r, g, b = 0.05, 0.70, 0.15

            floor_keys[floor_name].append((frame, r, g, b))

    return floor_keys


def _build_board_text(snap: dict, violation: dict,
                       violation_room: str) -> str:
    """Board text for one tick — occupancy management language, no evacuation."""
    lines = [
        "FIRE SAFETY DIGITAL TWIN",
        f"Tick: {snap['tick']}",
        "",
        f"Occupants: {snap['total_occ']}",
        "",
        "FLOOR OCCUPANCY:",
    ]

    for fl in FLOOR_ORDER:
        cnt = snap["by_floor"].get(fl, 0)
        if cnt > 0:
            short = (fl.replace("Ground Floor", "Ground")
                       .replace("First Floor", "First")
                       .replace("Second Floor", "Second")
                       .replace("Third Floor", "Third"))
            lines.append(f"  {short:12s}: {cnt}")

    lines.append("")

    if violation and snap.get("alerts"):
        over_rooms = [a for a in snap["alerts"] if a["severity"] == "OVER"]
        if over_rooms:
            lines.append(f"OVERCAPACITY ({len(over_rooms)}):")
            for a in over_rooms[:3]:
                lines.append(f"  {a['label']}: {a['current']}/{a['max']}"
                             f" (ADB Cl.2.43)")
        else:
            lines.append(f"Monitoring: {violation_room}")
            lines.append("Status: Occupancy within limits")
    elif violation:
        lines.append(f"Monitoring: {violation['node_label']}")
        lines.append("Status: Redirecting occupants")
    else:
        lines.append("Status: NORMAL — all rooms compliant")

    return "\n".join(lines)


def _write_compliance_pset(violation_room: str):
    """Writes ComplianceStatus=FAIL to the violation room's IFC Pset."""
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


FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}


def _update_signs_for_violation(violation_room: str, violation_floor: str):
    """Updates corridor sign for affected floor with ADB-cited message."""
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
    Full occupancy management animation bake.
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

    G              = BUILDING_GRAPH
    node_to_label  = {n: d["label"] for n, d in G.nodes(data=True)}
    node_to_floor  = {n: d["floor"]  for n, d in G.nodes(data=True)}
    label_to_floor = {d["label"]: d["floor"] for _, d in G.nodes(data=True)}
    violation_floor = label_to_floor.get(violation_room, "")

    # Find which tick the violation first triggers globally
    violation_start_tick = None
    for record in timeline:
        if record["violation"] is not None:
            violation_start_tick = record["tick"]
            break
            
    MONITOR_TICKS = 8

    # ── Occupant marker keyframes ─────────────────────────────────────────
    print("  Building occupant keyframes...")
    marker_keys = {i: [] for i in range(total_occupants)}
    board_texts = []

    for record in timeline:
        tick      = record["tick"]
        violation = record["violation"]
        nodes     = record["agent_nodes"]
        frame     = tick * frames_per_tick
        alerts    = record["snapshot"].get("alerts", [])

        # Track active violation zones for this specific frame
        over_rooms = set()
        over_floors = set()
        
        # Gated to isolate tracking and prevent initial warm-up noise on agent cones
        if violation_start_tick is not None and tick >= violation_start_tick:
            for a in alerts:
                if a.get("severity") == "OVER":
                    over_rooms.add(a.get("label", ""))
                    fl = label_to_floor.get(a.get("label", ""))
                    if fl:
                        over_floors.add(fl)

        in_monitor_window = (
            violation_start_tick is not None and
            tick >= violation_start_tick and
            tick < violation_start_tick + MONITOR_TICKS
        )

        for marker_id, node_id in enumerate(nodes):
            label    = node_to_label[node_id]
            floor    = node_to_floor[node_id]
            centroid = resolve(node_id)
            x, y, z  = _jitter(centroid, marker_id)

            # Color states track precisely with filtered structural definitions
            if label in over_rooms or (in_monitor_window and label == violation_room):
                colour = COLOUR_OVER
            elif floor in over_floors or (in_monitor_window and floor == violation_floor):
                colour = COLOUR_REDIRECTED
            else:
                colour = COLOUR_NORMAL

            marker_keys[marker_id].append([frame, x, y, z, *colour])

        board_texts.append(
            _build_board_text(record["snapshot"], violation, violation_room))

    # ── Floor colour keyframes ────────────────────────────────────────────
    print("  Building floor colour keyframes...")
    floor_keys = _build_floor_colour_timeline(
        timeline, frames_per_tick, violation_room)

    # ── IFC Pset + sign updates ───────────────────────────────────────────
    print(f"  Writing ComplianceStatus=FAIL to IFC for {violation_room}...")
    pset_result = _write_compliance_pset(violation_room)
    print(f"  Pset result: {pset_result.get('status', pset_result)}")

    if violation_floor:
        print(f"  Updating corridor sign for {violation_floor} (ADB Cl.2.43)...")
        _update_signs_for_violation(violation_room, violation_floor)

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

    # Explicitly enforce MATERIAL Shading Viewport Mode so node trees run natively
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.type = 'MATERIAL'

    # ════════════════════════════════════════════════════════════════
    # GENERATE COMPLIANT SHADERS FOR MATERIAL VIEWPORT RUNTIME
    # ════════════════════════════════════════════════════════════════
    # 1. Occupant Cones Shader
    occ_mat = bpy.data.materials.get('OccupantShader')
    if occ_mat is None:
        occ_mat = bpy.data.materials.new('OccupantShader')
    
    occ_mat.use_nodes = True
    occ_mat.node_tree.nodes.clear()
    
    nodes_occ = occ_mat.node_tree.nodes
    links_occ = occ_mat.node_tree.links
    
    out_node_occ  = nodes_occ.new('ShaderNodeOutputMaterial')
    bsdf_occ      = nodes_occ.new('ShaderNodeBsdfPrincipled')
    obj_info_occ  = nodes_occ.new('ShaderNodeObjectInfo')
    
    links_occ.new(bsdf_occ.outputs['BSDF'], out_node_occ.inputs['Surface'])
    links_occ.new(obj_info_occ.outputs['Color'], bsdf_occ.inputs['Base Color'])
    links_occ.new(obj_info_occ.outputs['Color'], bsdf_occ.inputs['Emission Color'])
    if 'Emission Strength' in bsdf_occ.inputs:
        bsdf_occ.inputs['Emission Strength'].default_value = 2.0

    # 2. Transparent Floor Safety Structural Shader
    floor_mat = bpy.data.materials.get('FloorSafetyShader')
    if floor_mat is None:
        floor_mat = bpy.data.materials.new('FloorSafetyShader')
        
    floor_mat.use_nodes = True
    floor_mat.blend_method = 'BLEND'  
    floor_mat.shadow_method = 'NONE'
    floor_mat.node_tree.nodes.clear()
    
    nodes_fl = floor_mat.node_tree.nodes
    links_fl = floor_mat.node_tree.links
    
    out_node_fl  = nodes_fl.new('ShaderNodeOutputMaterial')
    bsdf_fl      = nodes_fl.new('ShaderNodeBsdfPrincipled')
    obj_info_fl  = nodes_fl.new('ShaderNodeObjectInfo')
    
    links_fl.new(bsdf_fl.outputs['BSDF'], out_node_fl.inputs['Surface'])
    links_fl.new(obj_info_fl.outputs['Color'], bsdf_fl.inputs['Base Color'])
    if 'Alpha' in bsdf_fl.inputs:
        bsdf_fl.inputs['Alpha'].default_value = 0.35
    if 'Roughness' in bsdf_fl.inputs:
        bsdf_fl.inputs['Roughness'].default_value = 0.2
        
    # Clean slate for floor animations and base colors to fix Tick 0 caching issues
    for floor_name, col_name in floor_colls.items():
        floor_col = bpy.data.collections.get(col_name)
        if floor_col:
            for obj in floor_col.objects:
                if obj.type == 'MESH' and not any(obj.name.startswith(p) for p in skip):
                    if obj.animation_data:
                        obj.animation_data_clear()
                    obj.color = (0.05, 0.70, 0.15, 1.0)

    # ════════════════════════════════════════════════════════════════
    # PART 1 — Occupant cone markers (obj.color keyframes)
    # ════════════════════════════════════════════════════════════════
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
            lc.hide_viewport = False; lc.exclude = False
        for c in lc.children: _unhide(c)
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
        bm.to_mesh(mesh); bm.free()
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
        
        obj.data.materials.append(occ_mat)

        for frame, x, y, z, r, g, b, a in keys:
            obj.location = (x, y, z + 0.85)
            obj.color    = (r, g, b, a)
            obj.keyframe_insert(data_path='location', frame=frame)
            obj.keyframe_insert(data_path='color',    frame=frame)

        if obj.animation_data and obj.animation_data.action:
            for fc in obj.animation_data.action.fcurves:
                if fc.data_path == 'color':
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'CONSTANT'
                else:
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'LINEAR'

        created += 1

    print(f'Occupant markers baked: {{created}}')

    # ════════════════════════════════════════════════════════════════
    # PART 2 — Floor colour keyframes (obj.color on floor objects)
    # ════════════════════════════════════════════════════════════════
    print('Baking floor colours...')

    for floor_name, col_name in floor_colls.items():
        keys = floor_keys.get(floor_name, [])
        if not keys:
            continue

        floor_col = bpy.data.collections.get(col_name)
        if not floor_col:
            print(f'  NOT FOUND: {{col_name}}')
            continue

        floor_objs = [
            obj for obj in floor_col.objects
            if obj.type == 'MESH'
            and not any(obj.name.startswith(p) for p in skip)
        ]

        for obj in floor_objs:
            if floor_mat.name not in [m.name for m in obj.data.materials if m]:
                obj.data.materials.clear()
                obj.data.materials.append(floor_mat)

        for frame, r, g, b in keys:
            for obj in floor_objs:
                obj.color = (r, g, b, 1.0)
                obj.keyframe_insert(data_path='color', frame=frame)

        for obj in floor_objs:
            if obj.animation_data and obj.animation_data.action:
                for fc in obj.animation_data.action.fcurves:
                    if fc.data_path == 'color':
                        for kp in fc.keyframe_points:
                            kp.interpolation = 'CONSTANT'

        print(f'  {{floor_name}}: {{len(floor_objs)}} objects, {{len(keys)}} keyframes')

    # ════════════════════════════════════════════════════════════════
    # PART 3 — Board text (frame change handler)
    # ════════════════════════════════════════════════════════════════
    print('Setting up board text handler...')
    if 'FireSafetyBoard' not in bpy.data.objects:
        bpy.ops.object.text_add(location=(-30.0, -5.0, 16.0))
        board = bpy.context.object
        board.name = 'FireSafetyBoard'
        board.data.size = 0.85
        board.data.align_x = 'LEFT'
        board.rotation_euler = (1.5708, 0, 0)
        bmat = bpy.data.materials.new('BoardText')
        bmat.use_nodes = True
        bnode = bmat.node_tree.nodes.get('Principled BSDF')
        if bnode:
            bnode.inputs['Base Color'].default_value     = (1.0, 0.85, 0.0, 1.0)
            bnode.inputs['Emission Color'].default_value  = (1.0, 0.85, 0.0, 1.0)
            bnode.inputs['Emission Strength'].default_value = 2.0
        board.data.materials.clear()
        board.data.materials.append(bmat)
    else:
        board = bpy.data.objects['FireSafetyBoard']
        board.location    = (-30.0, -5.0, 16.0)
        board.data.size   = 0.85

    scene['fs_board_texts']     = json.dumps(board_texts)
    scene['fs_frames_per_tick'] = frames_per_tick

    def _board_handler(scn):
        texts = scn.get('fs_board_texts')
        fpt   = scn.get('fs_frames_per_tick', 24)
        if not texts: return
        data  = json.loads(texts)
        idx   = max(0, min(scn.frame_current // fpt, len(data) - 1))
        b     = bpy.data.objects.get('FireSafetyBoard')
        if b:  b.data.body = data[idx]

    existing = [fn for fn in bpy.app.handlers.frame_change_pre
                if fn.__name__ == '_board_handler']
    for fn in existing:
        bpy.app.handlers.frame_change_pre.remove(fn)
    bpy.app.handlers.frame_change_pre.append(_board_handler)
    _board_handler(scene)

    # ════════════════════════════════════════════════════════════════
    # PART 4 — Reset to frame 0 and redraw
    # ════════════════════════════════════════════════════════════════
    bpy.context.scene.frame_set(0)
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()

    report = {{
        'status'       : 'ok',
        'markers_baked': created,
        'floors_baked' : len(floor_colls),
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
    result = _read_result(timeout=300.0)
    result["total_ticks"]     = total_ticks
    result["frames_per_tick"] = frames_per_tick
    result["total_frames"]    = total_frames
    result["violation_room"]  = violation_room
    result["violation_tick"]  = violation_tick
    return result