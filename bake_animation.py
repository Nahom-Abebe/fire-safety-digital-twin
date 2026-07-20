# bake_animation.py
# Bakes smooth keyframed occupancy management animation into Blender.
# This is the VISUAL DEMO tool — no Claude agent, no API cost.
#
# Scenarios show localised occupancy violations and redirection:
#   - Occupants move naturally (random walk)
#   - When a room exceeds ADB capacity, it turns RED
#   - Corridor sign updates with ADB Cl.2.43 citation
#   - Occupants naturally disperse from overcrowded room
#   - No evacuation, no fire alarm
#
# Run: python bake_animation.py
# Run: python bake_animation.py --scenario TS-01
# Run: python bake_animation.py --scenario TS-01 --drive --fps 12 --speed 0.08
# Run: python bake_animation.py --jump 60    (jump to violation frame)
# Run: python bake_animation.py --play       (replay without rebaking)

import sys, os, argparse, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bim.animation_baker import bake_animation
from bim.viewport_utils import frame_view_on_objects
from bim.ifc_bridge import test_connection, send_to_blender

# ── Synchronized Scenario Definitions ─────────────────────────────────────────
# Aligned perfectly with tests/test_scenarios.py and fix_and_bake.py

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
        "violation_tick": 999,   # never triggers within 15 ticks
        "violation_room": "0-4",
        "ticks"         : 15,
        "seed"          : 5,
    },
}


# ── Viewport Shading Guard ───────────────────────────────────────────────────

def ensure_material_shading():
    """Forces Blender viewport into Material Preview to ensure colors/emissions render."""
    send_to_blender("""
import bpy
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.shading.type = 'MATERIAL'
        area.tag_redraw()
""")


# ── Playback Helpers ──────────────────────────────────────────────────────────

def play_animation() -> dict:
    """Start Blender playback via socket — no spacebar needed."""
    ensure_material_shading()
    return send_to_blender("""
import bpy
bpy.context.scene.frame_set(0)
bpy.ops.screen.animation_play()
print('Playback started')
""")


def drive_animation(total_frames: int, frames_per_tick: int, frame_delay: float = 0.08):
    """
    Python-driven frame loop — most reliable playback method.
    Steps Blender through every frame at a controlled rate.
    """
    ensure_material_shading()
    print(f"\nDriving animation ({total_frames} frames at {frame_delay}s/frame)...")
    print("Press Ctrl+C to stop\n")
    try:
        for frame in range(0, total_frames + 1):
            send_to_blender(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()
""")
            if frame % frames_per_tick == 0:
                tick = frame // frames_per_tick
                print(f"  Frame {frame:4d} | Tick {tick:2d}")
            time.sleep(frame_delay)
    except KeyboardInterrupt:
        print("\nStopped by user")


def jump_to_frame(frame: int):
    """Jump Blender to a specific frame without rebaking."""
    ensure_material_shading()
    print(f"Jumping to frame {frame}...")
    send_to_blender(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()
print('At frame {frame}')
""")
    print(f"Blender is now at frame {frame}")


# ── Main Bake Function ────────────────────────────────────────────────────────

def main(scenario: str = "default",
         frames_per_tick: int = 24,
         auto_drive: bool = False,
         frame_delay: float = 0.08):

    print("=" * 60)
    print(f"  BAKING ANIMATION — {scenario}")
    print("=" * 60)

    # ── Connection check ───────────────────────────────────────────────
    print("\nChecking Blender connection...")
    if not test_connection():
        print("FAILED — open Blender, load IFC, start MCP server (N-panel)")
        return
    print("OK")

    # Force view setup before execution
    ensure_material_shading()

    # ── Load scenario ──────────────────────────────────────────────────
    sc = SCENARIOS.get(scenario, SCENARIOS["default"])
    v_tick  = sc["violation_tick"]
    v_room  = sc["violation_room"]
    n_ticks = sc["ticks"]

    print(f"\nScenario    : {scenario}")
    print(f"Description : {sc['description']}")
    print(f"Ticks       : {n_ticks}")
    print(f"Violation   : room {v_room} at tick "
          f"{'none (baseline)' if v_tick >= 999 else v_tick}")
    print(f"FPS         : {frames_per_tick} frames/tick")
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

    total           = result["total_frames"]
    violation_frame = v_tick * frames_per_tick if v_tick < 999 else None

    # ── Frame viewport on markers ──────────────────────────────────────
    frame_view_on_objects("Occupant_")

    # ── Summary ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  BAKE COMPLETE")
    print(f"  Markers baked    : {result.get('markers_baked', 80)}")
    print(f"  Floors baked     : {result.get('floors_baked', 4)}")
    print(f"  Total frames     : 0 → {total} ({n_ticks} ticks)")
    if violation_frame:
        print(f"  Violation frame  : {violation_frame} (tick {v_tick})")
        print(f"  IFC Pset         : ComplianceStatus=FAIL written for {v_room}")
        print(f"  Sign updated     : ADB Cl.2.43 citation")
    else:
        print(f"  No violation     : baseline scenario (normal operation)")
    print("=" * 60)

    # ── Playback ───────────────────────────────────────────────────────
    if auto_drive:
        drive_animation(total, frames_per_tick, frame_delay)
    else:
        print("\nStarting Blender playback...")
        play_result = play_animation()
        print(f"  {play_result}")
        print()
        print("  If Blender is NOT playing, use:")
        print("  python bake_animation.py --scenario " + scenario + " --drive")
        if violation_frame:
            print(f"  python bake_animation.py --jump {violation_frame}")


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Bake and play occupancy management animation in Blender. "
            "Shows localised room redirection — not fire evacuation."
        )
    )

    parser.add_argument(
        "--scenario", default="default",
        choices=list(SCENARIOS.keys()),
        help="Scenario to bake (default, TS-01 to TS-05)")

    parser.add_argument(
        "--fps", type=int, default=24,
        help="Frames per tick — lower = slower animation "
             "(12=half speed, 24=default, 6=very slow)")

    parser.add_argument(
        "--drive", action="store_true",
        help="Python-driven playback — most reliable, "
             "bypasses Bonsai spacebar interception")

    parser.add_argument(
        "--speed", type=float, default=0.08,
        help="Seconds between frames in --drive mode "
             "(0.08=default, 0.15=slower, 0.04=faster)")

    parser.add_argument(
        "--jump", type=int, default=None,
        help="Jump to specific frame without rebaking")

    parser.add_argument(
        "--play", action="store_true",
        help="Start playback without rebaking "
             "(use after a bake has already run)")

    args = parser.parse_args()

    # ── Jump-only mode ─────────────────────────────────────────────────
    if args.jump is not None:
        print("Checking Blender connection...")
        if test_connection():
            jump_to_frame(args.jump)
        else:
            print("FAILED — start Bonsai MCP server in N-panel")

    # ── Play-only mode ─────────────────────────────────────────────────
    elif args.play:
        print("Checking Blender connection...")
        if test_connection():
            print("Starting playback...")
            print(play_animation())
        else:
            print("FAILED — start Bonsai MCP server in N-panel")

    # ── Full bake + playback ───────────────────────────────────────────
    else:
        main(
            scenario        = args.scenario,
            frames_per_tick = args.fps,
            auto_drive      = args.drive,
            frame_delay     = args.speed,
        )