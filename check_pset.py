# check_pset.py
# Reads Pset_FireSafetyStatus from the IFC model for a specific room
# and prints the current ComplianceStatus.

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE

if os.path.exists(RESULT_FILE):
    os.remove(RESULT_FILE)

result = send_to_blender(f"""
import json, traceback
from bonsai.bim.ifc import IfcStore

try:
    model = IfcStore.get_file()
    results = []

    # Check all spaces for Pset_FireSafetyStatus
    for space in model.by_type("IfcSpace"):
        name = space.LongName or space.Name or "Unknown"
        for rel in model.by_type("IfcRelDefinesByProperties"):
            if space in rel.RelatedObjects:
                p = rel.RelatingPropertyDefinition
                if hasattr(p, "Name") and p.Name == "Pset_FireSafetyStatus":
                    props = {{}}
                    for prop in p.HasProperties:
                        if hasattr(prop, "NominalValue") and prop.NominalValue:
                            props[prop.Name] = str(prop.NominalValue.wrappedValue)
                    if props:
                        results.append({{"room": name, "pset": props}})

    report = {{"status": "ok", "results": results}}

except Exception as e:
    report = {{"status": "error", "message": str(e),
               "trace": traceback.format_exc()}}

with open(r"{RESULT_FILE}", "w", encoding="utf-8") as f:
    json.dump(report, f)
print("Pset check done")
""")

data = _read_result(timeout=20.0)

if data.get("status") == "ok":
    results = data.get("results", [])
    if not results:
        print("No Pset_FireSafetyStatus found — run a scenario first")
    else:
        print(f"\nPset_FireSafetyStatus found on {len(results)} room(s):\n")
        for r in results:
            print(f"  Room: {r['room']}")
            for k, v in r["pset"].items():
                flag = " ← VIOLATION" if v == "FAIL" else ""
                print(f"    {k:20s}: {v}{flag}")
            print()
else:
    print(f"Error: {data}")