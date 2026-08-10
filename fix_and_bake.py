# fix_and_bake.py
# Clears conflicting materials, bakes occupancy management animation,
# updates corridor sign panels AFTER bake, then drives animation.
#
# All five scenarios first-class:
#
# TS-01: Single bedroom violation + board shows Lounge as false positive
# TS-02: blocked_exits=["EXIT-1"] — simulation routes around north exit
#        Two opposing signs: north RED, south GREEN
# TS-03: multi_violations=[{"room":"3-14","tick":4}] — multiple rooms
#        Four sign updates: F3 RED + F0/F1/F2 GREEN zone assessment
# TS-04: mobility_node="3-10" — wheelchair occupant avoids stairs
#        Two signs: corridor AMBER + stairwell RED + purple cone
# TS-05: Baseline fully suppressed — zero violations, zero sign updates

import sys, os, time, argparse, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bim.ifc_bridge import send_to_blender, test_connection
from bim.animation_baker import bake_animation
from bim.viewport_utils import frame_view_on_objects
from sensors.building_graph import BUILDING_GRAPH as G

# ── Scenario definitions ──────────────────────────────────────────────────────
SCENARIOS = {
    "default": {
        "description"     : "Ground floor bedroom 0-4 approaches capacity",
        "violation_tick"  : 5,
        "violation_room"  : "0-4",
        "ticks"           : 25,
        "seed"            : 42,
        "sign_lines"      : ["Room 0-4 is full",
                             "Please use", "Room 0-6"],
        "sign_colour"     : [0.90, 0.05, 0.05, 1.0],
        "adb_ref"         : "ADB Clause 2.43 — bedroom max occupancy",
        "extra_signs"     : {},
        "wheelchair"      : False,
        "board_override"  : None,
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : None,
    },

    "TS-01": {
        "description"     : "Single room congestion — F0 bedroom 0-4, lounge 0-20 filtered as false positive",
        "violation_tick"  : 4,
        "violation_room"  : "0-4",
        "ticks"           : 20,
        "seed"            : 1,
        "sign_lines"      : ["Room 0-4 is full",
                             "Please use", "Room 0-6"],
        "sign_colour"     : [0.90, 0.05, 0.05, 1.0],
        "adb_ref"         : "ADB Clause 2.43 — bedroom max occupancy care home",
        "extra_signs"     : {},
        "wheelchair"      : False,
        "board_override"  : (
            "FIRE SAFETY DIGITAL TWIN\n"
            "TS-01: Single Room Congestion\n\n"
            "AGENT DIRECTIVE:\n"
            "CYCLE: Tick 4 — VIOLATION\n"
            "ROOMS: 0-4 Bedroom (4/3) FAIL\n"
            "       0-20 Lounge (15/130) PASS\n"
            "       False positive filtered\n\n"
            "ADB: Clause 2.43 — bedroom max\n"
            "SIGNS: F0 CORRIDOR -> BLOCKED\n"
            "ACTION: Room 0-4 redirected\n"
            "ESCALATE: No"
        ),
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : None,
        "note"            : "0-20 Lounge (15/130) PASS — false positive shown on board",
    },

    "TS-02": {
        "description"     : "Exit obstruction — north exit blocked, simulation routes around it",
        "violation_tick"  : 5,
        "violation_room"  : "0-9",
        "ticks"           : 20,
        "seed"            : 2,
        "sign_lines"      : ["North Exit CLOSED",
                             "Use South Corridor",
                             "Follow green signs"],
        "sign_colour"     : [0.90, 0.05, 0.05, 1.0],
        "adb_ref"         : "ADB Table 2.1 — max travel distance 18m escape route",
        "extra_signs"     : {
            "SIGN_F0_CORRIDOR_S": {
                "lines" : ["South Exit OPEN",
                           "Primary route",
                           "Proceed now"],
                "colour": [0.05, 0.70, 0.15, 1.0],
            }
        },
        "wheelchair"      : False,
        "board_override"  : (
            "FIRE SAFETY DIGITAL TWIN\n"
            "TS-02: Exit Obstruction\n\n"
            "AGENT DIRECTIVE:\n"
            "CYCLE: Tick 5 — EXIT OBSTRUCTION\n"
            "ROOMS: 0-9 Bedroom assessed\n"
            "       EXIT-1 north unreachable\n\n"
            "ADB: Table 2.1 — 18m max travel\n"
            "SIGNS: NORTH -> BLOCKED\n"
            "       SOUTH -> ACTIVE\n"
            "ACTION: All occupants via south\n"
            "ESCALATE: No — south route clear"
        ),
        "blocked_exits"   : ["EXIT-1"],
        "multi_violations": [],
        "mobility_node"   : None,
        "note"            : "EXIT-1 blocked in simulation — occupants genuinely route to south exit",
    },

    "TS-03": {
        "description"     : "Multi-room congestion — F3 rooms 3-1 and 3-14 simultaneously overcrowded",
        "violation_tick"  : 4,
        "violation_room"  : "3-1",
        "ticks"           : 25,
        "seed"            : 3,
        "sign_lines"      : ["F3 rooms at capacity",
                             "Please use rooms",
                             "on Floor 2"],
        "sign_colour"     : [0.90, 0.05, 0.05, 1.0],
        "adb_ref"         : "ADB Clause 2.43 — bedroom occupancy | Section 2.33 — care home",
        "extra_signs"     : {
            "SIGN_F0_CORRIDOR_N": {
                "lines" : ["F0 Zone: CLEAR",
                           "Occupancy within",
                           "safe limits"],
                "colour": [0.05, 0.70, 0.15, 1.0],
            },
            "SIGN_F1_CORRIDOR": {
                "lines" : ["F1 Zone: CLEAR",
                           "Occupancy within",
                           "safe limits"],
                "colour": [0.05, 0.70, 0.15, 1.0],
            },
            "SIGN_F2_CORRIDOR": {
                "lines" : ["F2 Zone: CLEAR",
                           "Alternative rooms",
                           "available here"],
                "colour": [0.05, 0.70, 0.15, 1.0],
            },
        },
        "wheelchair"      : False,
        "board_override"  : (
            "FIRE SAFETY DIGITAL TWIN\n"
            "TS-03: Multi-Room Congestion\n\n"
            "AGENT DIRECTIVE:\n"
            "CYCLE: Tick 4 — VIOLATION\n"
            "ROOMS: 3-1 Bedroom (5/3) FAIL\n"
            "       3-14 Bedroom (4/3) FAIL\n"
            "       F0/F1/F2: 9 alerts PASS\n"
            "       (false positives filtered)\n\n"
            "ADB: Cl.2.43 + Sec.2.33\n"
            "SIGNS: F3 -> BLOCKED\n"
            "       F0/F1/F2 -> CLEAR\n"
            "ACTION: F3 redirected to F2\n"
            "ESCALATE: No"
        ),
        "blocked_exits"   : [],
        "multi_violations": [{"room": "3-14", "tick": 4}],
        "mobility_node"   : None,
        "note"            : "Rooms 3-1 + 3-14 both overcrowded simultaneously — modelled in simulation",
    },

    "TS-04": {
        "description"     : "Mobility constraint — wheelchair user on F3, stairwell avoided in simulation",
        "violation_tick"  : 2,
        "violation_room"  : "3-10",
        "ticks"           : 25,
        "seed"            : 7,
        "sign_lines"      : ["Wheelchair users",
                             "Proceed to Protected",
                             "Lobby — await staff"],
        "sign_colour"     : [0.90, 0.45, 0.00, 1.0],
        "adb_ref"         : "ADB Sections 3.5-3.6 — wheelchair refuge provisions",
        "extra_signs"     : {
            "SIGN_F3_STAIR": {
                "lines" : ["Stairwell B",
                           "Not accessible",
                           "Use lobby lift"],
                "colour": [0.90, 0.05, 0.05, 1.0],
            }
        },
        "wheelchair"      : True,
        "board_override"  : (
            "FIRE SAFETY DIGITAL TWIN\n"
            "TS-04: Mobility Constraint\n\n"
            "AGENT DIRECTIVE:\n"
            "CYCLE: Tick 2 — ACCESSIBILITY\n"
            "ROOMS: 3-10 Bedroom (4/3) FAIL\n"
            "       Wheelchair user present\n\n"
            "ADB: Sec 3.5-3.6 — refuge\n"
            "     Sec 2.33-2.36 — care home\n"
            "SIGNS: F3 CORRIDOR -> ALTERNATE\n"
            "       F3 STAIR    -> BLOCKED\n"
            "ACTION: Wheelchair refuge route\n"
            "ESCALATE: Yes — no compliant exit"
        ),
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : "3-10",
        "note"            : "Wheelchair marker avoids stairwells in simulation + purple cone visible",
    },

    "TS-05": {
        "description"     : "Baseline — normal operation, no violations (negative control)",
        "violation_tick"  : 999,
        "violation_room"  : None,
        "ticks"           : 15,
        "seed"            : 5,
        "sign_lines"      : [],
        "sign_colour"     : [0.90, 0.05, 0.05, 1.0],
        "adb_ref"         : "",
        "extra_signs"     : {},
        "wheelchair"      : False,
        "board_override"  : (
            "FIRE SAFETY DIGITAL TWIN\n"
            "TS-05: Baseline — Normal\n\n"
            "AGENT DIRECTIVE:\n"
            "CYCLE: Tick 0 — IDLE\n"
            "ROOMS: All rooms within limits\n"
            "       No violations detected\n\n"
            "ADB: Table B1 Purpose Group 2a\n"
            "     All rooms compliant\n"
            "SIGNS: None updated\n"
            "ACTION: No intervention required\n"
            "ESCALATE: No"
        ),
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : None,
        "note"            : "40 occupants — zero sign updates. Building stays green all 15 ticks.",
    },
}

FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}


def _is_baseline(sc):
    return sc["violation_tick"] >= 999 or sc["violation_room"] is None


# ── Blender helpers ───────────────────────────────────────────────────────────

def _clear_materials_and_set_viewport():
    send_to_blender("""
import bpy
removed = 0
for mat in list(bpy.data.materials):
    if mat.name.startswith('LIVE_') or mat.name.startswith('FS_FLOOR_'):
        bpy.data.materials.remove(mat)
        removed += 1
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.shading.type       = 'SOLID'
                space.shading.color_type = 'OBJECT'
                space.shading.show_shadows = False
        area.tag_redraw()
print(f'Cleared {removed} conflicting materials')
""")


def _reset_all_sign_panels_green():
    send_to_blender("""
import bpy
GREEN = [0.05, 0.70, 0.15, 1.0]
count = 0
for obj in bpy.data.objects:
    if obj.name.startswith('SignPanel_') and obj.data and obj.data.materials:
        mat  = obj.data.materials[0]
        node = mat.node_tree.nodes.get('Principled BSDF')
        if node:
            node.inputs['Base Color'].default_value     = GREEN
            node.inputs['Emission Color'].default_value  = GREEN
            node.inputs['Emission Strength'].default_value = 1.5
        count += 1
    if obj.name.startswith('SignText_'):
        obj.data.body = 'CLEAR\\nAll routes open'
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print(f'Reset {count} sign panels to GREEN')
""")


