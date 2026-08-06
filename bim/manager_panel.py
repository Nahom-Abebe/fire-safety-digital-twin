# bim/manager_panel.py
# Building manager overlay — ESCALATE triggers smooth pathfinding evacuation.
#
# Movement fix: fixed-speed stepping (not proportional) so cones visibly
# walk at constant pace. Paths simplified to 3 waypoints per cone:
#   floor exit node → ground floor exit → assembly point
# This avoids the NetworkX BFS freeze while still showing exit-path routing.

import os, sys, json, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bim.ifc_bridge import send_to_blender


def _build_simple_paths(snapshot: dict, centroids: dict) -> dict:
    """
    Builds simplified 3-waypoint paths for each cone:
      1. Nearest corridor on their floor (via centroid lookup)
      2. Ground floor main exit (EXIT-1 centroid)
      3. Assembly point (outside building)

    Uses simple floor-based routing rather than full BFS per cone —
    avoids the thread-blocking NetworkX traversal that caused the freeze.
    Returns {marker_id_str: [[x,y,z], [x,y,z], [x,y,z]]}
    """
    from sensors.building_graph import BUILDING_GRAPH as G

    occupancy = snapshot.get("occupancy", {})

    # Find exit centroid (ground floor main exit)
    exit_cent = None
    for label, c in centroids.items():
        if "EXIT" in label.upper():
            exit_cent = c
            break
    if exit_cent is None:
        exit_cent = {"x": 16.0, "y": -20.0, "z": 0.0}

    # Assembly point
    ASSEMBLY_X, ASSEMBLY_Y, ASSEMBLY_Z = 16.0, -35.0, 0.85

    rng       = random.Random(42)
    paths     = {}
    marker_id = 0

    for label, count in occupancy.items():
        # Find corridor centroid on same floor
        node = next((n for n, d in G.nodes(data=True)
                     if d["label"] == label), None)
        if node is None:
            marker_id += count
            continue

        floor = G.nodes[node].get("floor", "")

        # Find nearest corridor centroid on same floor
        corridor_cent = None
        for cl, c in centroids.items():
            n2 = next((n for n, d in G.nodes(data=True)
                       if d["label"] == cl), None)
            if n2 and G.nodes[n2].get("node_type") == "corridor" \
               and G.nodes[n2].get("floor") == floor:
                corridor_cent = c
                break

        if corridor_cent is None:
            corridor_cent = exit_cent

        for _ in range(count):
            # Jitter final position at assembly point
            jx = (rng.random() - 0.5) * 14.0
            jy = (rng.random() - 0.5) * 14.0

            paths[str(marker_id)] = [
                [corridor_cent["x"], corridor_cent["y"],
                 corridor_cent["z"] + 0.85],           # step 1: corridor
                [exit_cent["x"], exit_cent["y"],
                 exit_cent["z"] + 0.85],               # step 2: exit
                [ASSEMBLY_X + jx, ASSEMBLY_Y + jy,
                 ASSEMBLY_Z],                           # step 3: assembly
            ]
            marker_id += 1

    return paths


