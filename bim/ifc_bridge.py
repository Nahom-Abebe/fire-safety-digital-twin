# bim/ifc_bridge.py
# Blender socket connection + Pset get/set via GlobalId
# Protocol: plain UTF-8 JSON over TCP (proven working)
# Result channel: file-based (Blender writes JSON, Python reads it)

import socket, json, os, time

BLENDER_HOST = "localhost"
BLENDER_PORT = 9876
TIMEOUT      = 15.0

BIM_DIR     = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = os.path.join(BIM_DIR, "_result.json").replace("\\", "/")


def send_to_blender(code: str) -> dict:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((BLENDER_HOST, BLENDER_PORT))
            s.sendall(json.dumps({"type":"execute_code","params":{"code":code}}).encode())
            resp = b""
            s.settimeout(5.0)
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk: break
                    resp += chunk
                    try: json.loads(resp.decode()); break
                    except json.JSONDecodeError: continue
            except socket.timeout:
                pass
            if not resp:
                return {"status":"sent"}
            try: return json.loads(resp.decode())
            except: return {"status":"sent","raw":resp.decode("utf-8",errors="ignore")}
    except ConnectionRefusedError:
        return {"error":"Cannot connect","detail":"Start Bonsai MCP server in N-panel"}
    except Exception as e:
        return {"error":str(e)}


def test_connection() -> bool:
    return "error" not in send_to_blender("print('OK')")


def _read_result(timeout=8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(RESULT_FILE):
            try:
                with open(RESULT_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(RESULT_FILE)
                return data
            except: time.sleep(0.05)
        time.sleep(0.05)
    return {"error":"Timed out waiting for result file"}


def get_ifc_pset_properties(global_id: str, pset_name: str) -> dict:
    if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)
    code = f"""
import json
from bonsai.bim.ifc import IfcStore
model  = IfcStore.get_file()
result = {{"global_id":"{global_id}","pset_name":"{pset_name}","properties":{{}},"found":False}}
if model:
    element = model.by_guid("{global_id}")
    if element:
        for rel in model.by_type("IfcRelDefinesByProperties"):
            if element not in rel.RelatedObjects: continue
            pset = rel.RelatingPropertyDefinition
            if hasattr(pset,"Name") and pset.Name == "{pset_name}":
                result["found"] = True
                for p in pset.HasProperties:
                    try: result["properties"][p.Name] = p.NominalValue.wrappedValue
                    except: result["properties"][p.Name] = None
                break
with open(r"{RESULT_FILE}","w",encoding="utf-8") as f:
    json.dump(result,f)
print("pset read done")
"""
    r = send_to_blender(code)
    if "error" in r: return r
    return _read_result()


def update_ifc_pset_properties(global_id: str, pset_name: str, props: dict) -> dict:
    props_literal = repr(props)
    code = f"""
import ifcopenshell, ifcopenshell.api
from bonsai.bim.ifc import IfcStore
model = IfcStore.get_file()
if model:
    element = model.by_guid("{global_id}")
    if element:
        target = None
        for rel in model.by_type("IfcRelDefinesByProperties"):
            if element in rel.RelatedObjects:
                p = rel.RelatingPropertyDefinition
                if hasattr(p,"Name") and p.Name == "{pset_name}":
                    target = p; break
        if target is None:
            target = ifcopenshell.api.run("pset.add_pset",model,
                product=element,name="{pset_name}")
        ifcopenshell.api.run("pset.edit_pset",model,
            pset=target,properties={props_literal})
        print(f"Updated {pset_name} on {global_id}")
"""
    return send_to_blender(code)