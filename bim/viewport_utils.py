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