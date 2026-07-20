# bim/pset_sync.py
# Writes live occupancy data to Pset_FireSafetyStatus for every
# occupied room in a single batched Blender round trip.

import json, os
from bim.ifc_bridge import send_to_blender, _read_result, RESULT_FILE
from bim.bim_query import SPACES
from bim.room_geometry import GRAPH_TO_IFC

# Pre-build graph-label → GlobalId lookup
_ifc_name_to_gid = {s["name"]: gid for gid, s in SPACES.items()}
LABEL_TO_GID = {
    label: _ifc_name_to_gid.get(ifc_name)
    for label, ifc_name in GRAPH_TO_IFC.items()
    if _ifc_name_to_gid.get(ifc_name)
}


def bulk_update_occupancy_psets(snapshot: dict) -> dict:
    """
    Writes CurrentOccupancy + ComplianceStatus to Pset_FireSafetyStatus
    for every occupied room. Single batched Blender round trip per tick.
    This is the live Digital Twin write-back — values persist in the IFC file.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy

    label_to_node = {d["label"]: n for n, d in BUILDING_GRAPH.nodes(data=True)}
    occupancy     = snapshot.get("occupancy", {})
    over_labels   = {a["label"] for a in snapshot.get("alerts", [])
                     if a["severity"] == "OVER"}
    warn_labels   = {a["label"] for a in snapshot.get("alerts", [])
                     if a["severity"] == "WARNING"}

    updates = []
    for label, count in occupancy.items():
        gid  = LABEL_TO_GID.get(label)
        node = label_to_node.get(label)
        if not gid or node is None:
            continue
        max_occ    = get_max_occupancy(node)
        compliance = ("FAIL"    if label in over_labels
                      else "WARNING" if label in warn_labels
                      else "PASS")
        updates.append((gid, count, max_occ, compliance))

    if not updates:
        return {"updated": 0}

    updates_json = json.dumps(updates)

    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    code = f"""
import ifcopenshell, ifcopenshell.api, json, traceback
from bonsai.bim.ifc import IfcStore

try:
    model   = IfcStore.get_file()
    updates = json.loads('{updates_json}')
    count   = 0

    if model:
        for gid, current, max_occ, compliance in updates:
            element = model.by_guid(gid)
            if not element:
                continue
            target = None
            for rel in model.by_type("IfcRelDefinesByProperties"):
                if element in rel.RelatedObjects:
                    p = rel.RelatingPropertyDefinition
                    if hasattr(p, "Name") and p.Name == "Pset_FireSafetyStatus":
                        target = p
                        break
            if target is None:
                target = ifcopenshell.api.run(
                    "pset.add_pset", model,
                    product=element, name="Pset_FireSafetyStatus")
            ifcopenshell.api.run("pset.edit_pset", model,
                pset=target,
                properties={{
                    "CurrentOccupancy" : current,
                    "ComplianceStatus" : compliance,
                    "LastUpdatedBy"    : "SimulationTick",
                }})
            count += 1

    report = {{"status": "ok", "updated": count}}

except Exception as e:
    report = {{"status": "error", "message": str(e),
               "trace": traceback.format_exc()}}

with open(r"{RESULT_FILE}", "w", encoding="utf-8") as f:
    json.dump(report, f)
print("Pset sync:", report.get("status"), report.get("updated", ""))
"""
    send_to_blender(code)
    return _read_result(timeout=20.0)


def write_fire_alarm_status(fire_node_label: str, active: bool) -> dict:
    """
    Writes FireAlarmStatus = True/False to Pset_FireSafetyStatus on the
    IFC space corresponding to the fire zone graph label.

    This is Peter Lawrence's bidirectional Digital Twin requirement:
    the hazard state is written into the actual IFC model, not just held
    in Python memory. If you save the IFC after running, the FireAlarmStatus
    is stored permanently in the BIM asset.

    fire_node_label : graph label e.g. '0-A'
    active          : True = alarm active, False = all-clear
    """
    ifc_name = GRAPH_TO_IFC.get(fire_node_label)
    if not ifc_name:
        return {"error": f"No IFC mapping found for graph label '{fire_node_label}'"}

    gid = _ifc_name_to_gid.get(ifc_name)
    if not gid:
        return {"error": f"No GlobalId found for IFC name '{ifc_name}'"}

    from bim.ifc_bridge import update_ifc_pset_properties
    result = update_ifc_pset_properties(
        gid,
        "Pset_FireSafetyStatus",
        {
            "FireAlarmStatus" : active,
            "ComplianceStatus": "FAIL" if active else "PASS",
            "LastUpdatedBy"   : "FireAlarmTrigger",
        }
    )
    return {
        "status"          : "ok",
        "graph_label"     : fire_node_label,
        "ifc_name"        : ifc_name,
        "global_id"       : gid,
        "FireAlarmStatus" : active,
        "ifc_result"      : result,
    }


def reset_all_psets() -> dict:
    """
    Resets FireAlarmStatus to False on all rooms.
    Call this between evaluation scenarios to clear previous state.
    """
    reset_count = 0
    errors      = []

    for label, gid in LABEL_TO_GID.items():
        if not gid:
            continue
        try:
            from bim.ifc_bridge import update_ifc_pset_properties
            update_ifc_pset_properties(gid, "Pset_FireSafetyStatus", {
                "CurrentOccupancy" : 0,
                "ComplianceStatus" : "UNKNOWN",
                "FireAlarmStatus"  : False,
                "LastUpdatedBy"    : "System",
            })
            reset_count += 1
        except Exception as e:
            errors.append(f"{label}: {e}")

    return {
        "reset"  : reset_count,
        "errors" : errors,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sensors.sensor_sim import initialise_occupants, get_sensor_snapshot

    print("Testing pset_sync...")
    initialise_occupants(80, seed=42)
    snap = get_sensor_snapshot()

    print(f"Occupancy snapshot: {snap['total_occ']} occupants")
    result = bulk_update_occupancy_psets(snap)
    print(f"Bulk update: {result}")

    print("\nTesting FireAlarmStatus write...")
    result = write_fire_alarm_status("0-A", True)
    print(f"FireAlarmStatus: {result}")