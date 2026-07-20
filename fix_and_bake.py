# fix_and_bake.py
# Clears conflicting materials, sets correct viewport mode,
# then runs the bake. Run this instead of bake_animation.py directly.

import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender, test_connection
from bim.ifc_bridge import send_to_blender as s2b
from bim.animation_baker import bake_animation
from bim.viewport_utils import frame_view_on_objects

print("Checking connection...")
if not test_connection():
    print("FAILED"); exit()
print("OK")

print("Configuring viewport for Material Preview Shading...")
send_to_blender("""
import bpy

# Switch viewports to MATERIAL preview mode to support node-based emission and color changes
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.shading.type = 'MATERIAL'
                # Ensure transparent overlays are handled beautifully
                if hasattr(space.shading, "type"):
                    space.shading.use_composite = True
        area.tag_redraw()

print('Viewport successfully set to MATERIAL preview mode')
""")

print("Done — running bake now...")

parser = argparse.ArgumentParser()
parser.add_argument("--scenario", default="TS-01",
                    choices=["TS-01","TS-02","TS-03","TS-04","TS-05","default"])
parser.add_argument("--fps", type=int, default=12)
parser.add_argument("--speed", type=float, default=0.08)
args = parser.parse_args()

SCENARIOS = {
    "default": {"violation_tick": 5,  "violation_room": "0-4",  "ticks": 25, "seed": 42},
    "TS-01"  : {"violation_tick": 4,  "violation_room": "0-20", "ticks": 20, "seed": 1},
    "TS-02"  : {"violation_tick": 5,  "violation_room": "0-A",  "ticks": 20, "seed": 2},
    "TS-03"  : {"violation_tick": 8,  "violation_room": "1-A",  "ticks": 25, "seed": 3},
    "TS-04"  : {"violation_tick": 6,  "violation_room": "3-1",  "ticks": 25, "seed": 4},
    "TS-05"  : {"violation_tick": 999,"violation_room": "0-4",  "ticks": 15, "seed": 5},
}

sc = SCENARIOS[args.scenario]

result = bake_animation(
    total_occupants = 80,
    total_ticks     = sc["ticks"],
    violation_tick  = sc["violation_tick"],
    violation_room  = sc["violation_room"],
    frames_per_tick = args.fps,
    seed            = sc["seed"],
)

if result.get("status") != "ok":
    print(f"FAILED: {result}"); exit()

frame_view_on_objects("Occupant_")
total = result["total_frames"]
v_frame = sc["violation_tick"] * args.fps

print(f"\nBake complete — {total} frames")
print(f"Violation triggers at frame {v_frame} (tick {sc['violation_tick']})")
print("\nDriving simulation timeline...")

try:
    for frame in range(0, total + 1):
        s2b(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': 
        area.tag_redraw()
""")
        if frame % args.fps == 0:
            print(f"   Frame {frame:4d} | Tick {frame//args.fps:2d}")
        time.sleep(args.speed)
except KeyboardInterrupt:
    print("Playback stopped by user.")