def install_manager_panel():
    project_root = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    ).replace("\\", "\\\\")

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
        "# ── Button layout ─────────────────────────────────────────────",
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
        "    _text('ESCALATE TO ASSEMBLY POINT', b['e']['x']+14, b['e']['y']+20, sz=12)",
        "    _text('Shift+E', b['e']['x']+14, b['e']['y']+6, sz=9, col=(1,1,1,0.5))",
        "    _text('RESET ALL SIGNS TO CLEAR',   b['r']['x']+14, b['r']['y']+20, sz=12)",
        "    _text('Shift+R', b['r']['x']+14, b['r']['y']+6, sz=9, col=(1,1,1,0.5))",
        "",
        "# ── ESCALATE ──────────────────────────────────────────────────",
        "def _escalate():",
        "    import sys; sys.path.insert(0, ROOT)",
        "",
        "    # Step 1 — lower attractiveness (Python, instant)",
        "    try:",
        "        from sensors.sensor_sim import set_room_attractiveness",
        "        from sensors.building_graph import BUILDING_GRAPH as G",
        "        for _, d in G.nodes(data=True):",
        "            if d.get('node_type') == 'room':",
        "                try: set_room_attractiveness(d['label'], 0.0)",
        "                except Exception: pass",
        "    except Exception: pass",
        "",
        "    # Step 2 — build simple 3-waypoint paths in Python (no BFS freeze)",
        "    paths_json = '{}'",
        "    try:",
        "        from sensors.sensor_sim import get_sensor_snapshot",
        "        from bim.room_geometry import load_room_centroids",
        "        from bim.manager_panel import _build_simple_paths",
        "        snap      = get_sensor_snapshot()",
        "        centroids = load_room_centroids()",
        "        paths     = _build_simple_paths(snap, centroids)",
        "        import json",
        "        paths_json = json.dumps(paths)",
        "        print(f'Built paths for {len(paths)} cones')",
        "    except Exception as e:",
        "        print(f'Path build error: {e}')",
        "",
        "    # Step 3 — one Blender call: signs red + start smooth timer walk",
        "    from bim.ifc_bridge import send_to_blender as _s",
        "    _s(f'''",
        "import bpy, json, math",
        "",
        "# Update sign panels RED",
        "RED = (0.90, 0.05, 0.05, 1.0)",
        "for obj in bpy.data.objects:",
        "    if obj.name.startswith(\"SignPanel_\") and obj.data and obj.data.materials:",
        "        nd = obj.data.materials[0].node_tree.nodes.get(\"Principled BSDF\")",
        "        if nd:",
        "            nd.inputs[\"Base Color\"].default_value = RED",
        "            if \"Emission Color\" in nd.inputs: nd.inputs[\"Emission Color\"].default_value = RED",
        "            if \"Emission Strength\" in nd.inputs: nd.inputs[\"Emission Strength\"].default_value = 3.0",
        "    if obj.name.startswith(\"SignText_\"):",
        "        obj.data.body = \"ESCALATED\\\\nProceed to\\\\nAssembly Point\"",
        "",
        "# Load paths",
        "all_paths = json.loads(r\"\"\"{paths_json}\"\"\")",
        "ORANGE    = (0.95, 0.55, 0.10, 1.0)",
        "wp_idx    = {{mid: 0 for mid in all_paths}}",
        "",
        "# Turn all cones orange immediately",
        "for mid in all_paths:",
        "    obj = bpy.data.objects.get(f\"Occupant_{{int(mid):03d}}\")",
        "    if obj:",
        "        obj.color = ORANGE",
        "        if obj.data and obj.data.materials:",
        "            bsdf = obj.data.materials[0].node_tree.nodes.get(\"Principled BSDF\")",
        "            if bsdf:",
        "                bsdf.inputs[\"Base Color\"].default_value    = ORANGE",
        "                bsdf.inputs[\"Emission Color\"].default_value = ORANGE",
        "",
        "# Fixed-speed movement: 0.35 metres per step at 0.06s interval",
        "# Cones visibly walk — not proportional fade",
        "SPEED    = 0.35",
        "INTERVAL = 0.06",
        "THRESH   = 0.25",
        "",
        "def _walk():",
        "    all_done = True",
        "    for mid, wps in all_paths.items():",
        "        idx = wp_idx.get(mid, 0)",
        "        if idx >= len(wps): continue",
        "        all_done = False",
        "        obj = bpy.data.objects.get(f\"Occupant_{{int(mid):03d}}\")",
        "        if not obj:",
        "            wp_idx[mid] = idx + 1",
        "            continue",
        "        tx, ty, tz = wps[idx]",
        "        dx = tx - obj.location.x",
        "        dy = ty - obj.location.y",
        "        dz = tz - obj.location.z",
        "        dist = math.sqrt(dx*dx + dy*dy + dz*dz)",
        "        if dist < THRESH:",
        "            wp_idx[mid] = idx + 1",
        "        else:",
        "            factor = min(SPEED / dist, 1.0)",
        "            obj.location.x += dx * factor",
        "            obj.location.y += dy * factor",
        "            obj.location.z += dz * factor",
        "    for area in bpy.context.screen.areas:",
        "        if area.type == \"VIEW_3D\": area.tag_redraw()",
        "    if all_done:",
        "        print(\"All cones reached assembly point\")",
        "        return None",
        "    return INTERVAL",
        "",
        "if bpy.app.timers.is_registered(_walk):",
        "    bpy.app.timers.unregister(_walk)",
        "bpy.app.timers.register(_walk, first_interval=0.1)",
        "for area in bpy.context.screen.areas:",
        "    if area.type == \"VIEW_3D\": area.tag_redraw()",
        "print(f\"Walking {{len(all_paths)}} cones at fixed speed to assembly point\")",
        "''')",
        "    print('Escalation triggered — watch cones walk to assembly point')",
        "",
        "# ── RESET ─────────────────────────────────────────────────────",
        "def _reset():",
        "    from bim.ifc_bridge import send_to_blender as _s",
        "    _s(r'''",
        "import bpy",
        "GREEN = (0.05, 0.70, 0.15, 1.0)",
        "for obj in bpy.data.objects:",
        "    if obj.name.startswith('SignPanel_') and obj.data and obj.data.materials:",
        "        nd = obj.data.materials[0].node_tree.nodes.get('Principled BSDF')",
        "        if nd:",
        "            nd.inputs['Base Color'].default_value = GREEN",
        "            if 'Emission Color' in nd.inputs: nd.inputs['Emission Color'].default_value = GREEN",
        "            if 'Emission Strength' in nd.inputs: nd.inputs['Emission Strength'].default_value = 1.5",
        "    if obj.name.startswith('SignText_'):",
        "        obj.data.body = 'Status: CLEAR\\nAll routes open'",
        "for area in bpy.context.screen.areas:",
        "    if area.type == 'VIEW_3D': area.tag_redraw()",
        "print('RESET: all signs GREEN')",
        "''')",
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
        "        l.label(text='Shift+E escalate  |  Shift+R reset')",
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
        "print('Fire Safety UI ready — fixed-speed cone movement on ESCALATE')",
    ])

    return send_to_blender(blender_code)


if __name__ == "__main__":
    print("Installing Fire Safety Manager Panel...")
    result = install_manager_panel()
    print(f"Result: {result}")