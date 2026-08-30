# bim/signage.py
# Updates evacuation sign Psets via GlobalId and feeds status back
# into the simulation graph so occupant movement probabilities update.
# Also updates physical corridor sign panels in Blender interior view.

from bim.ifc_bridge import update_ifc_pset_properties
from bim.bim_query import get_sign


def update_sign(sign_id: str, message: str, status: str,
                adb_ref: str = "") -> dict:
    """
    Updates a sign's Pset_EvacuationSign in the IFC model AND
    feeds the status back into the simulation graph so occupant
    movement probabilities update on the next tick AND
    updates the physical corridor sign panel in Blender interior view.

    sign_id : matches 'name' field in global_ids_v2.json
    message : text displayed on the sign
    status  : ACTIVE | BLOCKED | ALTERNATE
    adb_ref : ADB section justifying this decision (for traceability)
    """
    sign = get_sign(sign_id)
    if "error" in sign:
        return sign

    # Write to IFC via GlobalId
    result = update_ifc_pset_properties(
        sign["global_id"], "Pset_EvacuationSign",
        {
            "CurrentMessage": message,
            "Status"        : status,
            "ADBReference"  : adb_ref,
            "LastUpdatedBy" : "OccupancyManagementAgent",
        }
    )

    # Feed status back into simulation graph
    points_toward = sign.get("points_toward", "")
    if points_toward:
        _feed_back_to_simulation(points_toward, status)

    # Update physical corridor sign panel in Blender interior view
    _update_blender_sign_panel(sign_id, message, status, adb_ref)

    return {
        "sign_id"      : sign_id,
        "status"       : status,
        "message"      : message,
        "adb_ref"      : adb_ref,
        "points_toward": points_toward,
        "ifc_result"   : result,
    }


def _feed_back_to_simulation(points_toward: str, status: str):
    """
    Translates a sign's points_toward field into a graph node label
    and updates the simulation's sign_blocked property so occupants
    avoid blocked routes on the next movement tick.
    """
    try:
        from sensors.sensor_sim import update_sign_status

        TARGET_MAP = {
            "EXIT-NORTH"   : "EXIT-1",
            "EXIT-SOUTH"   : "EXIT-3",
            "EXIT-EAST"    : "EXIT-2",
            "EXIT-WEST"    : "EXIT-4",
            "STAIR-A-F1"  : "B-L1",
            "STAIR-A-F2"  : "B-L2",
            "STAIR-A-F3"  : "B-L3",
            "ASSEMBLY-AREA": "EXIT-1",
            "FIRE-DOOR"    : None,
        }

        graph_label = TARGET_MAP.get(points_toward, points_toward)
        if graph_label:
            update_sign_status(graph_label, status)
    except Exception:
        pass


def _update_blender_sign_panel(sign_id: str, message: str,
                                 status: str, adb_ref: str = ""):
    """
    Updates the physical corridor sign panel in Blender's interior view.
    Panel colour:
      ACTIVE    -> green  (route open)
      BLOCKED   -> red    (route closed)
      ALTERNATE -> amber  (use alternate route)

    Panel text shows the message and ADB reference.
    Silently skips if the panel object does not exist yet
    (created by interior_signage.create_corridor_signs).
    """
    try:
        from bim.ifc_bridge import send_to_blender

        colours = {
            "ACTIVE"   : [0.05, 0.70, 0.15, 1.0],   # green
            "BLOCKED"  : [0.90, 0.05, 0.05, 1.0],   # red
            "ALTERNATE": [0.90, 0.45, 0.00, 1.0],   # amber
        }
        colour = colours.get(status, colours["ACTIVE"])

        # Truncate for panel display
        display_msg = message[:80].replace("'", "")
        display_status = status

        send_to_blender(f"""
import bpy

panel = bpy.data.objects.get('SignPanel_{sign_id}')
txt   = bpy.data.objects.get('SignText_{sign_id}')

if panel and panel.data.materials:
    mat  = panel.data.materials[0]
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value     = {colour}
        node.inputs['Emission Color'].default_value  = {colour}
        node.inputs['Emission Strength'].default_value = 2.0

if txt:
    ref_line = '\\nRef: {display_ref}' if '{display_ref}' else ''
    txt.data.body = '{display_status}: {display_msg}'

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()

print('Sign panel updated: {sign_id} -> {status}')
""")
    except Exception:
        pass

def reset_all_signs() -> list:
    """
    Resets all signs to ACTIVE status and clears all corridor panels
    to green. Used at the start of each demo session or scenario.
    """
    from bim.bim_query import get_all_signs
    from bim.ifc_bridge import send_to_blender

    results = []

    # Reset IFC Psets
    for sign in get_all_signs():
        r = update_ifc_pset_properties(
            sign["global_id"], "Pset_EvacuationSign",
            {
                "Status"        : "ACTIVE",
                "CurrentMessage": sign.get("message", "All routes open"),
                "LastUpdatedBy" : "System",
            }
        )
        results.append({"sign": sign["name"], "result": r})

    # Reset all corridor sign panels to green in Blender
    try:
        send_to_blender("""
import bpy

GREEN = [0.05, 0.70, 0.15, 1.0]
reset_count = 0

for obj in bpy.data.objects:
    if obj.name.startswith('SignPanel_') and obj.data and obj.data.materials:
        mat  = obj.data.materials[0]
        node = mat.node_tree.nodes.get('Principled BSDF')
        if node:
            node.inputs['Base Color'].default_value     = GREEN
            node.inputs['Emission Color'].default_value  = GREEN
            node.inputs['Emission Strength'].default_value = 1.5
        reset_count += 1

    if obj.name.startswith('SignText_'):
        floor = obj.name.split('_')[2] if '_' in obj.name else ''
        obj.data.body = f'{floor} CORRIDOR\\nStatus: CLEAR\\nAll routes open'

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()

print(f'Reset {reset_count} sign panels to green')
""")
    except Exception:
        pass

    return results