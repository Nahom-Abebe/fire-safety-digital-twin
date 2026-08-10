# bim/manager_panel.py
# Building manager overlay — ESCALATE triggers smooth pathfinding evacuation.
#
# Bug fix: paths JSON was embedded in an f-string which broke on curly braces.
# Now paths are saved to a temp file and read by Blender — fully reliable.
#
# Movement: fixed-speed (0.35m/step) so cones visibly walk to assembly point.
# Paths: room → corridor → exit → assembly point (3 waypoints, no BFS freeze).

import os, sys, json, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bim.ifc_bridge import send_to_blender

# Temp file for passing paths to Blender
BIM_DIR    = os.path.dirname(os.path.abspath(__file__))
PATHS_FILE = os.path.join(BIM_DIR, "_evac_paths.json").replace("\\", "/")


def _build_simple_paths(snapshot: dict, centroids: dict) -> dict:
    """
    Builds 3-waypoint paths for every cone:
      1. Nearest corridor centroid on same floor
      2. Ground floor exit centroid
      3. Assembly point with jitter

    Simple lookup — no BFS, no freeze.
    Returns {marker_id_str: [[x,y,z], [x,y,z], [x,y,z]]}
    """
    from sensors.building_graph import BUILDING_GRAPH as G

    occupancy = snapshot.get("occupancy", {})

    # Find exit centroid
    exit_cent = None
    for label, c in centroids.items():
        if "EXIT" in label.upper() or "exit" in label.lower():
            exit_cent = c
            break
    if exit_cent is None:
        exit_cent = {"x": 16.0, "y": -18.0, "z": 0.0}

    ASSEMBLY_X, ASSEMBLY_Y, ASSEMBLY_Z = 16.0, -35.0, 0.85

    # Build floor → corridor centroid lookup
    floor_corridor = {}
    for cl, c in centroids.items():
        n = next((nd for nd, d in G.nodes(data=True) if d["label"] == cl), None)
        if n and G.nodes[n].get("node_type") == "corridor":
            fl = G.nodes[n].get("floor", "")
            if fl not in floor_corridor:
                floor_corridor[fl] = c

    rng       = random.Random(42)
    paths     = {}
    marker_id = 0

    for label, count in occupancy.items():
        node = next((n for n, d in G.nodes(data=True)
                     if d["label"] == label), None)
        if node is None:
            marker_id += count
            continue

        floor         = G.nodes[node].get("floor", "")
        corridor_cent = floor_corridor.get(floor, exit_cent)

        for _ in range(count):
            jx = (rng.random() - 0.5) * 14.0
            jy = (rng.random() - 0.5) * 14.0
            paths[str(marker_id)] = [
                [corridor_cent["x"], corridor_cent["y"], corridor_cent["z"] + 0.85],
                [exit_cent["x"],     exit_cent["y"],     exit_cent["z"] + 0.85],
                [ASSEMBLY_X + jx,    ASSEMBLY_Y + jy,    ASSEMBLY_Z],
            ]
            marker_id += 1

    return paths


