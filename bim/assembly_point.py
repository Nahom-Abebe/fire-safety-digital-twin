# bim/assembly_point.py
# Creates and manages the assembly point marker outside the building.
#
# Fixes applied:
#
#   1. compute_evacuation_paths() was replaced with
#      compute_signage_aware_evacuation_paths() — a real networkx
#      shortest-path per occupant's actual room, not a flattened
#      3-hop guess. Any node whose sign is currently marked
#      sign_blocked (the SAME graph property sensor_sim.py's normal-
#      operation movement already reacts to, set by
#      update_sign_status()) is heavily penalised in the path search,
#      so an evacuating occupant naturally avoids whatever exit the
#      signage is currently telling them to avoid — using a signal
#      that's already real and already meaningful elsewhere in the
#      project, not a new rule invented just for escalation.
#
#   2. animate_evacuation_via_paths() used STEP_SIZE=0.12 as a
#      fraction of remaining distance per step — proportional
#      stepping, which covers most of the journey in the first few
#      steps then barely moves for the rest, making movement nearly
#      invisible. This is the exact bug already diagnosed and fixed
#      with fixed-speed stepping inside manager_panel.py's own
#      escalate handler — but this function wasn't being called by
#      anything at the time, so the fix never made it back here. Now
#      uses the same fixed-speed (0.35m/step) approach.
#
#   3. Paths are now written to a temp file and read by the Blender
#      script, matching the pattern already proven in
#      manager_panel.py, rather than embedding a potentially large
#      JSON payload directly inside an f-string.

import os, json, random
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE

ASSEMBLY_X = 16.0
ASSEMBLY_Y = -35.0
ASSEMBLY_Z = 0.0

REDIRECT_COUNT = 5

BIM_DIR          = os.path.dirname(os.path.abspath(__file__))
EVAC_PATHS_FILE  = os.path.join(BIM_DIR, "_full_evac_paths.json").replace("\\", "/")


# ── Create assembly point marker ──────────────────────────────────────────────

def create_assembly_point() -> dict:
    """
    Places a green glowing circle outside the building with a label.
    Call once from phase1_setup.py.
    """
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, bmesh, json, traceback
try:
    for obj in list(bpy.data.objects):
        if obj.name.startswith('AssemblyPoint'):
            bpy.data.objects.remove(obj, do_unlink=True)
    for mat in list(bpy.data.materials):
        if mat.name.startswith('AssemblyMat') or mat.name.startswith('AssemblyText'):
            bpy.data.materials.remove(mat)

    mesh = bpy.data.meshes.new('AssemblyPoint_m')
    bm   = bmesh.new()
    bmesh.ops.create_circle(bm, cap_ends=True, cap_tris=False,
                             segments=32, radius=4.0)
    bm.to_mesh(mesh); bm.free()
    circle = bpy.data.objects.new('AssemblyPoint_Circle', mesh)
    bpy.context.scene.collection.objects.link(circle)
    circle.location = ({ASSEMBLY_X}, {ASSEMBLY_Y}, {ASSEMBLY_Z} + 0.05)

    mat = bpy.data.materials.new('AssemblyMat')
    mat.use_nodes = True
    n = mat.node_tree.nodes.get('Principled BSDF')
    if n:
        n.inputs['Base Color'].default_value      = (0.0, 0.85, 0.2, 1.0)
        n.inputs['Emission Color'].default_value   = (0.0, 0.85, 0.2, 1.0)
        n.inputs['Emission Strength'].default_value = 2.0
        n.inputs['Alpha'].default_value = 0.8
    mat.blend_method = 'BLEND'
    mesh.materials.append(mat)

    tmat = bpy.data.materials.new('AssemblyTextMat')
    tmat.use_nodes = True
    tn = tmat.node_tree.nodes.get('Principled BSDF')
    if tn:
        tn.inputs['Base Color'].default_value      = (1.0, 1.0, 1.0, 1.0)
        tn.inputs['Emission Color'].default_value   = (1.0, 1.0, 1.0, 1.0)
        tn.inputs['Emission Strength'].default_value = 3.0

    bpy.ops.object.text_add(
        location=({ASSEMBLY_X}, {ASSEMBLY_Y}, {ASSEMBLY_Z} + 1.5))
    txt = bpy.context.object
    txt.name           = 'AssemblyPoint_Label'
    txt.data.body      = 'EMERGENCY ASSEMBLY POINT'
    txt.data.size      = 1.0
    txt.data.align_x   = 'CENTER'
    txt.rotation_euler = (1.5708, 0, 0)
    txt.data.materials.clear()
    txt.data.materials.append(tmat)

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()

    report = {{'status':'ok','location':[{ASSEMBLY_X},{ASSEMBLY_Y},{ASSEMBLY_Z}]}}
