# bim/signage.py
# Updates evacuation sign Psets via GlobalId
# Also feeds the new status back into the simulation's edge weights

from bim.ifc_bridge import update_ifc_pset_properties
from bim.bim_query import get_sign


def update_sign(sign_id: str, message: str, status: str,
                 adb_ref: str = "") -> dict:
    """
    Updates a sign's Pset_EvacuationSign in the IFC model AND
    feeds the status back into the simulation graph so occupant
    movement probabilities update on the next tick.

    sign_id : matches 'name' field in global_ids_v2.json
    message : text displayed on the sign
    status  : ACTIVE | BLOCKED | ALTERNATE
    adb_ref : ADB section justifying this decision (for traceability)
    """
    sign = get_sign(sign_id)
    if "error" in sign:
        return sign

    # Write to IFC via GlobalId (Peter's pattern)
    result = update_ifc_pset_properties(
        sign["global_id"], "Pset_EvacuationSign",
        {
            "CurrentMessage": message,
            "Status"        : status,
            "LastUpdatedBy" : "FireSafetyAgent",
        }
    )

    # Feed status back into simulation (key bidirectional step)
    points_toward = sign.get("points_toward", "")
    if points_toward:
        _feed_back_to_simulation(points_toward, status)

    return {
        "sign_id"     : sign_id,
        "status"      : status,
        "message"     : message,
        "adb_ref"     : adb_ref,
        "points_toward": points_toward,
        "ifc_result"  : result,
    }


def _feed_back_to_simulation(points_toward: str, status: str):
    """
    Translates a sign's points_toward field into a graph node label
    and updates the simulation's sign_blocked property.
    """
    try:
        from sensors.sensor_sim import update_sign_status

        # Map sign direction targets to graph node labels
        TARGET_MAP = {
            "EXIT-NORTH"  : "EXIT-1",
            "EXIT-SOUTH"  : "EXIT-3",
            "EXIT-EAST"   : "EXIT-2",
            "EXIT-WEST"   : "EXIT-4",
            "STAIR-A-F1"  : "B-L1",
            "STAIR-A-F2"  : "B-L2",
            "STAIR-A-F3"  : "B-L3",
            "ASSEMBLY-AREA": "EXIT-1",
            "FIRE-DOOR"   : None,
        }

        graph_label = TARGET_MAP.get(points_toward, points_toward)
        if graph_label:
            update_sign_status(graph_label, status)
    except Exception:
        pass  # simulation may not be running — silently skip


def reset_all_signs() -> list:
    """Resets all signs to ACTIVE status. Used between scenarios."""
    from bim.bim_query import get_all_signs
    results = []
    for sign in get_all_signs():
        r = update_ifc_pset_properties(
            sign["global_id"], "Pset_EvacuationSign",
            {"Status": "ACTIVE", "CurrentMessage": sign.get("message", ""),
             "LastUpdatedBy": "System"}
        )
        results.append({"sign": sign["name"], "result": r})
    return results