def install_manager_panel():
    project_root = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    ).replace("\\", "/")

    blender_code = "\n".join([
        "import bpy, sys, blf, gpu",
        "from gpu_extras.batch import batch_for_shader",
        "",
        f"ROOT = r'{project_root}'",
        "if ROOT not in sys.path: sys.path.insert(0, ROOT)",
        "",
        "# ── Remove old handler ────────────────────────────────────────",
        "_NS = bpy.app.driver_namespace",
        "if _NS.get('fs_handle'):",
        "    try:",
        "        bpy.types.SpaceView3D.draw_handler_remove(_NS['fs_handle'], 'WINDOW')",
        "    except Exception: pass",
        "_NS['fs_handle'] = None",
        "",
        "BTN = {",
        "    'e': {'x': 60, 'y': 120, 'w': 280, 'h': 50},",
        "    'r': {'x': 60, 'y': 58,  'w': 280, 'h': 50},",
        "}",
        "COL = {",
        "    'e':  (0.72, 0.05, 0.05, 0.93),",
        "    'eh': (0.95, 0.15, 0.15, 0.98),",
        "    'r':  (0.05, 0.50, 0.12, 0.93),",
        "    'rh': (0.08, 0.70, 0.18, 0.98),",
        "    'sh': (0, 0, 0, 0.38),",
        "}",
        "_hov = {'e': False, 'r': False}",
        "",
        "def _inside(b, mx, my):",
        "    return b['x'] <= mx <= b['x']+b['w'] and b['y'] <= my <= b['y']+b['h']",
        "",
        "def _rect(x, y, w, h, col):",
        "    try: shader = gpu.shader.from_builtin('UNIFORM_COLOR')",
        "    except Exception: return",
        "    verts = [(x,y),(x+w,y),(x+w,y+h),(x,y),(x+w,y+h),(x,y+h)]",
        "    batch = batch_for_shader(shader, 'TRIS', {'pos': verts})",
        "    shader.bind()",
        "    shader.uniform_float('color', col)",
        "    gpu.state.blend_set('ALPHA')",
        "    batch.draw(shader)",
        "    gpu.state.blend_set('NONE')",
        "",
        "def _text(t, x, y, sz=12, col=(1,1,1,1)):",
        "    blf.size(0, sz)",
        "    blf.color(0, col[0], col[1], col[2], col[3])",
        "    blf.position(0, x, y, 0)",
        "    blf.draw(0, str(t))",
        "",
        "def _draw():",
        "    b  = BTN",
        "    ce = COL['eh'] if _hov['e'] else COL['e']",
        "    cr = COL['rh'] if _hov['r'] else COL['r']",
        "    _rect(b['e']['x']+4, b['e']['y']-4, b['e']['w'], b['e']['h'], COL['sh'])",
        "    _rect(b['r']['x']+4, b['r']['y']-4, b['r']['w'], b['r']['h'], COL['sh'])",
        "    _rect(b['e']['x'], b['e']['y'], b['e']['w'], b['e']['h'], ce)",
        "    _rect(b['r']['x'], b['r']['y'], b['r']['w'], b['r']['h'], cr)",
        "    _rect(b['e']['x'], b['e']['y'], 5, b['e']['h'], (1, 0.3, 0.3, 0.9))",
        "    _rect(b['r']['x'], b['r']['y'], 5, b['r']['h'], (0.3, 1, 0.4, 0.9))",
        "    _text('ESCALATE TO ASSEMBLY POINT', b['e']['x']+14, b['e']['y']+18, sz=12)",
        "    _text('RESET ALL SIGNS TO CLEAR',   b['r']['x']+14, b['r']['y']+18, sz=12)",
        "",
        "# ── ESCALATE ──────────────────────────────────────────────────",
        f"PATHS_FILE = r'{PATHS_FILE}'",
        "",
        "def _escalate():",
        "    import sys, json; sys.path.insert(0, ROOT)",
        "",
        "    # Step 1 — lower attractiveness",
        "    try:",
        "        from sensors.sensor_sim import set_room_attractiveness",
        "        from sensors.building_graph import BUILDING_GRAPH as G",
        "        for _, d in G.nodes(data=True):",
        "            if d.get('node_type') == 'room':",
        "                try: set_room_attractiveness(d['label'], 0.0)",
        "                except Exception: pass",
        "    except Exception as e:",
        "        print(f'Attractiveness error: {e}')",
        "",
        "    # Step 2 — build paths and save to file (avoids f-string curly brace bug)",
        "    try:",
        "        from sensors.sensor_sim import get_sensor_snapshot",
        "        from bim.room_geometry import load_room_centroids",
        "        from bim.manager_panel import _build_simple_paths",
        "        snap      = get_sensor_snapshot()",
        "        centroids = load_room_centroids()",
        "        paths     = _build_simple_paths(snap, centroids)",
        "        with open(PATHS_FILE, 'w') as fp:",
        "            json.dump(paths, fp)",
        "        print(f'Saved paths for {len(paths)} cones to {PATHS_FILE}')",
        "    except Exception as e:",
        "        print(f'Path build error: {e}')",
        "        paths = {}",
        "",
        "    # Step 3 — one Blender call: signs red + start timer (reads file)",
        "    from bim.ifc_bridge import send_to_blender as _s",
        "    _s(",
        "        'import bpy, json, math, os\\n'",
        "        'RED = (0.90, 0.05, 0.05, 1.0)\\n'",
        "        'for obj in bpy.data.objects:\\n'",
        "        '    if obj.name.startswith(\"SignPanel_\") and obj.data and obj.data.materials:\\n'",
        "        '        nd = obj.data.materials[0].node_tree.nodes.get(\"Principled BSDF\")\\n'",
        "        '        if nd:\\n'",
        "        '            nd.inputs[\"Base Color\"].default_value = RED\\n'",
        "        '            if \"Emission Color\" in nd.inputs: nd.inputs[\"Emission Color\"].default_value = RED\\n'",
        "        '            if \"Emission Strength\" in nd.inputs: nd.inputs[\"Emission Strength\"].default_value = 3.0\\n'",
        "        '    if obj.name.startswith(\"SignText_\"):\\n'",
        "        '        obj.data.body = \"ESCALATED\\\\nProceed to\\\\nAssembly Point\"\\n'",
        f"        'PATHS_FILE = r\"{PATHS_FILE}\"\\n'",
        "        'all_paths = {}\\n'",
        "        'try:\\n'",
        "        '    with open(PATHS_FILE) as fp: all_paths = json.load(fp)\\n'",
        "        '    print(f\"Loaded paths for {len(all_paths)} cones\")\\n'",
        "        'except Exception as e: print(f\"Path load error: {e}\")\\n'",
        "        'ORANGE = (0.95, 0.55, 0.10, 1.0)\\n'",
        "        'wp_idx = {mid: 0 for mid in all_paths}\\n'",
        "        'for mid in all_paths:\\n'",
        "        '    obj = bpy.data.objects.get(f\"Occupant_{int(mid):03d}\")\\n'",
        "        '    if obj:\\n'",
        "        '        obj.color = ORANGE\\n'",
        "        '        if obj.data and obj.data.materials:\\n'",
        "        '            bsdf = obj.data.materials[0].node_tree.nodes.get(\"Principled BSDF\")\\n'",
        "        '            if bsdf:\\n'",
        "        '                bsdf.inputs[\"Base Color\"].default_value = ORANGE\\n'",
        "        '                bsdf.inputs[\"Emission Color\"].default_value = ORANGE\\n'",
        "        'SPEED = 0.35\\n'",
        "        'INTERVAL = 0.06\\n'",
        "        'THRESH = 0.25\\n'",
        "        'def _walk():\\n'",
        "        '    all_done = True\\n'",
        "        '    for mid, wps in all_paths.items():\\n'",
        "        '        idx = wp_idx.get(mid, 0)\\n'",
        "        '        if idx >= len(wps): continue\\n'",
        "        '        all_done = False\\n'",
        "        '        obj = bpy.data.objects.get(f\"Occupant_{int(mid):03d}\")\\n'",
        "        '        if not obj: wp_idx[mid] = idx+1; continue\\n'",
        "        '        tx,ty,tz = wps[idx]\\n'",
        "        '        dx,dy,dz = tx-obj.location.x, ty-obj.location.y, tz-obj.location.z\\n'",
        "        '        dist = math.sqrt(dx*dx+dy*dy+dz*dz)\\n'",
        "        '        if dist < THRESH: wp_idx[mid] = idx+1\\n'",
        "        '        else:\\n'",
        "        '            f = min(SPEED/dist, 1.0)\\n'",
        "        '            obj.location.x += dx*f\\n'",
        "        '            obj.location.y += dy*f\\n'",
        "        '            obj.location.z += dz*f\\n'",
        "        '    for area in bpy.context.screen.areas:\\n'",
        "        '        if area.type==\"VIEW_3D\": area.tag_redraw()\\n'",
        "        '    if all_done: print(\"All cones reached assembly point\"); return None\\n'",
        "        '    return INTERVAL\\n'",
        "        'if bpy.app.timers.is_registered(_walk): bpy.app.timers.unregister(_walk)\\n'",
        "        'bpy.app.timers.register(_walk, first_interval=0.2)\\n'",
        "        'for area in bpy.context.screen.areas:\\n'",
        "        '    if area.type==\"VIEW_3D\": area.tag_redraw()\\n'",
        "        f'print(\"Walking cones to assembly point via {PATHS_FILE}\")\\n'",
        "    )",
        "    print('Escalation triggered')",
        "",
        "# ── RESET ─────────────────────────────────────────────────────",
        "def _reset():",
        "    from bim.ifc_bridge import send_to_blender as _s",
        "    _s(",
        "        'import bpy\\n'",
        "        'GREEN = (0.05, 0.70, 0.15, 1.0)\\n'",
        "        'for obj in bpy.data.objects:\\n'",
        "        '    if obj.name.startswith(\"SignPanel_\") and obj.data and obj.data.materials:\\n'",
        "        '        nd = obj.data.materials[0].node_tree.nodes.get(\"Principled BSDF\")\\n'",
        "        '        if nd:\\n'",
        "        '            nd.inputs[\"Base Color\"].default_value = GREEN\\n'",
        "        '            if \"Emission Color\" in nd.inputs: nd.inputs[\"Emission Color\"].default_value = GREEN\\n'",
        "        '            if \"Emission Strength\" in nd.inputs: nd.inputs[\"Emission Strength\"].default_value = 1.5\\n'",
        "        '    if obj.name.startswith(\"SignText_\"):\\n'",
        "        '        obj.data.body = \"Status: CLEAR\\\\nAll routes open\"\\n'",
        "        'for area in bpy.context.screen.areas:\\n'",
        "        '    if area.type==\"VIEW_3D\": area.tag_redraw()\\n'",
        "        'print(\"RESET: all signs GREEN\")\\n'",
        "    )",
        "    print('Reset complete')",
        "",
        "# ── Operators ─────────────────────────────────────────────────",
        "class FS_OT_click(bpy.types.Operator):",
        "    bl_idname = 'firesafety.click'",
        "    bl_label  = 'FS Click'",
        "    def invoke(self, ctx, ev):",
        "        mx, my = ev.mouse_region_x, ev.mouse_region_y",
        "        if _inside(BTN['e'], mx, my): _escalate(); return {'FINISHED'}",
        "        if _inside(BTN['r'], mx, my): _reset();    return {'FINISHED'}",
        "        return {'PASS_THROUGH'}",
        "",
        "class FS_OT_hover(bpy.types.Operator):",
        "    bl_idname = 'firesafety.hover'",
        "    bl_label  = 'FS Hover'",
        "    def invoke(self, ctx, ev):",
        "        mx, my = ev.mouse_region_x, ev.mouse_region_y",
        "        prev = (_hov['e'], _hov['r'])",
        "        _hov['e'] = _inside(BTN['e'], mx, my)",
        "        _hov['r'] = _inside(BTN['r'], mx, my)",
        "        if (_hov['e'], _hov['r']) != prev: ctx.area.tag_redraw()",
        "        return {'PASS_THROUGH'}",
        "",
        "class FS_OT_escalate(bpy.types.Operator):",
        "    bl_idname = 'firesafety.escalate'",
        "    bl_label  = 'Escalate'",
        "    def execute(self, ctx): _escalate(); return {'FINISHED'}",
        "",
        "class FS_OT_reset(bpy.types.Operator):",
        "    bl_idname = 'firesafety.reset_signs'",
        "    bl_label  = 'Reset Signs'",
        "    def execute(self, ctx): _reset(); return {'FINISHED'}",
        "",
        "class FS_PT_panel(bpy.types.Panel):",
        "    bl_label       = 'Building Manager'",
        "    bl_idname      = 'FS_PT_panel'",
        "    bl_space_type  = 'VIEW_3D'",
        "    bl_region_type = 'UI'",
        "    bl_category    = 'Fire Safety'",
        "    def draw(self, ctx):",
        "        l = self.layout",
        "        l.label(text='Click buttons bottom-left of viewport')",
        "        l.separator()",
        "        r = l.row(); r.scale_y = 2.0; r.alert = True",
        "        r.operator('firesafety.escalate', text='ESCALATE', icon='ERROR')",
        "        r2 = l.row(); r2.scale_y = 1.5",
        "        r2.operator('firesafety.reset_signs', text='RESET', icon='CHECKMARK')",
        "",
        "for cls in [FS_OT_click, FS_OT_hover, FS_OT_escalate,",
        "            FS_OT_reset, FS_PT_panel]:",
        "    try: bpy.utils.unregister_class(cls)",
        "    except Exception: pass",
        "    bpy.utils.register_class(cls)",
        "",
        "_NS['fs_handle'] = bpy.types.SpaceView3D.draw_handler_add(",
        "    _draw, (), 'WINDOW', 'POST_PIXEL')",
        "",
        "wm = bpy.context.window_manager",
        "kc = wm.keyconfigs.addon",
        "if kc:",
        "    km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')",
        "    for kmi in list(km.keymap_items):",
        "        if kmi.idname.startswith('firesafety.'): km.keymap_items.remove(kmi)",
        "    km.keymap_items.new('firesafety.click',       'LEFTMOUSE', 'PRESS')",
        "    km.keymap_items.new('firesafety.hover',       'MOUSEMOVE', 'ANY')",
        "    km.keymap_items.new('firesafety.escalate',    'E', 'PRESS', shift=True)",
        "    km.keymap_items.new('firesafety.reset_signs', 'R', 'PRESS', shift=True)",
        "",
        "for area in bpy.context.screen.areas:",
        "    if area.type == 'VIEW_3D': area.tag_redraw()",
        "print('Fire Safety UI ready')",
        "print('  ESCALATE: paths saved to file, read by Blender timer')",
        "print('  Fixed-speed movement — cones clearly visible walking')",
    ])

    return send_to_blender(blender_code)


if __name__ == "__main__":
    print("Installing Fire Safety Manager Panel...")
    result = install_manager_panel()
    print(f"Result: {result}")