except Exception as e:
    import traceback
    report = {{'status':'error','message':str(e),'trace':traceback.format_exc()}}
with open(r"{RESULT_FILE}","w",encoding="utf-8") as f:
    json.dump(report,f)
print('Assembly point:', report.get('status'))
"""
    send_to_blender(code)
    return _read_result(timeout=20.0)


# ── Signage-aware evacuation paths ─────────────────────────────────────────────

def _resolve_occupant_rooms_from_blender(centroids: dict) -> dict:
    """
    Returns {marker_id_str: room_label} by finding, for every
    Occupant_NNN object currently in the Blender scene, its nearest
    room/corridor centroid.

    MUST be called from code executing inside Blender (imports bpy
    internally). This exists because sensors/sensor_sim.py's
    occupancy state is plain Python module state living in whichever
    EXTERNAL script last called initialise_occupants()/move_occupants()
    (phase1_setup.py, phase2_runner.py, live_agent_runner.py) — each
    of those is a SEPARATE process talking to Blender over a socket,
    never sharing memory with Blender's own embedded interpreter.
    manager_panel.py's _escalate() is baked into and runs entirely
    inside Blender's own process (install_manager_panel() sends it
    over the socket once; from then on it's Blender's own operator
    code) — a fresh `from sensors.sensor_sim import get_sensor_snapshot`
    called from there imports a never-initialised copy of that module
    and always returns zero occupancy, regardless of what any
    external script has done. The cones' actual current position in
    the scene is the only thing that's always an accurate reflection
    of where everyone is, independent of which process last moved
    them or whether any tick-driving script has run at all since a
    one-time static phase1_setup.py placement — so that's the source
    of truth used here.

    Fix applied: this previously compared X/Y position only, ignoring
    height (Z) entirely. Every floor stacks directly on top of the
    one below at nearly the same room-layout footprint, so a cone
    genuinely on F2 or F3 could match an F1 room's centroid just
    because it happened to be closer (or tied) in a flat 2D
    comparison — floor identity was never actually part of the
    calculation. This is exactly what caused the wheelchair user to
    consistently end up routed to F1 regardless of which floor they
    actually started on: whatever tie-breaking fell out of dict
    iteration order produced the same wrong floor every time, not a
    random one. Real floor separation is 3m (F0=0.0, F1=3.0, F2=6.0,
    F3=9.0), far larger than any jitter noise, so including Z in the
    distance calculation makes floor identification unambiguous.
    """
    import bpy

    label_positions = list(centroids.items())
    occupant_rooms  = {}

    for obj in bpy.data.objects:
        if not obj.name.startswith("Occupant_"):
            continue
        # Skip any cone already marked evacuated/at-refuge by a
        # previous ESCALATE press — see animate_evacuation_via_paths()
        # for where this flag gets set, and _reset() in
        # manager_panel.py for where it gets cleared. Without this,
        # pressing ESCALATE a second time recomputed a path for every
        # cone including ones already standing outside at the
        # assembly point — since this function only knows about ROOM
        # centroids, an already-evacuated cone's position got matched
        # to whatever real room was geometrically nearest to the
        # assembly point, and the resulting path walked it back INTO
        # the building before re-evacuating.
        if obj.get("fs_evacuated", False):
            continue

        mid = obj.name.split("_", 1)[1]
        x, y, z = obj.location.x, obj.location.y, obj.location.z

        best_label, best_dist = None, None
        for label, c in label_positions:
            # Full 3D distance, not just X/Y — see fix note above.
            # Centroid z is stored at floor level; cones sit at
            # z + 0.85 (person-height offset, same convention
            # occupant_markers.py uses when placing them).
            d = ((c["x"] - x) ** 2 + (c["y"] - y) ** 2
                 + ((c["z"] + 0.85) - z) ** 2)
            if best_dist is None or d < best_dist:
                best_dist, best_label = d, label

        if best_label is not None:
            occupant_rooms[mid] = best_label

    return occupant_rooms


def _resolve_wheelchair_cones_from_blender() -> set:
    """
    Returns the set of marker_id strings currently marked with the
    WheelchairMat material — the same material both fix_and_bake.py's
    TS-04 marking and occupant_markers.py's baseline Phase 1 marker
    apply, so this works regardless of which one placed it. Read from
    the live scene, not sensor_sim state, for the same reason
    _resolve_occupant_rooms_from_blender() does.
    """
    import bpy
    wheelchair_mids = set()
    for obj in bpy.data.objects:
        if not obj.name.startswith("Occupant_"):
            continue
        if obj.data and obj.data.materials and obj.data.materials[0]:
            if obj.data.materials[0].name == "WheelchairMat":
                wheelchair_mids.add(obj.name.split("_", 1)[1])
    return wheelchair_mids


def _floor_corridor_nodes(G) -> dict:
    """Maps floor name -> its corridor node id — the same refuge
    target agent_walk.py's TS-04 mobility_refuge behaviour already
    uses for a wheelchair occupant during normal operation."""
    mapping = {}
    for n, d in G.nodes(data=True):
        if d.get("node_type") == "corridor" and d["floor"] not in mapping:
            mapping[d["floor"]] = n
    return mapping


def compute_signage_aware_evacuation_paths(centroids: dict,
                                           occupant_rooms: dict = None):
    """
    Computes a real graph shortest-path route from every occupant's
    current room to the assembly point, genuinely reacting to
    whatever the corridor signage currently says.

    Any node marked sign_blocked=True (set by sensors.sensor_sim's
    update_sign_status() — the same mechanism normal-operation
    movement already reacts to via _edge_probability()) is heavily
    penalised in the path search, so an evacuating occupant naturally
    routes around whatever exit or stairwell the signage is currently
    telling them to avoid. If nothing is currently marked blocked
    (e.g. a fresh phase1_setup.py run with no prior scenario active),
    this reduces to an ordinary shortest path — still real graph
    pathfinding, not a fixed hardcoded route.

    occupant_rooms : {marker_id_str: room_label} — where each cone
        currently is. If not supplied, resolved automatically from
        Blender's live scene via _resolve_occupant_rooms_from_blender()
        — this is what manager_panel.py's _escalate() relies on,
        since it runs inside Blender's own process where a fresh
        sensor_sim snapshot is always empty (see that function's
        docstring). A caller running in a process that DOES have a
        genuinely populated sensor_sim state (e.g. live_agent_runner.py,
        which calls initialise_occupants()/move_occupants() locally)
        could pass its own real snapshot-derived mapping instead.

    Wheelchair handling: a cone marked WheelchairMat (see
    _resolve_wheelchair_cones_from_blender()) currently on F0 Ground
    Floor is treated identically to every other cone — no stairs are
    needed to reach the exterior from ground level, so ordinary
    self-evacuation is realistic. A wheelchair cone on F1/F2/F3 gets a
    SHORT path ending at its own floor's corridor node (refuge) rather
    than continuing to assembly — matching agent_walk.py's TS-04
    mobility_refuge behaviour, and reflecting that standard fire
    escape stairs generally aren't usable by a non-ambulant wheelchair
    user; real ADB practice routes them to a refuge for assisted
    evacuation, not down stairs to the exterior. No special-casing is
    needed in animate_evacuation_via_paths()'s completion check — that
    cone's entry in the returned dict simply ends sooner, at a
    different final waypoint, and the existing per-cone "walk to your
    own last waypoint, then stop" logic handles the rest correctly.

    Fix applied — stairwell floating: room_geometry.py's centroid
    data only covers rooms and corridors (GRAPH_TO_IFC's 88 entries).
    Stairs, exits, and lobbies have no real IFC-mapped position.
    Previously those waypoints were skipped entirely, so a multi-floor
    path drew one straight 3D line directly from the top-floor
    corridor to the ground-floor corridor — cutting diagonally through
    several metres of height and every wall in between, which looked
    like the cone floating out of the building. Now, for any stair
    node lacking a real position, a waypoint is synthesised using the
    last known real X/Y (typically the corridor just left) at that
    node's OWN floor's real elevation (from the IFC floors data —
    F0=0.0m, F1=3.0m, F2=6.0m, F3=9.0m). This produces a genuine
    vertical descent through roughly one X/Y footprint before
    continuing horizontally once back on solid ground, rather than a
    single diagonal glide through the building. Not room-accurate —
    the true stairwell might be a few metres from that reused X/Y —
    but geometrically honest: the cone moves down, then across, never
    through a wall or the open air outside it.

    Returns (paths, refuge_info):
      paths       : {marker_id_str: [[x,y,z], [x,y,z], ...]}
      refuge_info : [{"mid": marker_id_str, "floor": floor_name}, ...]
                    one entry per wheelchair cone that stayed on its
                    floor instead of evacuating — empty list if none.
    """
    import networkx as nx
    from sensors.building_graph import BUILDING_GRAPH as G, ASSEMBLY_ID

    # Real per-floor elevations (IFC floors data) — used only to give
    # a stair/lobby waypoint the correct height, not a fabricated one.
    FLOOR_ELEVATIONS = {
        "F0 Ground Floor": 0.0,
        "F1 First Floor" : 3.0,
        "F2 Second Floor": 6.0,
        "F3 Third Floor" : 9.0,
    }
    GROUND_FLOOR = "F0 Ground Floor"

    if occupant_rooms is None:
        occupant_rooms = _resolve_occupant_rooms_from_blender(centroids)

    wheelchair_mids = _resolve_wheelchair_cones_from_blender()
    floor_corridor  = _floor_corridor_nodes(G)
    node_by_label   = {d["label"]: n for n, d in G.nodes(data=True)}

    def _signage_weight(u, v, edge_data):
        base = edge_data.get("weight", 1)
        if G.nodes[v].get("sign_blocked", False):
            return base * 50   # strongly discouraged, not physically impossible
        return base

    def _waypoints_for(node_path):
        """Shared waypoint-building logic — real centroids where they
        exist, synthesised stair-height waypoints where they don't."""
        waypoints = []
        last_xy = None
        for n in node_path:
            wp_label = G.nodes[n]["label"]
            c = centroids.get(wp_label)
            if c:
                waypoints.append([c["x"], c["y"], c["z"] + 0.85])
                last_xy = (c["x"], c["y"])
            elif last_xy is not None:
                node_floor = G.nodes[n].get("floor", "")
                z = FLOOR_ELEVATIONS.get(node_floor)
                if z is not None:
                    waypoints.append([last_xy[0], last_xy[1], z + 0.85])
        return waypoints

    paths       = {}
    refuge_info = []

    for mid, label in occupant_rooms.items():
        node = node_by_label.get(label)
        if node is None:
            continue

        current_floor = G.nodes[node].get("floor", "")
        is_wheelchair  = mid in wheelchair_mids
        needs_refuge   = is_wheelchair and current_floor != GROUND_FLOOR

        if needs_refuge:
            refuge_node = floor_corridor.get(current_floor)
            target = refuge_node if refuge_node is not None else node
            try:
                node_path = nx.shortest_path(G, node, target,
                                             weight=_signage_weight)
            except nx.NetworkXNoPath:
                node_path = [node]
            waypoints = _waypoints_for(node_path)
            if not waypoints:
                continue
            paths[mid] = waypoints
            refuge_info.append({"mid": mid, "floor": current_floor})
            continue

        try:
            node_path = nx.shortest_path(G, node, ASSEMBLY_ID,
                                         weight=_signage_weight)
        except nx.NetworkXNoPath:
            continue

        waypoints = _waypoints_for(node_path)

        rng = random.Random(42 + int(mid))
        jx  = (rng.random() - 0.5) * 14.0
        jy  = (rng.random() - 0.5) * 14.0
        waypoints.append([ASSEMBLY_X + jx, ASSEMBLY_Y + jy, ASSEMBLY_Z + 0.85])
        paths[mid] = waypoints

    return paths, refuge_info


def animate_evacuation_via_paths(paths: dict) -> None:
    """
    Sends pre-computed exit paths to Blender and uses bpy.app.timers
    to advance each cone along its path waypoints in real time.

    Fixed-speed stepping (0.35m per step) — was proportional stepping
    (a fraction of remaining distance per step), which covers most of
    the journey in the first few steps then barely moves for the
    rest, making the movement look nearly frozen. See module docstring.

    paths: {marker_id_str: [[x,y,z], [x,y,z], ...]}
    """
    with open(EVAC_PATHS_FILE, "w", encoding="utf-8") as f:
        json.dump(paths, f)

    ORANGE = [0.95, 0.55, 0.10, 1.0]

    send_to_blender(f"""
import bpy, json, math

with open(r"{EVAC_PATHS_FILE}") as f:
    all_paths = json.load(f)

ORANGE   = {ORANGE}
wp_index = {{mid: 0 for mid in all_paths}}

for mid in all_paths:
    obj = bpy.data.objects.get(f'Occupant_{{int(mid):03d}}')
    if obj:
        obj.color = ORANGE
        if obj.data and obj.data.materials:
            bsdf = obj.data.materials[0].node_tree.nodes.get('Principled BSDF')
            if bsdf:
                bsdf.inputs['Base Color'].default_value = ORANGE
                bsdf.inputs['Emission Color'].default_value = ORANGE
                if 'Emission Strength' in bsdf.inputs:
                    bsdf.inputs['Emission Strength'].default_value = 1.5

SPEED    = 0.35   # metres per step — fixed, not proportional
INTERVAL = 0.06
THRESH   = 0.25

def _evac_step():
    all_done = True
    for mid, waypoints in all_paths.items():
        idx = wp_index.get(mid, 0)
        if idx >= len(waypoints):
            continue
        all_done = False
        obj = bpy.data.objects.get(f'Occupant_{{int(mid):03d}}')
        if not obj:
            wp_index[mid] = idx + 1
            continue
        tx, ty, tz = waypoints[idx]
        dx = tx - obj.location.x
        dy = ty - obj.location.y
        dz = tz - obj.location.z
        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
        if dist < THRESH:
            wp_index[mid] = idx + 1
            if wp_index[mid] >= len(waypoints):
                # This specific cone has just reached its own final
                # waypoint (assembly point, or refuge for a wheelchair
                # cone) — mark it so a later ESCALATE press skips it
                # entirely instead of recomputing a path that would
                # walk it back into the building. Cleared by _reset().
                obj["fs_evacuated"] = True
        else:
            factor = min(SPEED / dist, 1.0)
            obj.location.x += dx * factor
            obj.location.y += dy * factor
            obj.location.z += dz * factor

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()

    if all_done:
        print('All cones reached assembly point via signage-aware paths')

        # Building is now empty — no occupancy anywhere means no
        # genuine ADB violation anywhere, so every floor turns green.
        # Corridor signs are DELIBERATELY left untouched here — they
        # stay red with the evacuation message until _reset() is
        # explicitly pressed, independent of floor colour.
        #
        # Floor animation_data is cleared first, not just the colour
        # set directly: if a fix_and_bake.py scenario's baked keyframes
        # are still active on these floor meshes, its still-running
        # external drive loop will keep calling frame_set() and
        # re-apply the baked per-tick colour on the very next call,
        # immediately undoing a plain colour assignment — the same
        # conflict already fixed for the cones themselves, applied
        # here to floors at the one moment it actually matters.
        GREEN = (0.05, 0.70, 0.15, 1.0)
        FLOOR_COLLECTIONS = {{
            "F0 Ground Floor": "IfcBuildingStorey/F0 Ground Floor",
            "F1 First Floor" : "IfcBuildingStorey/F1 First Floor",
            "F2 Second Floor": "IfcBuildingStorey/F2 Second Floor",
            "F3 Third Floor" : "IfcBuildingStorey/F3 Third Floor",
        }}
        SKIP = ('IfcFurnishing', 'IfcBuildingElementProxy', 'IfcFlowTerminal',
                'IfcSanitaryTerminal', 'IfcLightFixture', 'IfcAlarm',
                'IfcSign', 'IfcDoor', 'IfcWindow')

        coloured = 0
        for floor_name, col_name in FLOOR_COLLECTIONS.items():
            col = bpy.data.collections.get(col_name)
            if not col:
                continue
            for obj in col.objects:
                if obj.type != 'MESH':
                    continue
                if any(obj.name.startswith(p) for p in SKIP):
                    continue
                if obj.animation_data:
                    obj.animation_data_clear()
                obj.color = GREEN
                coloured += 1

        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D': area.tag_redraw()
        print(f'Building empty — {{coloured}} floor objects set GREEN '
              f'(signs unchanged, stay red until RESET)')
        return None
    return INTERVAL

if bpy.app.timers.is_registered(_evac_step):
    bpy.app.timers.unregister(_evac_step)
bpy.app.timers.register(_evac_step, first_interval=0.2)
print(f'Signage-aware evacuation started for {{len(all_paths)}} cones')
""")


# ── Agent sign response (partial redirect) ────────────────────────────────────

def redirect_cones_via_sign(affected_floor: str,
                             violation_room: str,
                             centroids: dict,
                             snapshot: dict) -> None:
    """
    When the agent blocks a corridor sign, move cones in the violation room
    toward the nearest exit — simulating occupants reacting to the sign change.

    This is NOT a full building evacuation:
    - Only cones in the violation room move
    - They walk to the nearest exit node, then stop (do not go to assembly)
    - Reflects occupancy management: room redirected, not evacuated

    Called from live_agent_runner.py after act_update_sign fires.
    Unchanged by this update — already uses real get_exit_path() data,
    a separate concern from the manager's full-building ESCALATE.
    """
    from sensors.building_graph import BUILDING_GRAPH as G, get_exit_path

    v_node = next((n for n, d in G.nodes(data=True)
                   if d["label"] == violation_room), None)
    if v_node is None:
        return

    path_result = get_exit_path(v_node)
    path_labels = path_result.get("path_labels", [])

    waypoints = []
    for pl in path_labels:
        c = centroids.get(pl)
        if c:
            waypoints.append([c["x"], c["y"], c["z"] + 0.85])

    if not waypoints:
        return

    occupancy = snapshot.get("occupancy", {})
    count_in_room = occupancy.get(violation_room, 0)
    if count_in_room == 0:
        return

    rng = random.Random(1000)
    marker_id = 0
    room_marker_ids = []
    for label, count in occupancy.items():
        if label == violation_room:
            room_marker_ids = list(range(marker_id, marker_id + count))
            break
        marker_id += count

    if not room_marker_ids:
        return

    paths = {str(mid): waypoints for mid in room_marker_ids}
    pj    = json.dumps(paths)
    ORANGE = [0.95, 0.55, 0.10, 1.0]

    send_to_blender(f"""
import bpy, json

paths    = json.loads('{pj}')
ORANGE   = {ORANGE}
wp_idx   = {{mid: 0 for mid in paths}}
STEP     = 0.10
INTERVAL = 0.15
THRESH   = 0.3

for mid in paths:
    obj = bpy.data.objects.get(f'Occupant_{{int(mid):03d}}')
    if obj:
        obj.color = ORANGE
        if obj.data and obj.data.materials:
            bsdf = obj.data.materials[0].node_tree.nodes.get('Principled BSDF')
            if bsdf:
                bsdf.inputs['Base Color'].default_value    = ORANGE
                bsdf.inputs['Emission Color'].default_value = ORANGE

def _redirect_step():
    all_done = True
    for mid, wps in paths.items():
        idx = wp_idx.get(mid, 0)
        if idx >= len(wps): continue
        all_done = False
        obj = bpy.data.objects.get(f'Occupant_{{int(mid):03d}}')
        if not obj: wp_idx[mid] = idx+1; continue
        tx,ty,tz = wps[idx]
        dx,dy,dz = tx-obj.location.x, ty-obj.location.y, tz-obj.location.z
        dist = (dx*dx+dy*dy+dz*dz)**0.5
        if dist < THRESH: wp_idx[mid] = idx+1
        else:
            obj.location.x += dx*STEP
            obj.location.y += dy*STEP
            obj.location.z += dz*STEP
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()
    if all_done:
        print('Cones redirected to exit (sign response)')
        return None
    return INTERVAL

bpy.app.timers.register(_redirect_step, first_interval=0.1)
print(f'Sign response: {{len(paths)}} cones redirecting to nearest exit')
""")


# ── Legacy fire-and-forget (kept for compatibility) ───────────────────────────

def move_cones_to_assembly(marker_ids: list) -> None:
    """
    Legacy instant move — kept for backward compatibility only.
    Teleports, does not use any path or signage state. Use
    animate_evacuation_via_paths() with
    compute_signage_aware_evacuation_paths() instead for anything new.
    """
    if not marker_ids:
        return

    rng       = random.Random(sum(marker_ids))
    positions = []
    for mid in marker_ids:
        jx = (rng.random() - 0.5) * 6.0
        jy = (rng.random() - 0.5) * 6.0
        positions.append((mid,
                          ASSEMBLY_X + jx,
                          ASSEMBLY_Y + jy,
                          ASSEMBLY_Z + 0.85))

    pj     = json.dumps(positions)
    ORANGE = [0.95, 0.55, 0.10, 1.0]

    send_to_blender(f"""
import bpy, json
positions = json.loads('{pj}')
ORANGE    = {ORANGE}
moved     = 0
for mid, x, y, z in positions:
    name = f'Occupant_{{mid:03d}}'
    obj  = bpy.data.objects.get(name)
    if obj:
        if obj.animation_data: obj.animation_data_clear()
        obj.location = (x, y, z)
        obj.color    = ORANGE
        if obj.data and obj.data.materials:
            bsdf = obj.data.materials[0].node_tree.nodes.get('Principled BSDF')
            if bsdf:
                bsdf.inputs['Base Color'].default_value    = ORANGE
                bsdf.inputs['Emission Color'].default_value = ORANGE
        moved += 1
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print(f'Moved {{moved}} cones to assembly point')
""")


def test_assembly_point() -> None:
    print("Moving 5 test cones to assembly point...")
    move_cones_to_assembly([0, 1, 2, 3, 4])
    print("Done — check Blender viewport")