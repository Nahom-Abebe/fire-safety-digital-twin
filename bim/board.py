# bim/board.py
# Building manager's dashboard text panel in Blender.

from bim.ifc_bridge import send_to_blender

_MAX_LINE = 45   

FLOOR_COLLECTIONS = {
    "F0 Ground Floor": "IfcBuildingStorey/F0 Ground Floor",
    "F1 First Floor" : "IfcBuildingStorey/F1 First Floor",
    "F2 Second Floor": "IfcBuildingStorey/F2 Second Floor",
    "F3 Third Floor" : "IfcBuildingStorey/F3 Third Floor",
}

SKIP_PREFIXES = (
    'IfcFurnishing', 'IfcBuildingElementProxy', 'IfcFlowTerminal',
    'IfcSanitaryTerminal', 'IfcLightFixture', 'IfcAlarm', 'IfcSign',
    'IfcDoor', 'IfcWindow',
)


def _truncate(text: str) -> str:
    """Truncate a single line to _MAX_LINE characters."""
    return text[:_MAX_LINE] if len(text) > _MAX_LINE else text


def create_board() -> dict:
    """Creates the FireSafetyBoard text object if it does not already exist."""
    code = """
import bpy
if 'FireSafetyBoard' not in bpy.data.objects:
    bpy.ops.object.text_add(location=(-30.0, -5.0, 16.0))
    b = bpy.context.object
    b.name = 'FireSafetyBoard'
    b.data.body = 'FIRE SAFETY DIGITAL TWIN'
    b.data.size = 0.85
    b.data.align_x = 'LEFT'
    b.rotation_euler = (1.5708, 0, 0)
    mat = bpy.data.materials.new('BoardText')
    mat.use_nodes = True
    n = mat.node_tree.nodes.get('Principled BSDF')
    if n:
        n.inputs['Base Color'].default_value     = (1.0, 0.85, 0.0, 1.0)
        n.inputs['Emission Color'].default_value  = (1.0, 0.85, 0.0, 1.0)
        n.inputs['Emission Strength'].default_value = 2.0
    b.data.materials.clear()
    b.data.materials.append(mat)
    print('Board created')
else:
    print('Board already exists')
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    return send_to_blender(code)


def update_board(snapshot: dict, agent_message: str = "") -> dict:
    """
    Updates the board with current occupancy state and agent directive.
    Turns every floor collection RED if evacuation_mode is active.
    """
    by_floor   = snapshot.get("by_floor", {})
    alerts     = snapshot.get("alerts", [])
    total      = snapshot.get("total_occ", 0)
    tick       = snapshot.get("tick", 0)
    timestamp  = snapshot.get("timestamp", "")
    evac_mode  = snapshot.get("evacuation_mode", False)

    FLOOR_ORDER = [
        "F0 Ground Floor",
        "F1 First Floor",
        "F2 Second Floor",
        "F3 Third Floor",
    ]

    FLOOR_SHORT = {
        "F0 Ground Floor": "F0 Ground",
        "F1 First Floor" : "F1 First",
        "F2 Second Floor": "F2 Second",
        "F3 Third Floor" : "F3 Third",
    }

    lines = [
        "FIRE SAFETY DIGITAL TWIN",
        _truncate(f"Time: {timestamp}  Tick: {tick}"),
        "",
        f"Occupants: {total}",
        "",
        "FLOOR OCCUPANCY:",
    ]

    for fl in FLOOR_ORDER:
        cnt = by_floor.get(fl, 0)
        if cnt > 0:
            short = FLOOR_SHORT.get(fl, fl[:12])
            lines.append(f"  {short:12s}: {cnt}")

    lines.append("")

    # Compliance & Evacuation status
    over = [a for a in alerts if a.get("severity") == "OVER"]
    if evac_mode:
        lines.append("Status: CRITICAL - EVACUATING")
    elif over:
        lines.append(f"OVERCAPACITY ({len(over)}):")
        for a in over[:3]:
            lines.append(_truncate(
                f"  {a['label']}: {a['current']}/{a['max']}"))
    else:
        lines.append("Status: NORMAL")

    # Agent directive
    if agent_message:
        lines.append("")
        lines.append("AGENT DIRECTIVE:")
        
        directive_lines = [
            ln.strip() for ln in agent_message.split("\n")
            if ln.strip()
        ][:6]
        for dl in directive_lines:
            lines.append(_truncate(dl))

    text = (
        "\n".join(lines)
        .replace('"', "'")
        .replace("\\", "")
        .replace("'''", "'")
    )

    floor_collections_json = str(FLOOR_COLLECTIONS)
    skip_prefixes_json     = str(SKIP_PREFIXES)

    code = f"""
import bpy

# Update text board content
b = bpy.data.objects.get('FireSafetyBoard')
if b:
    b.data.body = '''{text}'''

# If Escalation / Evacuation mode is active, colour every floor
# collection RED — iterates the actual mesh objects inside each
# floor's IFC storey collection, same approach as every other
# floor-coloring function in this project. The previous version
# looked up a single object named after the floor, which never
# existed, so this was a silent no-op.
if {str(evac_mode)}:
    FLOOR_COLLECTIONS = {floor_collections_json}
    SKIP = {skip_prefixes_json}

    red_mat = bpy.data.materials.get('EvacRedMaterial')
    if not red_mat:
        red_mat = bpy.data.materials.new('EvacRedMaterial')
        red_mat.use_nodes = True
        bsdf = red_mat.node_tree.nodes.get('Principled BSDF')
        if bsdf:
            bsdf.inputs['Base Color'].default_value = (1.0, 0.0, 0.0, 1.0)
            if 'Emission Color' in bsdf.inputs:
                bsdf.inputs['Emission Color'].default_value = (1.0, 0.0, 0.0, 1.0)
            if 'Emission Strength' in bsdf.inputs:
                bsdf.inputs['Emission Strength'].default_value = 1.5

    coloured = 0
    for floor_name, col_name in FLOOR_COLLECTIONS.items():
        col = bpy.data.collections.get(col_name)
        if not col:
            continue
        for obj in col.objects:
            if obj.type != 'MESH':
                continue
            if any(obj.name.startswith(p) for p in SKIP):
                continue
            if obj.data:
                obj.data.materials.clear()
                obj.data.materials.append(red_mat)
                coloured += 1
    print(f'Evacuation mode: coloured {{coloured}} objects RED across all floors')

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
"""
    return send_to_blender(code)


if __name__ == "__main__":
    fake_snapshot = {
        "timestamp": "14:30:00",
        "tick"     : 5,
        "total_occ": 80,
        "evacuation_mode": True,
        "by_floor" : {
            "F0 Ground Floor": 22,
            "F1 First Floor" : 25,
            "F2 Second Floor": 19,
            "F3 Third Floor" : 14,
        },
        "alerts": [
            {"label": "1-16", "current": 4, "max": 3, "severity": "OVER"},
        ],
    }
    fake_message = (
        "CRITICAL: FULL BUILDING EVACUATION INITIATED.\n"
        "ALL OCCUPANTS EVACUATING TO ASSEMBLY POINT."
    )

    print("Board text preview (Evacuation Mode):")
    print("-" * _MAX_LINE)
    update_board(fake_snapshot, fake_message)