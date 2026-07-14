# play_animation.py
# Starts the Blender animation playback via the socket connection.
# No spacebar needed — run this from PowerShell.

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bim.ifc_bridge import send_to_blender

print("Starting animation playback in Blender...")

result = send_to_blender("""
import bpy

# Reset to frame 0 first
bpy.context.scene.frame_set(0)

# Start playback — bypasses keyboard shortcut
bpy.ops.screen.animation_play()

print("Animation playing")
""")

print(result)
print("Animation started — watch Blender")
