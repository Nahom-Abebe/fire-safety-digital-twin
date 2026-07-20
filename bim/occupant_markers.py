# bim/occupant_markers.py
# Creates and repositions occupant cone markers in Blender.
# Uses bmesh (not bpy.ops) to avoid operator context failures.
#
# Two reposition functions:
#   reposition_markers()      — full round-trip with result file (phase 2 runner)
#   live_reposition_markers() — fire-and-forget, no result file (live agent runner)
#                               safe to call every tick without competing with
#                               floor colour or Pset update calls

import json, os, random
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE
from bim.room_geometry import load_room_centroids

COLOUR_NORMAL  = (0.15, 0.45, 0.90, 1.0)  # blue   — compliant
COLOUR_WARNING = (0.95, 0.55, 0.10, 1.0)  # orange — near capacity
COLOUR_OVER    = (0.90, 0.10, 0.10, 1.0)  # red    — over capacity

_HELPERS = r"""
import bpy, bmesh

def _col():
    sc = bpy.context.scene.collection
    c = bpy.data.collections.get('OccupantMarkers')
    if c is None:
        c = bpy.data.collections.new('OccupantMarkers')
    if c.name not in sc.children.keys():
        sc.children.link(c)
    c.hide_viewport = False; c.hide_render = False
    def _u(lc):
        if lc.collection.name == 'OccupantMarkers':
            lc.hide_viewport = False; lc.exclude = False
        for ch in lc.children: _u(ch)
    _u(bpy.context.view_layer.layer_collection)
    return c

def _cone(name, r1, r2, depth):
    mesh = bpy.data.meshes.new(name + '_m')
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=8,
                           radius1=r1, radius2=r2, depth=depth)
    bm.to_mesh(mesh); bm.free()

    # Create unique emissive material for this cone
    mat = bpy.data.materials.new(name=name + '_mat')
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get('Principled BSDF')
    if bsdf:
        # Default blue — will be overridden by keyframe or obj.color
        bsdf.inputs['Base Color'].default_value    = (0.15, 0.45, 0.90, 1.0)
        bsdf.inputs['Emission Color'].default_value = (0.15, 0.45, 0.90, 1.0)
        bsdf.inputs['Emission Strength'].default_value = 1.5
    mesh.materials.append(mat)
    return bpy.data.objects.new(name, mesh)
"""


def _jitter(centroid, seed):
    rng = random.Random(seed)
    return (centroid["x"] + (rng.random() - 0.5) * centroid["dx"] * 0.28,
            centroid["y"] + (rng.random() - 0.5) * centroid["dy"] * 0.28,
            centroid["z"])


# ── Initial placement ─────────────────────────────────────────────────────────

