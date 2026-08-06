# bim/assembly_point.py
# Creates and manages the assembly point marker outside the building.
# Provides two movement modes:
#
#   animate_evacuation_via_paths()
#     — full pathfinding evacuation triggered by manager ESCALATE button
#     — each cone follows room → corridor → stair → exit → assembly point
#     — uses bpy.app.timers for smooth real-time movement (no keyframes)
#
#   redirect_cones_via_sign()
#     — partial redirect triggered when agent updates a sign
#     — only cones on the affected floor move toward nearest exit
#     — simulates occupants reacting to corridor signage change
#
#   move_cones_to_assembly()
#     — fire-and-forget instant move (legacy, kept for compatibility)

import os, json, random
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE

ASSEMBLY_X = 16.0
ASSEMBLY_Y = -35.0
ASSEMBLY_Z = 0.0

REDIRECT_COUNT = 5


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


# ── Compute evacuation paths from graph ───────────────────────────────────────

def compute_evacuation_paths(snapshot: dict, centroids: dict) -> dict:
    """
    Computes the exit path for every occupant using the building graph.
    Returns {marker_id: [(x,y,z), ...]} — ordered waypoints from current
    room through corridor → stairwell → exit → assembly point.

    Called in Python before sending to Blender so the graph traversal
    stays in Python where the NetworkX graph is available.
    """
    from sensors.building_graph import BUILDING_GRAPH as G, get_exit_path

    occupancy  = snapshot.get("occupancy", {})
    node_by_label = {d["label"]: n for n, d in G.nodes(data=True)}

    rng_jitter = random.Random(42)
    paths      = {}
    marker_id  = 0

    for label, count in occupancy.items():
        node = node_by_label.get(label)
        if node is None:
            marker_id += count
            continue

        # Get graph path from this room to nearest exit
        path_result  = get_exit_path(node)
        path_labels  = path_result.get("path_labels", [label])

        # Build 3D waypoints from path labels via centroids
        waypoints = []
        for pl in path_labels:
            c = centroids.get(pl)
            if c:
                waypoints.append([
                    c["x"] + (rng_jitter.random() - 0.5) * 0.5,
                    c["y"] + (rng_jitter.random() - 0.5) * 0.5,
                    c["z"] + 0.85
                ])

        # Final destination — assembly point with spread
        jx = (rng_jitter.random() - 0.5) * 14.0
        jy = (rng_jitter.random() - 0.5) * 14.0
        waypoints.append([ASSEMBLY_X + jx, ASSEMBLY_Y + jy, ASSEMBLY_Z + 0.85])

        # Assign to each marker in this room
        for _ in range(count):
            paths[str(marker_id)] = waypoints
            marker_id += 1

    return paths


# ── Full pathfinding evacuation (manager escalate) ────────────────────────────

def animate_evacuation_via_paths(paths: dict) -> None:
    """
    Sends pre-computed exit paths to Blender and uses bpy.app.timers
    to advance each cone along its path waypoints in real time.

    paths: {marker_id_str: [[x,y,z], [x,y,z], ...]}
    Each cone follows: room → corridor → stair → exit → assembly point
    Speed: ~0.5m per step, 0.15s per step interval
    """
    pj     = json.dumps(paths)
    ORANGE = [0.95, 0.55, 0.10, 1.0]

    send_to_blender(f"""
import bpy, json

# Pre-computed paths per cone
all_paths = json.loads('{pj}')
ORANGE    = {ORANGE}

# Current waypoint index per cone
wp_index  = {{mid: 0 for mid in all_paths}}

# Turn all cones orange immediately
for mid in all_paths:
    name = f'Occupant_{{int(mid):03d}}'
    obj  = bpy.data.objects.get(name)
    if obj:
        obj.color = ORANGE
        if obj.data and obj.data.materials:
            mat  = obj.data.materials[0]
            bsdf = mat.node_tree.nodes.get('Principled BSDF')
            if bsdf:
                bsdf.inputs['Base Color'].default_value    = ORANGE
                bsdf.inputs['Emission Color'].default_value = ORANGE
                if 'Emission Strength' in bsdf.inputs:
                    bsdf.inputs['Emission Strength'].default_value = 1.5

# Cancel any existing timer
if hasattr(bpy.app.timers, '_fs_evac') and bpy.app.timers.is_registered(bpy.app.timers._fs_evac):
    bpy.app.timers.unregister(bpy.app.timers._fs_evac)

STEP_SIZE = 0.12   # fraction of remaining distance per step
INTERVAL  = 0.12   # seconds between steps
THRESHOLD = 0.3    # metres — close enough to advance to next waypoint

def _evac_step():
    all_done = True
    for mid, waypoints in all_paths.items():
        idx  = wp_index.get(mid, 0)
        if idx >= len(waypoints):
            continue
        all_done = False
        name = f'Occupant_{{int(mid):03d}}'
        obj  = bpy.data.objects.get(name)
        if not obj:
            wp_index[mid] = idx + 1
            continue
        tx, ty, tz = waypoints[idx]
        dx = tx - obj.location.x
        dy = ty - obj.location.y
        dz = tz - obj.location.z
        dist = (dx**2 + dy**2 + dz**2) ** 0.5
        if dist < THRESHOLD:
            wp_index[mid] = idx + 1    # advance to next waypoint
        else:
            obj.location.x += dx * STEP_SIZE
            obj.location.y += dy * STEP_SIZE
            obj.location.z += dz * STEP_SIZE

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()

    if all_done:
        print('All cones reached assembly point via exit paths')
        return None        # stop timer
    return INTERVAL        # continue

bpy.app.timers.register(_evac_step, first_interval=0.2)
print(f'Pathfinding evacuation started for {{len(all_paths)}} cones')
print('Cones following: room -> corridor -> stair -> exit -> assembly point')
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
    """
    from sensors.building_graph import BUILDING_GRAPH as G, get_exit_path

    # Find the violation room node and its exit path
    v_node = next((n for n, d in G.nodes(data=True)
                   if d["label"] == violation_room), None)
    if v_node is None:
        return

    path_result = get_exit_path(v_node)
    path_labels = path_result.get("path_labels", [])

    # Build waypoints for exit path (stop at exit, not assembly point)
    waypoints = []
    for pl in path_labels:
        c = centroids.get(pl)
        if c:
            waypoints.append([c["x"], c["y"], c["z"] + 0.85])

    if not waypoints:
        return

    # Find which marker IDs are in the violation room
    occupancy = snapshot.get("occupancy", {})
    count_in_room = occupancy.get(violation_room, 0)
    if count_in_room == 0:
        return

    # Find marker IDs sequentially (same seed as live_reposition_markers)
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

    # Build paths for just these cones
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

# Turn affected cones orange
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
    Legacy instant move — kept for backward compatibility.
    For new code use animate_evacuation_via_paths() instead.
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