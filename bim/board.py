# bim/board.py
# Building manager's dashboard text panel in Blender.

from bim.ifc_bridge import send_to_blender

_MAX_LINE = 45   # hard character limit per line


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
    Turns all floor objects RED if evacuation_mode is active.
    """
    by_floor   = snapshot.get("by_floor", {})
    alerts     = snapshot.get("alerts", [])
    total      = snapshot.get("total_occ", 0)
    tick       = snapshot.get("tick", 0)
    timestamp  = snapshot.get("timestamp", "")
    evac_mode  = snapshot.get("evacuation_mode", False)

    # Floor display order — F0 first, F3 last
    FLOOR_ORDER = [
        "F0 Ground Floor",
        "F1 First Floor",
        "F2 Second Floor",
        "F3 Third Floor",
    ]

    # Short floor labels that fit within line limit
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

    # Agent directive — strictly truncated
    if agent_message:
        lines.append("")
        lines.append("AGENT DIRECTIVE:")
        # Take first 6 non-empty lines, truncate each
        directive_lines = [
            ln.strip() for ln in agent_message.split("\n")
            if ln.strip()
        ][:6]
        for dl in directive_lines:
            lines.append(_truncate(dl))

    # Join and sanitise for embedding in Python f-string
    text = "\n".join(lines).replace('"', "'").replace("\\", "")

    code = f"""
import bpy

# Update text board content
b = bpy.data.objects.get('FireSafetyBoard')
if b:
    b.data.body = '''{text}'''

# If Escalation / Evacuation mode is active, set all floor objects to RED material
if {str(evac_mode)}:
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

    floor_names = ['F0 Ground Floor', 'F1 First Floor', 'F2 Second Floor', 'F3 Third Floor']
    for fl_name in floor_names:
        fl_obj = bpy.data.objects.get(fl_name)
        if fl_obj:
            if fl_obj.data.materials:
                fl_obj.data.materials[0] = red_mat
            else:
                fl_obj.data.materials.append(red_mat)

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