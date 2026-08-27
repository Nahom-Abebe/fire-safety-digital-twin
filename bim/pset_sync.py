# bim/pset_sync.py
# Writes live occupancy data to Pset_FireSafetyStatus for every
# occupied room in a single batched Blender round trip.
# Also writes FireAlarmStatus=True to the fire zone space 

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
    Writes CurrentOccupancy + MaxOccupancy + ComplianceStatus to
    Pset_FireSafetyStatus for every occupied room. Single batched
    Blender round trip per tick. This is the live Digital Twin
    write-back — values persist in the IFC file.

    Fix applied (re-applied — this was fixed once already earlier in
    the project's history, but the working copy of this file had
    regressed to the version below without it):

    ComplianceStatus was being derived from snapshot["alerts"], via
    over_labels/warn_labels built from severity tags on that list.
    But sensor_sim.py's is_occupancy_alert_relevant() deliberately
    EXCLUDES non-occupiable, personal-care, and circulation room
    types (Bath, W/C, Store, Storage, Corridor, Stair, Lobby) from
    ever appearing in snapshot["alerts"] at all — a fix made
    specifically to cut alarm-fatigue noise from single-occupant
    rooms. That's correct for what triggers a live alert, but it
    means those room TYPES could never reach FAIL or WARNING here,
    regardless of actual occupancy — confirmed directly against a
    real Pset dump: a Bath at 2/1 and a Corridor at 18/10 both showed
    ComplianceStatus=PASS, strictly contradicting their own numbers.

    Now computes ComplianceStatus directly from count vs max_occ for
    EVERY occupied room, completely decoupled from alert-relevance
    scope — a room's Pset compliance reflects its own real numbers
    unconditionally, regardless of whether that room's type is
    considered alert-worthy for live signage purposes. Also closes a
    related gap: this had no WARNING tier at all, unlike
    bim_query.py's check_occupancy_compliance() (used by the live
    agent's check_compliance tool), which already uses an 80%
    threshold. Every room exactly at capacity (W/C 1/1, Store 1/1,
    Corridor 10/10, etc.) showed PASS here despite being genuinely at
    capacity. Now uses the same 80% threshold as the live agent path,
    so both compliance-checking pathways in this project agree with
    each other.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy

    label_to_node = {d["label"]: n for n, d in BUILDING_GRAPH.nodes(data=True)}
    occupancy     = snapshot.get("occupancy", {})

    updates = []
    for label, count in occupancy.items():
        gid  = LABEL_TO_GID.get(label)
        node = label_to_node.get(label)
        if not gid or node is None:
            continue
        max_occ = get_max_occupancy(node)

        # Direct comparison — see fix note above. Never gated by
        # whether this room's type would trigger a live alert.
        ratio = (count / max_occ) if max_occ > 0 else 0
        if count > max_occ:
            compliance = "FAIL"
        elif max_occ > 0 and ratio >= 0.8:
            compliance = "WARNING"
        else:
            compliance = "PASS"

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
                    "MaxOccupancy"     : max_occ,
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
    Resets FireAlarmStatus to False on all rooms, and MaxOccupancy to
    the real value from get_max_occupancy() so a fresh reset can't
    leave a stale or externally-sourced capacity number sitting next
    to a freshly-reset CurrentOccupancy=0 / ComplianceStatus=UNKNOWN.
    Call this between evaluation scenarios to clear previous state.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy

    label_to_node = {d["label"]: n for n, d in BUILDING_GRAPH.nodes(data=True)}

    reset_count = 0
    errors      = []

    for label, gid in LABEL_TO_GID.items():
        if not gid:
            continue
        try:
            from bim.ifc_bridge import update_ifc_pset_properties
            node    = label_to_node.get(label)
            max_occ = get_max_occupancy(node) if node is not None else None
            props = {
                "CurrentOccupancy" : 0,
                "ComplianceStatus" : "UNKNOWN",
                "FireAlarmStatus"  : False,
                "LastUpdatedBy"    : "System",
            }
            if max_occ is not None:
                props["MaxOccupancy"] = max_occ
            update_ifc_pset_properties(gid, "Pset_FireSafetyStatus", props)
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