# test_seed_violations.py
import sys; sys.path.insert(0,'.')
from sensors.sensor_sim import initialise_occupants, move_occupants, get_sensor_snapshot

print("Finding seeds with early genuine violations...\n")
for seed in range(1, 50):
    initialise_occupants(80, seed=seed)
    for tick in range(1, 8):
        move_occupants()
        snap = get_sensor_snapshot()
        overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
        if overs:
            rooms = [f"{a['label']}({a['current']}/{a['max']})" for a in overs]
            print(f"Seed {seed:2d}: OVER at tick {tick} — {rooms}")
            break
    else:
        pass