def _update_sign_panel(sign_id: str, lines: list, colour: list):
    """Updates one sign panel to given colour with short 3-line text."""
    if not lines:
        return
    body       = "\\n".join(lines)
    colour_str = str(colour)
    send_to_blender(f"""
import bpy
COL   = {colour_str}
panel = bpy.data.objects.get('SignPanel_{sign_id}')
if not panel:
    panel = bpy.data.objects.get('{sign_id}')
txt   = bpy.data.objects.get('SignText_{sign_id}')
if panel and panel.data and panel.data.materials:
    mat  = panel.data.materials[0]
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value     = COL
        node.inputs['Emission Color'].default_value  = COL
        node.inputs['Emission Strength'].default_value = 2.5
if txt:
    txt.data.body = '{body}'
    txt.data.size = 0.08
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print('Sign updated: {sign_id}')
""")


def _update_board_override(board_text: str):
    """Sets the FireSafetyBoard to the scenario-specific directive."""
    safe = board_text.replace("'", "\\'").replace('"', '\\"')
    send_to_blender(f"""
import bpy
board = bpy.data.objects.get('FireSafetyBoard')
if board and hasattr(board.data, 'body'):
    board.data.body = '{safe}'
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print('Board directive applied')
""")


def _mark_wheelchair_user(mobility_marker_id: int):
    """
    Colors the specific wheelchair cone purple using its exact name
    from the bake result (Occupant_NNN where NNN = mobility_marker_id).
    Parents the WC User label to the cone so it follows through animation.
    Finding by z position was unreliable at frame 0 — uses the exact ID.
    """
    PURPLE    = [0.60, 0.10, 0.80, 1.0]
    cone_name = f"Occupant_{mobility_marker_id:03d}"

    send_to_blender(f"""
import bpy
PURPLE    = {PURPLE}
cone_name = '{cone_name}'

for obj in list(bpy.data.objects):
    if obj.name == 'WheelchairLabel':
        bpy.data.objects.remove(obj, do_unlink=True)

wc = bpy.data.objects.get(cone_name)
if wc:
    wc.color = PURPLE
    if wc.data and wc.data.materials:
        mat  = wc.data.materials[0]
        bsdf = mat.node_tree.nodes.get('Principled BSDF')
        if bsdf:
            bsdf.inputs['Base Color'].default_value = PURPLE
            if 'Emission Color' in bsdf.inputs:
                bsdf.inputs['Emission Color'].default_value = PURPLE
            if 'Emission Strength' in bsdf.inputs:
                bsdf.inputs['Emission Strength'].default_value = 2.5

    # Add label at offset from cone origin then parent to cone
    # Label follows cone through every frame of animation
    bpy.ops.object.text_add(location=(0.0, 0.0, 2.5))
    txt = bpy.context.object
    txt.name           = 'WheelchairLabel'
    txt.data.body      = 'WC User'
    txt.data.size      = 0.35
    txt.data.align_x   = 'CENTER'
    txt.rotation_euler = (1.5708, 0, 0)
    txt.parent         = wc
    txt.parent_type    = 'OBJECT'

    tmat = bpy.data.materials.new('WCLabelMat')
    tmat.use_nodes = True
    tn = tmat.node_tree.nodes.get('Principled BSDF')
    if tn:
        tn.inputs['Base Color'].default_value = (1, 1, 1, 1)
        if 'Emission Color' in tn.inputs:
            tn.inputs['Emission Color'].default_value = (1, 1, 1, 1)
        if 'Emission Strength' in tn.inputs:
            tn.inputs['Emission Strength'].default_value = 3.0
    txt.data.materials.clear()
    txt.data.materials.append(tmat)

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()
    print(f'Wheelchair cone: {{cone_name}} -> purple, label parented (follows animation)')
else:
    print(f'Cone not found: {{cone_name}}')
""")


