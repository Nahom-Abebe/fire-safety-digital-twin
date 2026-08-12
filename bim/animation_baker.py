# bim/animation_baker.py
# Bakes per-agent occupancy management timeline into Blender keyframes.

import json, os, random
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE
from bim.room_geometry import load_room_centroids
from bim.signage import update_sign
from sensors.agent_walk import simulate_agent_timeline
from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy

# ── Marker colours ────────────────────────────────────────────────────────────
COLOUR_NORMAL      = (0.15, 0.45, 0.90, 1.0)   # blue   — compliant
COLOUR_REDIRECTED  = (0.95, 0.55, 0.10, 1.0)   # orange — same floor as violation
COLOUR_OVER        = (0.90, 0.10, 0.10, 1.0)   # red    — in violation room
COLOUR_WHEELCHAIR  = (0.60, 0.10, 0.80, 1.0)   # purple — mobility constrained

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

FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}

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
                                  violation_room) -> dict:
    """
    Floor colour logic gated behind scenario trigger tick.
    Returns all-green when violation_room is None (baseline).
    """
    G = BUILDING_GRAPH
    label_to_floor  = {d["label"]: d["floor"] for _, d in G.nodes(data=True)}
    violation_floor = (label_to_floor.get(violation_room, "")
                       if violation_room else "")

    MONITOR_TICKS = 8

    violation_start_tick = None
    if violation_room is not None:
        for record in timeline:
            if record["violation"] is not None:
                violation_start_tick = record["tick"]
                break

    floor_keys = {fl: [] for fl in FLOOR_COLLECTIONS}

    for record in timeline:
        tick  = record["tick"]
        frame = tick * frames_per_tick

        # Baseline — always green
        if violation_room is None or violation_start_tick is None:
            for floor_name in FLOOR_COLLECTIONS:
                floor_keys[floor_name].append((frame, 0.05, 0.70, 0.15))
            continue

        alerts = record["snapshot"].get("alerts", [])

        over_floors = set()
        if tick >= violation_start_tick:
            for a in alerts:
                if a.get("severity") == "OVER":
                    fl = label_to_floor.get(a.get("label", ""))
                    if fl:
                        over_floors.add(fl)

        in_monitor_window = (
            tick >= violation_start_tick and
            tick < violation_start_tick + MONITOR_TICKS
        )

        for floor_name in FLOOR_COLLECTIONS:
            if in_monitor_window and floor_name == violation_floor:
                r, g, b = 0.90, 0.05, 0.05
            elif floor_name in over_floors:
                r, g, b = 0.90, 0.05, 0.05
            else:
                r, g, b = 0.05, 0.70, 0.15
            floor_keys[floor_name].append((frame, r, g, b))

    return floor_keys


SIGN_RED   = [0.90, 0.05, 0.05, 1.0]
SIGN_GREEN = [0.05, 0.70, 0.15, 1.0]


