# test_seed_violations.py (Fixed for isolated execution)
import random
import sys
import numpy as np

sys.path.insert(0, ".")
from sensors.sensor_sim import (
    get_sensor_snapshot,
    initialise_occupants,
    move_occupants,
)

print("Finding seeds with isolated early genuine violations...\n")

for seed in range(1, 50):
    # Reset both random modules to isolate each seed run
    random.seed(seed)
    np.random.seed(seed)

    initialise_occupants(80, seed=seed)
    for tick in range(1, 15):
        move_occupants()
        snap = get_sensor_snapshot()
        overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
        if overs:
            rooms = [f"{a['label']}({a['current']}/{a['max']})" for a in overs]
            print(f"Seed {seed:2d}: OVER at tick {tick:2d} — {rooms}")
            break