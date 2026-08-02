# bim/assembly_point.py
# Creates and manages the assembly point marker outside the building.
# Occupants redirect here when a room is flagged as over capacity.

import os, json, random
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE

# Assembly point coordinates — outside the building front entrance
# These are the default values — adjust after running create_assembly_point()
# if it appears in the wrong location in your Blender scene
ASSEMBLY_X = 16.0
ASSEMBLY_Y = -35.0
ASSEMBLY_Z = 0.0

REDIRECT_COUNT = 5   # how many cones to redirect per violation


def create_assembly_point() -> dict:
    """
    Places a green glowing circle outside the building with a label.
    Call once from phase1_setup.py.
    Returns the status and location so you can verify placement.
    """
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import bpy, bmesh, json, traceback

try:
    # Remove any existing assembly point objects
    for obj in list(bpy.data.objects):
        if obj.name.startswith('AssemblyPoint'):
            bpy.data.objects.remove(obj, do_unlink=True)
    for mat in list(bpy.data.materials):
        if mat.name.startswith('AssemblyMat') or mat.name.startswith('AssemblyText'):
            bpy.data.materials.remove(mat)

    # ── Green circle on the ground ────────────────────────────────────
    mesh = bpy.data.meshes.new('AssemblyPoint_m')
    bm   = bmesh.new()
    bmesh.ops.create_circle(bm, cap_ends=True, cap_tris=False,
                             segments=32, radius=4.0)
    bm.to_mesh(mesh)
    bm.free()

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

    # ── Main label ────────────────────────────────────────────────────
    bpy.ops.object.text_add(
        location=({ASSEMBLY_X}, {ASSEMBLY_Y}, {ASSEMBLY_Z} + 1.5))
    txt = bpy.context.object
    txt.name = 'AssemblyPoint_Label'
    txt.data.body        = 'ASSEMBLY POINT'
    txt.data.size        = 1.2
    txt.data.align_x     = 'CENTER'
    txt.rotation_euler   = (1.5708, 0, 0)

    tmat = bpy.data.materials.new('AssemblyTextMat')
    tmat.use_nodes = True
    tn = tmat.node_tree.nodes.get('Principled BSDF')
    if tn:
        tn.inputs['Base Color'].default_value      = (1.0, 1.0, 1.0, 1.0)
        tn.inputs['Emission Color'].default_value   = (1.0, 1.0, 1.0, 1.0)
        tn.inputs['Emission Strength'].default_value = 3.0
    txt.data.materials.clear()
    txt.data.materials.append(tmat)

    # ── Capacity sub-label ────────────────────────────────────────────
    bpy.ops.object.text_add(
        location=({ASSEMBLY_X}, {ASSEMBLY_Y} + 3.5, {ASSEMBLY_Z} + 0.5))
    cap = bpy.context.object
    cap.name = 'AssemblyPoint_Capacity'
    cap.data.body        = 'Overflow area - max 20 persons'
    cap.data.size        = 0.6
    cap.data.align_x     = 'CENTER'
    cap.rotation_euler   = (1.5708, 0, 0)
    cap.data.materials.clear()
    cap.data.materials.append(tmat)

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()

    report = {{
        'status'  : 'ok',
        'location': [{ASSEMBLY_X}, {ASSEMBLY_Y}, {ASSEMBLY_Z}],
        'objects' : ['AssemblyPoint_Circle', 'AssemblyPoint_Label',
                     'AssemblyPoint_Capacity']
    }}

except Exception as e:
    import traceback
    report = {{'status': 'error', 'message': str(e),
               'trace': traceback.format_exc()}}

with open(r"{RESULT_FILE}", "w", encoding="utf-8") as f:
    json.dump(report, f)
print('Assembly point created:', report.get('status'))
"""
    send_to_blender(code)
    return _read_result(timeout=20.0)


def move_cones_to_assembly(marker_ids: list) -> None:
    """
    Moves a list of cone markers to the assembly point with jitter.
    Clears baked keyframes from these cones first so the drive loop
    cannot override their positions with keyframe values.
    Cones turn orange to show they are redirected occupants.
    marker_ids: list of int e.g. [0, 3, 7, 12, 15]
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
        # Clear baked keyframes so drive loop cannot override position
        if obj.animation_data:
            obj.animation_data_clear()
        if obj.data and obj.data.materials:
            mat = obj.data.materials[0]
            if mat.node_tree and mat.node_tree.animation_data:
                mat.node_tree.animation_data_clear()

        # Now move to assembly point permanently
        obj.location = (x, y, z)
        obj.color    = ORANGE
        if obj.data.materials:
            mat  = obj.data.materials[0]
            bsdf = mat.node_tree.nodes.get('Principled BSDF')
            if bsdf:
                bsdf.inputs['Base Color'].default_value      = ORANGE
                bsdf.inputs['Emission Color'].default_value   = ORANGE
                bsdf.inputs['Emission Strength'].default_value = 1.5
        moved += 1
    else:
        print(f'Cone not found: {{name}}')

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()

print(f'Moved {{moved}}/{{len(positions)}} cones to assembly point (keyframes cleared)')
""")

def test_assembly_point() -> None:
    """
    Quick test — moves 5 cones to the assembly point immediately.
    Run: python -c "from bim.assembly_point import test_assembly_point; test_assembly_point()"
    """
    print("Moving 5 test cones to assembly point...")
    move_cones_to_assembly([0, 1, 2, 3, 4])
    print("Done — check Blender viewport")