def _build_sign_states_timeline(timeline: list,
                                 violation_room,
                                 blocked_exits: list = None,
                                 mobility_node: str = None,
                                 mobility_refuge: bool = False) -> list:
    """
    Per-tick sign payloads for the four primary corridor displays.
    Each entry is {sign_id: [text, colour]}.

    Correctness fixes:

    1. Per-floor specificity — each floor's sign reports the room that
       is actually over capacity on THAT floor, not the scenario's
       primary violation_room borrowed onto every red floor.

    2. Room-alert glitch at tick 0 — occupants are placed on random
       rooms at the very start, so pure chance can put a room over
       capacity before the scripted violation has even triggered,
       flashing a sign red then green a tick later. Room-based alerts
       (Priority 3 below) only ever fire from violation_start_tick
       onward — before that, nothing but a structurally blocked exit
       can turn a sign red.

    3. Exit-obstruction priority (TS-02) — a floor whose exit is in
       blocked_exits gets a directional redirect message instead of
       room-occupancy text, once the violation is active. Same rhythm
       as every other scenario: green until the trigger tick, then red
       for the rest of the run — not red from frame 0 regardless of
       when anything actually happens. Direction is read from the
       sign's own id suffix (_N / _S).

    4. Mobility-refuge floor (TS-04, mobility_refuge=True) — once the
       violation triggers, the floor holding the tracked occupant shows
       a wheelchair-specific refuge instruction instead of generic
       room-occupancy text, and never reverts to green afterwards, even
       if the underlying room's count later drops — the refuge
       situation stays open until the scenario ends. Every other floor
       still correctly returns to green once its own alert clears,
       which is the whole point of the simulation (rooms brought back
       into compliance).
    """
    G = BUILDING_GRAPH
    label_to_floor  = {d["label"]: d["floor"] for _, d in G.nodes(data=True)}
    violation_floor = (label_to_floor.get(violation_room, "")
                       if violation_room else "")
    mobility_floor  = (label_to_floor.get(mobility_node, "")
                       if mobility_refuge and mobility_node else "")

    blocked_exit_floors = set()
    for exit_label in (blocked_exits or []):
        enode = next((n for n, d in G.nodes(data=True)
                      if d["label"] == exit_label), None)
        if enode is not None:
            blocked_exit_floors.add(G.nodes[enode]["floor"])

    def _direction(sign_id):
        if sign_id.endswith("_N"): return "North", "South"
        if sign_id.endswith("_S"): return "South", "North"
        return None, None

    violation_start_tick = None
    if violation_room is not None:
        for record in timeline:
            if record["violation"] is not None:
                violation_start_tick = record["tick"]
                break

    # Bridges the brief gap between "violation just triggered" and the
    # alerts list catching up — not applied to the mobility-refuge floor,
    # which stays open for the whole run regardless of this window.
    MONITOR_TICKS = 8

    sign_timeline = []

    for record in timeline:
        tick   = record["tick"]
        alerts = record["snapshot"].get("alerts", [])

        violation_active = (
            violation_start_tick is not None and tick >= violation_start_tick
        )
        in_monitor = (
            violation_active and tick < violation_start_tick + MONITOR_TICKS
        )

        frame_signs = {}
        for fl_name, sign_id in FLOOR_SIGNS.items():

            # Priority 1 -- exit obstruction on this floor. Gated by
            # violation_active so it follows the same rhythm as every
            # other scenario (green until the violation tick, then
            # reacts) rather than showing red from frame 0 regardless
            # of the scripted trigger. Once triggered it stays red for
            # the rest of the run, same as Priority 2 below -- the
            # exit really does remain blocked for the whole scenario,
            # it just shouldn't announce that before anything has
            # actually happened yet.
            if fl_name in blocked_exit_floors and violation_active:
                this_dir, alt_dir = _direction(sign_id)
                if this_dir:
                    frame_signs[sign_id] = [
                        f"{this_dir} Corridor BLOCKED\n"
                        f"Use {alt_dir} Exit",
                        SIGN_RED,
                    ]
                else:
                    frame_signs[sign_id] = [
                        "Primary Exit BLOCKED\nUse Alternative Route",
                        SIGN_RED,
                    ]
                continue

            # Priority 2 -- mobility refuge floor, permanent once triggered
            if mobility_refuge and fl_name == mobility_floor and violation_active:
                frame_signs[sign_id] = [
                    "Wheelchair users\n"
                    "Proceed to refuge\n"
                    "point -- await staff",
                    SIGN_RED,
                ]
                continue

            # Priority 3 -- this floor's own over-capacity room(s), only
            # once the scripted violation is actually active
            if violation_active:
                floor_alerts = [a for a in alerts
                                if a.get("severity") == "OVER"
                                and label_to_floor.get(a.get("label", "")) == fl_name]
                if floor_alerts:
                    a = floor_alerts[0]
                    frame_signs[sign_id] = [
                        f"Room {a['label']} is full\n"
                        f"Please use alternative areas",
                        SIGN_RED,
                    ]
                    continue

                if in_monitor and fl_name == violation_floor:
                    frame_signs[sign_id] = [
                        f"Room {violation_room} is full\n"
                        f"Please use alternative areas",
                        SIGN_RED,
                    ]
                    continue

            frame_signs[sign_id] = ["Status: CLEAR\nAll routes open", SIGN_GREEN]

        sign_timeline.append(frame_signs)

    return sign_timeline



