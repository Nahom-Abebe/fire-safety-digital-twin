# fix_and_bake.py
# Clears conflicting materials, sets correct viewport mode,
# bakes the occupancy management animation, updates corridor
# sign panels AFTER the bake completes, then drives the animation.
#
# Run: python fix_and_bake.py
# Run: python fix_and_bake.py --scenario TS-01 --fps 12 --speed 0.08

import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bim.ifc_bridge import send_to_blender, test_connection
from bim.animation_baker import bake_animation
from bim.viewport_utils import frame_view_on_objects
from sensors.building_graph import BUILDING_GRAPH as G

SCENARIOS = {
    "default": {
        "description"   : "Ground floor room 0-4 approaches capacity",
        "violation_tick": 5,
        "violation_room": "0-4",
        "ticks"         : 25,
        "seed"          : 42,
    },
    "TS-01": {
        "description"   : "Single room congestion — F0 room 0-20 (Lounge area)",
        "violation_tick": 4,
        "violation_room": "0-20",
        "ticks"         : 20,
        "seed"          : 1,
    },
    "TS-02": {
        "description"   : "Corridor congestion — F0 main corridor 0-A",
        "violation_tick": 5,
        "violation_room": "0-A",
        "ticks"         : 20,
        "seed"          : 2,
    },
    "TS-03": {
        "description"   : "First floor congestion — F1 corridor 1-A",
        "violation_tick": 8,
        "violation_room": "1-A",
        "ticks"         : 25,
        "seed"          : 3,
    },
    "TS-04": {
        "description"   : "Top floor congestion — F3 room 3-1 (mobility constraint)",
        "violation_tick": 6,
        "violation_room": "3-1",
        "ticks"         : 25,
        "seed"          : 4,
    },
    "TS-05": {
        "description"   : "Baseline — no violation (normal operation)",
        "violation_tick": 999,
        "violation_room": "0-4",
        "ticks"         : 15,
        "seed"          : 5,
    },
}

# Which corridor sign serves each floor
FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clear_materials_and_set_viewport():
    """
    Removes conflicting LIVE_ and FS_FLOOR_ materials from previous
    live_agent_runner sessions. Sets viewport to SOLID + OBJECT COLOR
    so obj.color keyframes are visible during the baked animation.
    """
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
print('Viewport set to SOLID + OBJECT COLOR')
""")


def _reset_all_sign_panels_green():
    """Resets all corridor sign panels to green before baking."""
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
        floor = obj.name.replace('SignText_SIGN_', '').replace('_CORRIDOR_N','')\\
                        .replace('_CORRIDOR','')
        obj.data.body = f'Status: CLEAR\\nAll routes open'
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print(f'Reset {count} sign panels to GREEN')
""")


def _update_sign_panel_red(sign_id: str, room: str, floor_name: str):
    """
    Turns a specific corridor sign panel RED after the bake completes.
    Shows a simple occupant-facing message — no ADB references.
    Also moves 5 cones to the assembly point to show redirection.
    """
    message = f"Room {room} is full\\nPlease use an alternative area"

    send_to_blender(f"""
import bpy
RED = [0.90, 0.05, 0.05, 1.0]

panel = bpy.data.objects.get('SignPanel_{sign_id}')
txt   = bpy.data.objects.get('SignText_{sign_id}')

if panel and panel.data.materials:
    mat  = panel.data.materials[0]
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value     = RED
        node.inputs['Emission Color'].default_value  = RED
        node.inputs['Emission Strength'].default_value = 2.5

if txt:
    txt.data.body = 'BLOCKED: {message}'

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()

print('Sign panel updated: {sign_id} -> RED')
""")

    # Move some cones to the assembly point to show redirection
    try:
        from bim.assembly_point import move_cones_to_assembly
        import random
        rng          = random.Random(42)
        redirect_ids = rng.sample(range(80), 5)
        move_cones_to_assembly(redirect_ids)
        print(f"  5 occupants redirected to assembly point")
    except Exception as e:
        print(f"  Assembly point redirect skipped: {e}")


