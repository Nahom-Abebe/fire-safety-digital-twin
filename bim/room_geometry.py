# bim/room_geometry.py
import json, os
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE

BIM_DIR       = os.path.dirname(os.path.abspath(__file__))
CENTROID_FILE = os.path.join(BIM_DIR, "room_centroids.json")

# Graph label → IFC room number 
GRAPH_TO_IFC = {
    "0-1":"029","0-2":"028","0-3":"027","0-4":"002","0-5":"007",
    "0-6":"005","0-7":"003","0-8":"008","0-9":"009","0-10":"011",
    "0-11":"010","0-12":"012","0-13":"013","0-14":"014","0-15":"016",
    "0-16":"015","0-17":"025","0-18":"026","0-19":"0023","0-20":"0022",
    "0-A":"0022","0-B":"0023",
    "1-1":"113","1-2":"114","1-3":"115","1-4":"116","1-5":"117",
    "1-6":"119","1-7":"120","1-8":"121","1-9":"122","1-10":"123",
    "1-11":"124","1-12":"129","1-13":"131","1-14":"132","1-15":"134",
    "1-16":"135","1-17":"136","1-18":"137","1-19":"138","1-20":"139",
    "1-A":"125","1-B":"126",
    "2-1":"228","2-2":"229","2-3":"230","2-4":"231","2-5":"232",
    "2-6":"233","2-7":"234","2-8":"235","2-9":"236","2-10":"237",
    "2-11":"239","2-12":"244","2-13":"245","2-14":"246","2-15":"247",
    "2-16":"248","2-17":"249","2-18":"250","2-19":"251","2-20":"252",
    "2-A":"240","2-B":"241",
    "3-1":"343","3-2":"344","3-3":"345","3-4":"346","3-5":"347",
    "3-6":"348","3-7":"349","3-8":"350","3-9":"352","3-10":"353",
    "3-11":"354","3-12":"359","3-13":"360","3-14":"361","3-15":"362",
    "3-16":"363","3-17":"364","3-18":"365","3-19":"366","3-20":"367",
    "3-A":"355","3-B":"356",
}


def extract_room_centroids() -> dict:
    if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)
    mapping_json = json.dumps(GRAPH_TO_IFC)
    code = f"""
import bpy, json
from mathutils import Vector
mapping = json.loads('{mapping_json}')
results = {{}}
for label, ifc_name in mapping.items():
    obj = bpy.data.objects.get(f"IfcSpace/{{ifc_name}}")
    if obj is None or obj.type != 'MESH': continue
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs=[c.x for c in corners]; ys=[c.y for c in corners]; zs=[c.z for c in corners]
    results[label] = {{
        "ifc_name": ifc_name,
        "x": (min(xs)+max(xs))/2, "y": (min(ys)+max(ys))/2,
        "z": min(zs), "dx": max(xs)-min(xs), "dy": max(ys)-min(ys),
    }}
with open(r"{RESULT_FILE}","w",encoding="utf-8") as f:
    json.dump(results,f)
print(f"Extracted {{len(results)}} centroids")
"""
    send_to_blender(code)
    result = _read_result(timeout=12.0)
    if "error" in result: return result
    with open(CENTROID_FILE,"w",encoding="utf-8") as f:
        json.dump(result,f,indent=2)
    return {"extracted": len(result), "saved_to": CENTROID_FILE}


def load_room_centroids() -> dict:
    if not os.path.exists(CENTROID_FILE):
        raise FileNotFoundError("Run extract_room_centroids() first")
    with open(CENTROID_FILE, encoding="utf-8") as f:
        return json.load(f)