def _build_board_text(snap: dict, violation,
                       violation_room) -> str:
    """Board text — ADB citations go here, not on physical signs."""
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
                       .replace("First Floor",  "First")
                       .replace("Second Floor", "Second")
                       .replace("Third Floor",  "Third"))
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

    # Mobility escalation alert — only ever appears once the tracked
    # occupant has genuinely reached and settled at their floor's refuge
    # point (sensors/agent_walk.py only sets at_refuge True when the
    # scenario explicitly enables refuge-seeking, i.e. TS-04). Routine
    # "currently in room X" chatter is intentionally not shown for
    # every scenario — this is a manager-facing alert, not a tracker.
    ms = snap.get("mobility_status")
    if ms and ms.get("at_refuge"):
        lines.append("")
        lines.append("ALERT: Wheelchair user awaiting")
        lines.append(f"assistance — {ms['floor']} refuge point")

    # TS-02: note blocked exit on board — only once the violation has
    # actually triggered. blocked_exits is structurally populated from
    # tick 0 (the exit really is blocked from the start of the walk),
    # but announcing it before anything has happened yet is the same
    # premature-disclosure problem the corridor signs had.
    if violation and snap.get("blocked_exits"):
        lines.append(f"Exit blocked: {snap['blocked_exits']}")

    return "\n".join(lines)


def _write_compliance_pset(violation_room: str):
    """Writes ComplianceStatus=FAIL — only called when violation_room is not None."""
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


def _update_signs_for_violation(violation_room: str, violation_floor: str):
    """
    Updates corridor sign — simple occupant text only, no ADB on signs.
    ADB citations go on the board and in logs.
    """
    sign_id = FLOOR_SIGNS.get(violation_floor)
    if not sign_id:
        return
    update_sign(
        sign_id,
        f"Room {violation_room} is full — please use alternative areas",
        "ALTERNATE",
        "ADB Vol2 Clause 2.43 — residential care home bedroom occupancy"
    )


# ── Main bake function ────────────────────────────────────────────────────────