def _jump_to_frame(frame: int):
    """
    Jumps Blender to a specific frame and redraws the viewport.
    Used to show the violation state before driving the animation.
    """
    send_to_blender(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print(f'Jumped to frame {frame}')
""")


def _drive_animation(total_frames: int, frames_per_tick: int,
                     frame_delay: float):
    """Drives the animation frame by frame from Python."""
    print(f"\nDriving animation ({total_frames} frames "
          f"at {frame_delay}s/frame)...")
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

    # ── Connection ────────────────────────────────────────────────────────
    print("\nChecking Blender connection...")
    if not test_connection():
        print("FAILED — open Blender, load IFC, start MCP server (N-panel)")
        return
    print("OK")

    sc = SCENARIOS.get(scenario, SCENARIOS["default"])
    v_tick = sc["violation_tick"]
    v_room = sc["violation_room"]
    n_ticks = sc["ticks"]

    print(f"\nScenario    : {scenario}")
    print(f"Description : {sc['description']}")
    print(f"Ticks       : {n_ticks}")
    print(f"Violation   : room {v_room} at tick "
          f"{'none (baseline)' if v_tick >= 999 else v_tick}")
    print(f"FPS         : {frames_per_tick} frames/tick")

    # ── Step 1: Clear conflicting materials ───────────────────────────────
    print("\nClearing conflicting materials and setting viewport...")
    _clear_materials_and_set_viewport()

    # ── Step 2: Reset all sign panels to green ────────────────────────────
    print("Resetting corridor sign panels to GREEN...")
    _reset_all_sign_panels_green()

    # ── Step 3: Bake animation ────────────────────────────────────────────
    print(f"\nBaking keyframes (30–90s)...")

    result = bake_animation(
        total_occupants = 80,
        total_ticks     = n_ticks,
        violation_tick  = v_tick,
        violation_room  = v_room,
        frames_per_tick = frames_per_tick,
        seed            = sc["seed"],
    )

    if result.get("status") != "ok":
        print(f"\nFAILED: {result}")
        return

    total   = result["total_frames"]
    v_frame = v_tick * frames_per_tick if v_tick < 999 else None

    frame_view_on_objects("Occupant_")

    print("\n" + "=" * 60)
    print("  BAKE COMPLETE")
    print(f"  Markers baked    : {result.get('markers_baked', 80)}")
    print(f"  Floors baked     : {result.get('floors_baked', 4)}")
    print(f"  Total frames     : 0 → {total} ({n_ticks} ticks)")
    if v_frame:
        print(f"  Violation frame  : {v_frame} (tick {v_tick})")
    print("=" * 60)

    # ── Step 4: Update corridor sign AFTER bake ───────────────────────────
    # Done AFTER bake so Blender is free to receive the Blender command.
    # Sign update before bake gets dropped because Blender is busy.
    if v_frame and v_tick < 999:
        print(f"\nUpdating corridor sign panels after bake...")

        # Find which floor the violation room is on
        v_node  = next((n for n, d in G.nodes(data=True)
                        if d["label"] == v_room), None)
        v_floor = G.nodes[v_node]["floor"] if v_node else None
        sign_id = FLOOR_SIGNS.get(v_floor)

        if sign_id and v_floor:
            _update_sign_panel_red(sign_id, v_room, v_floor)
            print(f"  {sign_id} → RED")
            print(f"  Message: Room {v_room} is full — "
                  f"please use an alternative area")
        else:
            print(f"  No corridor sign mapped for {v_floor}")

        # Jump to violation frame so the red state is immediately visible
        print(f"\nJumping to violation frame {v_frame}...")
        _jump_to_frame(v_frame)
        time.sleep(1.5)   # pause so it is visible before driving starts
        print("  Floor is RED, sign panel is RED")

    elif v_tick >= 999:
        print("\nBaseline scenario — no violation, all signs stay GREEN")

    # ── Step 5: Drive animation ───────────────────────────────────────────
    _drive_animation(total, frames_per_tick, frame_delay)

    print("\nAnimation complete.")
    if v_frame:
        print(f"Tip: Jump to frame {v_frame} to see the violation state again:")
        print(f"  python fix_and_bake.py --jump {v_frame}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix materials, bake animation, update signs, drive")

    parser.add_argument("--scenario", default="default",
                        choices=list(SCENARIOS.keys()))
    parser.add_argument("--fps", type=int, default=24,
                        help="Frames per tick (12=slow, 24=default)")
    parser.add_argument("--speed", type=float, default=0.08,
                        help="Seconds per frame during drive (0.08=default)")
    parser.add_argument("--jump", type=int, default=None,
                        help="Jump to frame without rebaking")

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
        main(
            scenario        = args.scenario,
            frames_per_tick = args.fps,
            frame_delay     = args.speed,
        )