def create_markers(snapshot: dict) -> dict:
    """
    Creates one cone marker per occupant at their starting positions.
    Deletes any existing markers first. Uses result file round-trip.
    Call once at initialisation — not every tick.
    """
    centroids = load_room_centroids()
    occupancy = snapshot.get("occupancy", {})
    placements, mid, seed = [], 0, 0

    for label, count in occupancy.items():
        c = centroids.get(label)
        if not c:
            continue
        for _ in range(count):
            x, y, z = _jitter(c, seed)
            seed += 1
            placements.append((mid, x, y, z, *COLOUR_NORMAL))
            mid += 1

    pj = json.dumps(placements)
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = _HELPERS + f"""
import json, traceback
try:
    pl = json.loads('{pj}')
    col = _col()
    for o in list(bpy.data.objects):
        if o.name.startswith('Occupant_'):
            bpy.data.objects.remove(o, do_unlink=True)
    created = 0
    for mid, x, y, z, r, g, b, a in pl:
        obj = _cone(f'Occupant_{{mid:03d}}', 0.25, 0.05, 1.7)
        col.objects.link(obj)
        obj.location = (x, y, z + 0.85)
        
        # ─── Define Highly Visible Safety Orange (RGBA) ───────────────────
        safety_orange = (1.0, 0.38, 0.0, 1.0)
        
        obj.color = safety_orange  # Works in SOLID+OBJECT viewport mode
        obj.hide_viewport = False
        obj.hide_render   = False
        
        # Set material color properties for Material Preview / Rendered mode
        if obj.data.materials:
            mat  = obj.data.materials[0]
            bsdf = mat.node_tree.nodes.get('Principled BSDF')
            if bsdf:
                bsdf.inputs['Base Color'].default_value     = safety_orange
                bsdf.inputs['Emission Color'].default_value  = safety_orange
                bsdf.inputs['Emission Strength'].default_value = 5.0  # Extra bright pop
        # ──────────────────────────────────────────────────────────────────
                
        created += 1
    report = {{'status': 'ok', 'created': created}}
except Exception as e:
    report = {{'status': 'error', 'message': str(e), 'trace': traceback.format_exc()}}
with open(r"{RESULT_FILE}", "w", encoding="utf-8") as f:
    json.dump(report, f)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    send_to_blender(code)
    r = _read_result(timeout=25.0)
    r["requested"] = len(placements)
    return r

# ── Full reposition (phase2_runner) ───────────────────────────────────────────

def reposition_markers(snapshot: dict) -> dict:
    """
    Full reposition with result file round-trip.
    Safe to use in phase2_runner.py where no other concurrent
    Blender calls compete for RESULT_FILE.
    DO NOT use in live_agent_runner.py — use live_reposition_markers instead.
    """
    from sensors.building_graph import BUILDING_GRAPH
    centroids       = load_room_centroids()
    occupancy       = snapshot.get("occupancy", {})
    placements, mid, seed = [], 0, 1000

    for label, count in occupancy.items():
        c = centroids.get(label)
        if not c:
            continue
        for _ in range(count):
            x, y, z = _jitter(c, seed)
            seed += 1
            placements.append((mid, x, y, z, *COLOUR_NORMAL))
            mid += 1

    pj = json.dumps(placements)
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = _HELPERS + f"""
import json, traceback
try:
    pl   = json.loads('{pj}')
    col  = _col()
    existing = {{o.name for o in bpy.data.objects if o.name.startswith('Occupant_')}}
    wanted   = {{f'Occupant_{{p[0]:03d}}' for p in pl}}
    updated  = 0
    for mid, x, y, z, r, g, b, a in pl:
        name = f'Occupant_{{mid:03d}}'
        obj  = bpy.data.objects.get(name)
        if obj is None:
            obj = _cone(name, 0.25, 0.05, 1.7)
            col.objects.link(obj)
        obj.location = (x, y, z + 0.85)
        obj.color    = (r, g, b, a)
        updated += 1
    for name in existing - wanted:
        o = bpy.data.objects.get(name)
        if o: bpy.data.objects.remove(o, do_unlink=True)
    report = {{'status': 'ok', 'updated': updated}}
except Exception as e:
    report = {{'status': 'error', 'message': str(e), 'trace': traceback.format_exc()}}
with open(r"{RESULT_FILE}", "w", encoding="utf-8") as f:
    json.dump(report, f)
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    send_to_blender(code)
    r = _read_result(timeout=20.0)
    r["requested"] = len(placements)
    return r


# ── Fire-and-forget reposition (live_agent_runner) ────────────────────────────

def live_reposition_markers(snapshot: dict) -> None:
    """
    Fast fire-and-forget marker repositioning for live_agent_runner.py.

    Key differences from reposition_markers():
      - Does NOT write or read RESULT_FILE — no competition with
        _update_floor_colours() or bulk_update_occupancy_psets()
        which run in the same tick
      - Only moves existing markers (no delete/recreate) — markers
        placed by create_markers() at startup stay alive throughout
      - Returns immediately without waiting for Blender confirmation
      - Safe to call every 2 seconds in the live loop

    If a marker is missing (shouldn't happen after create_markers),
    it is skipped silently rather than triggering a recreation that
    could interfere with the result file channel.
    """
    centroids = load_room_centroids()
    occupancy = snapshot.get("occupancy", {})

    placements = []
    mid        = 0
    seed       = 1000   # different stream from create_markers seed=0

    for label, count in occupancy.items():
        c = centroids.get(label)
        if not c:
            continue
        for _ in range(count):
            x, y, z = _jitter(c, seed)
            seed += 1
            placements.append((mid, x, y, z, *COLOUR_NORMAL))
            mid += 1

    if not placements:
        return

    # Cap at 80 — one per occupant, no more
    pj = json.dumps(placements[:80])

    # Fire-and-forget: no result file written, no _read_result call
    send_to_blender(_HELPERS + f"""
import json
try:
    pl  = json.loads('{pj}')
    col = _col()
    for mid, x, y, z, r, g, b, a in pl:
        name = f'Occupant_{{mid:03d}}'
        obj  = bpy.data.objects.get(name)
        if obj:
            obj.location = (x, y, z + 0.85)
            obj.color    = (r, g, b, a)
        # If marker missing, skip — do not recreate during live loop
except Exception as e:
    print(f'live_reposition_markers error: {{e}}')
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
""")
    # Returns None immediately — no waiting