def bake_animation(total_occupants: int = 80,
                   total_ticks: int = 25,
                   violation_tick: int = 5,
                   violation_room = "0-4",
                   frames_per_tick: int = 24,
                   seed: int = 42,
                   blocked_exits: list = None,
                   multi_violations: list = None,
                   mobility_node: str = None,
                   mobility_refuge: bool = False) -> dict:
    """
    Full occupancy management animation bake.

    Parameters
    ----------
    blocked_exits : list of str
        Graph labels of exit nodes to block (TS-02). Passed to
        simulate_agent_timeline() so occupants genuinely avoid these
        exits, AND to the sign builder so the affected floor's corridor
        sign shows a directional redirect instead of room-occupancy text.
    multi_violations : list of dict
        Additional violation rooms beyond the primary one (TS-03).
        Format: [{"room": "3-14", "tick": 4}]
    mobility_node : str | None
        Room label of the mobility-constrained occupant. That cone is
        coloured purple in the baked keyframes for every non-baseline
        scenario, regardless of mobility_refuge.
    mobility_refuge : bool
        When True (TS-04), the mobility marker is drawn toward and then
        settles permanently at its floor's corridor node once the
        violation is active, and the board surfaces an escalation
        alert once it arrives. When False, the marker still avoids
        stairs but otherwise walks like any other occupant — no
        refuge-seeking, no alert.
    """
    centroids = load_room_centroids()
    resolve   = _make_resolver(centroids)

    is_baseline = (violation_room is None or violation_tick >= 999)

    print("  Simulating agent timeline (occupancy management)...")
    timeline = simulate_agent_timeline(
        total_occupants  = total_occupants,
        total_ticks      = total_ticks,
        violation_tick   = 999 if is_baseline else violation_tick,
        violation_room   = violation_room if not is_baseline else "0-4",
        seed             = seed,
        blocked_exits    = blocked_exits or [],
        multi_violations = multi_violations or [],
        mobility_node    = mobility_node,
        mobility_refuge  = mobility_refuge,
    )

    G              = BUILDING_GRAPH
    node_to_label  = {n: d["label"] for n, d in G.nodes(data=True)}
    node_to_floor  = {n: d["floor"]  for n, d in G.nodes(data=True)}
    label_to_floor = {d["label"]: d["floor"] for _, d in G.nodes(data=True)}
    violation_floor = (label_to_floor.get(violation_room, "")
                       if not is_baseline else "")

    # Extract mobility_marker_id from the first timeline record
    mobility_marker_id = timeline[0].get("mobility_marker_id") if timeline else None

    violation_start_tick = None
    if not is_baseline:
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

        over_rooms  = set()
        over_floors = set()
        if not is_baseline and violation_start_tick is not None and tick >= violation_start_tick:
            for a in alerts:
                if a.get("severity") == "OVER":
                    over_rooms.add(a.get("label", ""))
                    fl = label_to_floor.get(a.get("label", ""))
                    if fl:
                        over_floors.add(fl)

        in_monitor_window = (
            not is_baseline and
            violation_start_tick is not None and
            tick >= violation_start_tick and
            tick < violation_start_tick + MONITOR_TICKS
        )

        for marker_id, node_id in enumerate(nodes):
            label    = node_to_label[node_id]
            floor    = node_to_floor[node_id]
            centroid = resolve(node_id)
            x, y, z  = _jitter(centroid, marker_id)

            # Wheelchair marker (TS-04) — always purple
            if marker_id == mobility_marker_id and mobility_node is not None:
                colour = COLOUR_WHEELCHAIR
            elif label in over_rooms or (in_monitor_window and label == violation_room):
                colour = COLOUR_OVER
            elif floor in over_floors or (in_monitor_window and floor == violation_floor):
                colour = COLOUR_REDIRECTED
            else:
                colour = COLOUR_NORMAL

            marker_keys[marker_id].append([frame, x, y, z, *colour])

        board_texts.append(
            _build_board_text(record["snapshot"], violation,
                              violation_room if not is_baseline else None))

    # ── Floor & sign timelines ────────────────────────────────────────────
    print("  Building floor colour & sign state keyframes...")
    floor_keys  = _build_floor_colour_timeline(
        timeline, frames_per_tick, None if is_baseline else violation_room)
    sign_states = _build_sign_states_timeline(
        timeline, None if is_baseline else violation_room,
        blocked_exits   = blocked_exits or [],
        mobility_node   = mobility_node if not is_baseline else None,
        mobility_refuge = mobility_refuge if not is_baseline else False)

    # ── IFC Pset + sign updates — baseline guard ──────────────────────────
    if is_baseline:
        print("  Baseline scenario — IFC Pset write suppressed")
        print("  Corridor sign updates suppressed")
    else:
        print(f"  Writing ComplianceStatus=FAIL to IFC for {violation_room}...")
        pset_result = _write_compliance_pset(violation_room)
        print(f"  Pset result: {pset_result.get('status', pset_result)}")
        if violation_floor:
            print(f"  Updating corridor sign for {violation_floor}...")
            _update_signs_for_violation(violation_room, violation_floor)

    total_frames = total_ticks * frames_per_tick

    # ── Write payload to disk ─────────────────────────────────────────────
    with open(INPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "marker_keys"       : marker_keys,
            "board_texts"       : board_texts,
            "sign_states"       : sign_states,
            "floor_keys"        : floor_keys,
            "floor_collections" : FLOOR_COLLECTIONS,
            "skip_prefixes"     : list(SKIP_PREFIXES),
            "frames_per_tick"   : frames_per_tick,
            "total_frames"      : total_frames,
            "mobility_marker_id": mobility_marker_id,
        }, f)

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    # ── Blender bake script ───────────────────────────────────────────────
    code_template = """
import bpy, bmesh, json, traceback

def _set_emission(bsdf_node, color, strength=2.0):
    for name in ['Emission Color', 'Emission']:
        if name in bsdf_node.inputs:
            bsdf_node.inputs[name].default_value = color
            break
    if 'Emission Strength' in bsdf_node.inputs:
        bsdf_node.inputs['Emission Strength'].default_value = strength

try:
    with open(r"__INPUT_FILE__", "r", encoding="utf-8") as f:
        payload = json.load(f)

    marker_keys        = payload["marker_keys"]
    board_texts        = payload["board_texts"]
    sign_states        = payload.get("sign_states", [])
    floor_keys         = payload["floor_keys"]
    floor_colls        = payload["floor_collections"]
    skip               = tuple(payload["skip_prefixes"])
    frames_per_tick    = payload["frames_per_tick"]
    total_frames       = payload["total_frames"]
    mobility_marker_id = payload.get("mobility_marker_id")

    scene = bpy.context.scene
    scene.frame_start   = 0
    scene.frame_end     = total_frames
    scene.frame_current = 0

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.type = 'MATERIAL'

    # Occupant material — uses obj.color via Object Info
    occ_mat = bpy.data.materials.get('OccupantShader')
    if occ_mat is None:
        occ_mat = bpy.data.materials.new('OccupantShader')
    occ_mat.use_nodes = True
    occ_mat.node_tree.nodes.clear()
    nodes_occ = occ_mat.node_tree.nodes
    links_occ = occ_mat.node_tree.links
    out_occ   = nodes_occ.new('ShaderNodeOutputMaterial')
    bsdf_occ  = nodes_occ.new('ShaderNodeBsdfPrincipled')
    info_occ  = nodes_occ.new('ShaderNodeObjectInfo')
    links_occ.new(bsdf_occ.outputs['BSDF'], out_occ.inputs['Surface'])
    links_occ.new(info_occ.outputs['Color'], bsdf_occ.inputs['Base Color'])
    em = 'Emission Color' if 'Emission Color' in bsdf_occ.inputs else 'Emission'
    if em in bsdf_occ.inputs:
        links_occ.new(info_occ.outputs['Color'], bsdf_occ.inputs[em])
    if 'Emission Strength' in bsdf_occ.inputs:
        bsdf_occ.inputs['Emission Strength'].default_value = 2.0

    # Floor material
    floor_mat = bpy.data.materials.get('FloorSafetyShader')
    if floor_mat is None:
        floor_mat = bpy.data.materials.new('FloorSafetyShader')
    floor_mat.use_nodes = True
    if hasattr(floor_mat, 'blend_method'):
        floor_mat.blend_method = 'BLEND'
    floor_mat.node_tree.nodes.clear()
    nodes_fl = floor_mat.node_tree.nodes
    links_fl = floor_mat.node_tree.links
    out_fl   = nodes_fl.new('ShaderNodeOutputMaterial')
    bsdf_fl  = nodes_fl.new('ShaderNodeBsdfPrincipled')
    info_fl  = nodes_fl.new('ShaderNodeObjectInfo')
    links_fl.new(bsdf_fl.outputs['BSDF'], out_fl.inputs['Surface'])
    links_fl.new(info_fl.outputs['Color'], bsdf_fl.inputs['Base Color'])
    if 'Alpha'     in bsdf_fl.inputs: bsdf_fl.inputs['Alpha'].default_value = 0.35
    if 'Roughness' in bsdf_fl.inputs: bsdf_fl.inputs['Roughness'].default_value = 0.2

    for floor_name, col_name in floor_colls.items():
        floor_col = bpy.data.collections.get(col_name)
        if floor_col:
            for obj in floor_col.objects:
                if obj.type == 'MESH' and not any(obj.name.startswith(p) for p in skip):
                    if obj.animation_data:
                        obj.animation_data_clear()
                    obj.color = (0.05, 0.70, 0.15, 1.0)

    # PART 1 — Occupant cone markers
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
        name = f'Occupant_{marker_id:03d}'
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
                interp = 'CONSTANT' if fc.data_path == 'color' else 'LINEAR'
                for kp in fc.keyframe_points:
                    kp.interpolation = interp
        created += 1

    print(f'Occupant markers baked: {created}')
    if mobility_marker_id is not None:
        print(f'Wheelchair marker ID: {mobility_marker_id} (purple throughout)')

    # PART 2 — Floor colour keyframes
    print('Baking floor colours...')
    for floor_name, col_name in floor_colls.items():
        keys = floor_keys.get(floor_name, [])
        if not keys: continue
        floor_col = bpy.data.collections.get(col_name)
        if not floor_col:
            print(f'  NOT FOUND: {col_name}'); continue
        floor_objs = [
            obj for obj in floor_col.objects
            if obj.type == 'MESH' and not any(obj.name.startswith(p) for p in skip)
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
        print(f'  {floor_name}: {len(floor_objs)} objects, {len(keys)} keyframes')

    # PART 3 — Board & sign frame handler
    print('Setting up board and signage frame handler...')
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
            bnode.inputs['Base Color'].default_value = (1.0, 0.85, 0.0, 1.0)
            _set_emission(bnode, (1.0, 0.85, 0.0, 1.0), 2.0)
        board.data.materials.clear()
        board.data.materials.append(bmat)
    else:
        board = bpy.data.objects['FireSafetyBoard']
        board.location  = (-30.0, -5.0, 16.0)
        board.data.size = 0.85

    scene['fs_board_texts']     = json.dumps(board_texts)
    scene['fs_sign_states']     = json.dumps(sign_states)
    scene['fs_frames_per_tick'] = frames_per_tick

    def _board_handler(scn):
        texts = scn.get('fs_board_texts')
        fpt   = scn.get('fs_frames_per_tick', 24)
        if not texts: return
        data = json.loads(texts)
        idx  = max(0, min(scn.frame_current // fpt, len(data) - 1))
        b = bpy.data.objects.get('FireSafetyBoard')
        if b: b.data.body = data[idx]
        sign_data_str = scn.get('fs_sign_states')
        if sign_data_str:
            sign_data     = json.loads(sign_data_str)
            current_signs = sign_data[idx] if idx < len(sign_data) else {}
            # sign_id (e.g. 'SIGN_F0_CORRIDOR_N') is the dict key.
            # The actual objects are named SignText_<id> / SignPanel_<id> —
            # a plain lookup on sign_id itself always returned None, which
            # is why the panels never updated. Fixed here.
            for sign_id, payload in current_signs.items():
                text_val, colour = payload[0], payload[1]
                txt_obj = bpy.data.objects.get('SignText_' + sign_id)
                if txt_obj and hasattr(txt_obj.data, 'body'):
                    txt_obj.data.body = text_val
                panel_obj = bpy.data.objects.get('SignPanel_' + sign_id)
                if panel_obj and panel_obj.data and panel_obj.data.materials:
                    mat = panel_obj.data.materials[0]
                    node = (mat.node_tree.nodes.get('Principled BSDF')
                            if mat.node_tree else None)
                    if node:
                        node.inputs['Base Color'].default_value = colour
                        if 'Emission Color' in node.inputs:
                            node.inputs['Emission Color'].default_value = colour
                        if 'Emission Strength' in node.inputs:
                            node.inputs['Emission Strength'].default_value = (
                                2.5 if colour[0] > 0.5 else 1.5)

    existing = [fn for fn in bpy.app.handlers.frame_change_pre
                if fn.__name__ == '_board_handler']
    for fn in existing:
        bpy.app.handlers.frame_change_pre.remove(fn)
    bpy.app.handlers.frame_change_pre.append(_board_handler)
    _board_handler(scene)

    # PART 4 — Reset frame & redraw
    bpy.context.scene.frame_set(0)
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()

    report = {
        'status'       : 'ok',
        'markers_baked': created,
        'floors_baked' : len(floor_colls),
        'total_frames' : total_frames,
    }

except Exception as e:
    report = {
        'status' : 'error',
        'message': str(e),
        'trace'  : traceback.format_exc()
    }

with open(r"__RESULT_FILE__", 'w', encoding='utf-8') as f:
    json.dump(report, f)
print('Bake complete:', report.get('status'))
"""
    code = (code_template
            .replace("__INPUT_FILE__",  INPUT_FILE)
            .replace("__RESULT_FILE__", RESULT_FILE))

    print("  Sending to Blender (30-90s)...")
    send_to_blender(code)
    result = _read_result(timeout=300.0)
    result["total_ticks"]          = total_ticks
    result["frames_per_tick"]      = frames_per_tick
    result["total_frames"]         = total_frames
    result["violation_room"]       = violation_room
    result["violation_tick"]       = violation_tick
    result["mobility_marker_id"]   = mobility_marker_id
    return result