# bim/viewport_utils.py
import os, json
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE


def frame_view_on_objects(prefix: str) -> dict:
    if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)
    code = f"""
import bpy, json
targets = [o for o in bpy.data.objects if o.name.startswith('{prefix}')]
for o in bpy.context.selected_objects: o.select_set(False)
for o in targets: o.select_set(True)
if targets: bpy.context.view_layer.objects.active = targets[0]
area = region = None
for a in bpy.context.screen.areas:
    if a.type=='VIEW_3D':
        area = a
        for r in a.regions:
            if r.type=='WINDOW': region = r; break
        break
result = {{'count':len(targets),'framed':False}}
if area and region and targets:
    try:
        with bpy.context.temp_override(area=area,region=region):
            bpy.ops.view3d.view_selected()
        result['framed'] = True
    except Exception as e:
        result['error'] = str(e)
for a in bpy.context.screen.areas:
    if a.type=='VIEW_3D': a.tag_redraw()
with open(r"{RESULT_FILE}","w",encoding="utf-8") as f:
    json.dump(result,f)
"""
    send_to_blender(code)
    return _read_result(timeout=8.0)


def frame_view_on_specific_objects(names: list) -> dict:
    """
    Same mechanism as frame_view_on_objects(), generalized to an
    explicit list of object names instead of a prefix match — lets a
    caller select an EXACT, precomputed set of objects (e.g. only the
    occupant cones that are actually inside a specific violating room
    at a specific tick, from a real simulation timeline) rather than
    every object matching a naming pattern.

    Known limitation, see frame_view_on_room() below: view_selected()
    fits TIGHTLY to whatever's selected — with only 1-2 small cone
    objects, this produces an extremely tight, disorienting close-up
    with no room context. Prefer frame_view_on_room() for showing a
    violation; this remains available for cases that genuinely want a
    tight fit-to-object framing (e.g. all 80 cones at once).

    names: list of exact object names, e.g. ["Occupant_004", "Occupant_017"]
    """
    if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)
    names_json = json.dumps(names)
    code = f"""
import bpy, json
wanted  = set(json.loads('{names_json}'))
targets = [o for o in bpy.data.objects if o.name in wanted]
for o in bpy.context.selected_objects: o.select_set(False)
for o in targets: o.select_set(True)
if targets: bpy.context.view_layer.objects.active = targets[0]
area = region = None
for a in bpy.context.screen.areas:
    if a.type=='VIEW_3D':
        area = a
        for r in a.regions:
            if r.type=='WINDOW': region = r; break
        break
result = {{'requested': len(wanted), 'count': len(targets), 'framed': False}}
if area and region and targets:
    try:
        with bpy.context.temp_override(area=area,region=region):
            bpy.ops.view3d.view_selected()
        result['framed'] = True
    except Exception as e:
        result['error'] = str(e)
for a in bpy.context.screen.areas:
    if a.type=='VIEW_3D': a.tag_redraw()
with open(r"{RESULT_FILE}","w",encoding="utf-8") as f:
    json.dump(result,f)
"""
    send_to_blender(code)
    return _read_result(timeout=8.0)

def position_interior_camera(x: float, y: float, z: float,
                             dx: float, dy: float,
                             room_label: str = "") -> dict:
    """
    Creates or repositions a dedicated 'Interior_Camera' object inside
    the room at eye level, wide-angle lens, framed toward the room's
    centre. Unlike frame_view_on_room() (which repositions whatever
    VIEW_3D area happens to be first in bpy.context.screen.areas), a
    real camera object can be bound to ONE SPECIFIC split-screen panel
    via Numpad 0 while a second panel stays on the normal global
    overview — the correct architecture for a genuine split-screen
    demo, where repositioning "the" viewport's view has no way to
    leave a second, manually-split panel untouched.

    x, y, z, dx, dy : the room's real centroid + extent, from
                       load_room_centroids() — this project's actual
                       spatial data source for rooms (bim_query.SPACES
                       holds IFC properties like max_occ, not
                       centroids, and has no "label" field at all).
    """
    if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)
    size = max(dx, dy)
    code = f"""
import bpy, mathutils, json

cam_name = 'Interior_Camera'
cam_obj  = bpy.data.objects.get(cam_name)
if not cam_obj:
    cam_data = bpy.data.cameras.new(cam_name)
    cam_obj  = bpy.data.objects.new(cam_name, cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    cam_data.lens        = 18
    cam_data.clip_start  = 0.05

back = max({size} * 0.6, 1.5)
cam_location = mathutils.Vector(({x} - back, {y} - back, {z} + 1.4))
target_point = mathutils.Vector(({x}, {y}, {z} + 0.8))

cam_obj.location = cam_location
direction = target_point - cam_location
cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

bpy.context.scene.camera = cam_obj

result = {{'camera_positioned': True, 'location': list(cam_location)}}
with open(r"{RESULT_FILE}", "w", encoding="utf-8") as f:
    json.dump(result, f)
print('Interior camera positioned for {room_label}: ' + str(cam_location))
"""
    send_to_blender(code)
    return _read_result(timeout=8.0)


def set_upper_floor_occlusion(above_z: float, hide: bool) -> dict:
    """
    Wireframes wall/slab/roof/column geometry positioned ABOVE
    above_z, so an interior camera inside a room isn't blocked by the
    floor(s) above it. Only touches geometry above above_z — the
    target room's own floor and everything below stays fully solid,
    for spatial grounding.

    Fix applied to a suggested hide_viewport-based approach: switched
    to the same display_type='WIRE' technique already proven for
    building-wide transparency in live_agent_runner.py (see
    _make_building_transparent()) rather than fully hiding objects —
    hiding loses all sense of the building's structure above; a
    wireframe outline preserves it while still seeing through.

    above_z : real-world height (e.g. the target room's own z, from
              load_room_centroids()) — geometry above this gets
              wireframed, everything at or below is untouched
    hide    : True to wireframe, False to restore normal display
    """
    if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)
    display_type = "'WIRE'" if hide else "'TEXTURED'"
    code = f"""
import bpy, json

TRANSPARENT_PREFIXES = (
    'IfcWall', 'IfcSlab', 'IfcRoof', 'IfcColumn', 'IfcCurtainWall',
)
KEEP_OPAQUE_PREFIXES = (
    'IfcDoor', 'IfcWindow', 'IfcStair', 'IfcRailing', 'IfcFurnishing',
)

count = 0
for obj in bpy.data.objects:
    if obj.type != 'MESH':
        continue
    if obj.location.z <= {above_z}:
        continue
    if any(obj.name.startswith(p) for p in KEEP_OPAQUE_PREFIXES):
        continue
    if not any(obj.name.startswith(p) for p in TRANSPARENT_PREFIXES):
        continue
    obj.display_type = {display_type}
    count += 1

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()

result = {{'objects_touched': count}}
with open(r"{RESULT_FILE}", "w", encoding="utf-8") as f:
    json.dump(result, f)
print(str(count) + ' upper-floor objects set to ' + {display_type})
"""
    send_to_blender(code)
    return _read_result(timeout=8.0)