def _jump_to_frame(frame: int):
    send_to_blender(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print('Jumped to frame {frame}')
""")


def _drive_animation(total_frames: int, frames_per_tick: int,
                     frame_delay: float):
    print(f"\nDriving animation ({total_frames} frames at {frame_delay}s/frame)...")
    print("Press Ctrl+C to stop\n")
    try:
        for frame in range(0, total_frames + 1):
            send_to_blender(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
""")
            if frame % frames_per_tick == 0:
                tick = frame // frames_per_tick
                print(f"  Frame {frame:4d} | Tick {tick:2d}")
            time.sleep(frame_delay)
    except KeyboardInterrupt:
        print("\nStopped by user")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(scenario: str = "default",
         frames_per_tick: int = 24,
         frame_delay: float = 0.08):

    print("=" * 60)
    print(f"  FIX AND BAKE — {scenario}")
    print("=" * 60)

    print("\nChecking Blender connection...")
    if not test_connection():
        print("FAILED — open Blender, load IFC, start MCP server")
        return
    print("OK")

    sc       = SCENARIOS.get(scenario, SCENARIOS["default"])
    baseline = _is_baseline(sc)
    v_tick   = sc["violation_tick"]
    v_room   = sc["violation_room"]
    n_tick   = sc["ticks"]
    is_ts04  = sc.get("wheelchair", False)

    print(f"\nScenario      : {scenario}")
    print(f"Description   : {sc['description']}")
    print(f"Ticks         : {n_tick}")
    if baseline:
        print(f"Violation     : none (baseline)")
    else:
        print(f"Violation     : room {v_room} at tick {v_tick}")
        print(f"Primary sign  : {' / '.join(sc.get('sign_lines', []))}")
        if sc.get("blocked_exits"):
            print(f"Blocked exits : {sc['blocked_exits']}")
        if sc.get("multi_violations"):
            for mv in sc["multi_violations"]:
                print(f"Extra violation: {mv['room']} at tick {mv['tick']}")
        if sc.get("mobility_node"):
            print(f"Mobility node : {sc['mobility_node']} (wheelchair marker)")
        if sc.get("extra_signs"):
            for esid in sc["extra_signs"]:
                lines = sc["extra_signs"][esid].get("lines", [])
                print(f"Extra sign    : {esid} — {' / '.join(lines)}")
        if is_ts04:
            print(f"Wheelchair    : purple cone + WC User label on F3")
    if sc.get("note"):
        print(f"Note          : {sc['note']}")
    print(f"ADB ref       : {sc.get('adb_ref', '')} (board only)")
    print(f"FPS           : {frames_per_tick} frames/tick")

    print("\nClearing conflicting materials and setting viewport...")
    _clear_materials_and_set_viewport()

    print("Resetting corridor sign panels to GREEN...")
    _reset_all_sign_panels_green()

    print(f"\nBaking keyframes (30-90s)...")

    result = bake_animation(
        total_occupants  = 40 if baseline else 80,
        total_ticks      = n_tick,
        violation_tick   = 999 if baseline else v_tick,
        violation_room   = None if baseline else v_room,
        frames_per_tick  = frames_per_tick,
        seed             = sc["seed"],
        blocked_exits    = sc.get("blocked_exits", []),
        multi_violations = sc.get("multi_violations", []),
        mobility_node    = sc.get("mobility_node", None),
    )

    if result.get("status") != "ok":
        print(f"\nFAILED: {result}")
        return

    total   = result["total_frames"]
    v_frame = v_tick * frames_per_tick if not baseline else None

    frame_view_on_objects("Occupant_")

    print("\n" + "=" * 60)
    print("  BAKE COMPLETE")
    print(f"  Markers baked : {result.get('markers_baked', 80)}")
    print(f"  Floors baked  : {result.get('floors_baked', 4)}")
    print(f"  Total frames  : 0 -> {total} ({n_tick} ticks)")
    if baseline:
        print(f"  Violation     : NONE — baseline")
        print(f"  IFC Pset      : NOT written")
        print(f"  Sign updates  : 0")
    else:
        print(f"  Violation at  : frame {v_frame} tick {v_tick} room {v_room}")
        print(f"  ADB ref       : {sc['adb_ref']}")
    print("=" * 60)

    # Post-bake updates
    if baseline:
        print("\nBaseline — signs remain GREEN. No IFC writes.")
        if sc.get("board_override"):
            _update_board_override(sc["board_override"])
            print("  Board updated with IDLE directive")

    else:
        print(f"\nPost-bake updates...")

        # Primary sign — violation floor only
        v_node  = next((n for n, d in G.nodes(data=True)
                        if d["label"] == v_room), None)
        v_floor = G.nodes[v_node]["floor"] if v_node else None
        sign_id = FLOOR_SIGNS.get(v_floor)

        if sign_id and v_floor:
            colour = sc.get("sign_colour", [0.90, 0.05, 0.05, 1.0])
            _update_sign_panel(sign_id, sc.get("sign_lines", []), colour)
            print(f"  {sign_id} ({v_floor}) -> updated")
            print(f"  Text: {' / '.join(sc.get('sign_lines', []))}")
        else:
            print(f"  No sign mapped for {v_floor}")

        # Extra signs
        for esid, esc in sc.get("extra_signs", {}).items():
            _update_sign_panel(
                esid,
                esc.get("lines", []),
                esc.get("colour", [0.90, 0.05, 0.05, 1.0])
            )
            print(f"  {esid} -> updated ({' / '.join(esc.get('lines', []))})")

        # Wheelchair marker — TS-04 only
        if is_ts04:
            mobility_id = result.get("mobility_marker_id")
            if mobility_id is not None:
                print(f"  Marking wheelchair user cone "
                      f"Occupant_{mobility_id:03d} (purple + label follows)...")
                _mark_wheelchair_user(mobility_id)
            else:
                print("  WARNING: mobility_marker_id not in bake result")

        # Board directive
        if sc.get("board_override"):
            _update_board_override(sc["board_override"])
            print(f"  Board updated with {scenario} directive")

        # Jump to violation frame
        print(f"\n  Jumping to violation frame {v_frame}...")
        _jump_to_frame(v_frame)
        time.sleep(1.5)

        total_signs = 1 + len(sc.get("extra_signs", {}))
        print(f"  {total_signs} sign(s) updated | Floor RED | Board shows directive")

    _drive_animation(total, frames_per_tick, frame_delay)

    print("\nAnimation complete.")
    if v_frame:
        print(f"Tip — jump to violation frame:")
        print(f"  python fix_and_bake.py --jump {v_frame}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix materials, bake animation, update signs, drive")
    parser.add_argument("--scenario", default="default",
                        choices=list(SCENARIOS.keys()))
    parser.add_argument("--fps",   type=int,   default=24)
    parser.add_argument("--speed", type=float, default=0.08)
    parser.add_argument("--jump",  type=int,   default=None)
    args = parser.parse_args()

    if args.jump is not None:
        print("Checking Blender connection...")
        if test_connection():
            print(f"Jumping to frame {args.jump}...")
            _jump_to_frame(args.jump)
            print(f"At frame {args.jump}")
        else:
            print("FAILED")
    else:
        main(args.scenario, args.fps, args.speed)