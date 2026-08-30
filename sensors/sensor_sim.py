# sensors/sensor_sim.py
# Probabilistic graph-based occupancy simulation (Peter Lawrence's model).
# Movement probability depends on:
#   1. Room attractiveness (node property, set by agent)
#   2. Sign status (BLOCKED sign reduces edge probability to near zero)
#   3. Room capacity headroom (agents avoid rooms at/over capacity)
#   4. Global Evacuation Mode (directed movement to emergency exits)

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from datetime import datetime
import networkx as nx
from sensors.building_graph import (
    BUILDING_GRAPH, EXIT_IDS, STAIR_IDS,
    get_max_occupancy, get_exit_path, is_occupancy_alert_relevant
)

G = BUILDING_GRAPH

# Room nodes only (used for initial placement)
ROOM_NODES = [n for n, d in G.nodes(data=True) if d["node_type"] == "room"]

# Simulation state 
occupancy       = {node: 0 for node in G.nodes}
active_event    = None
tick_count      = 0
event_log       = []
EVACUATION_MODE = False

# Sign status fed back from bim/signage.py each tick
# {graph_label: "BLOCKED" | "ACTIVE" | "ALTERNATE"}
sign_status     = {}


#  Initialize 

def initialise_occupants(total: int = 80, seed: int = None):
    global occupancy, active_event, tick_count, event_log, sign_status, EVACUATION_MODE
    occupancy       = {node: 0 for node in G.nodes}
    active_event    = None
    tick_count      = 0
    event_log       = []
    sign_status     = {}
    EVACUATION_MODE = False
    
    rng = random.Random(seed) if seed is not None else random
    for _ in range(total):
        occupancy[rng.choice(ROOM_NODES)] += 1
    print(f"Initialised {total} occupants across {len(ROOM_NODES)} rooms")


# Evacuation Controls 

def trigger_global_evacuation():
    """Triggers global evacuation mode, routing all occupants to nearest exits."""
    global EVACUATION_MODE
    EVACUATION_MODE = True
    print(f"EVACUATION: Global building evacuation triggered at tick {tick_count}!")


def clear_evacuation():
    """Clears global evacuation mode and returns to normal occupant wandering."""
    global EVACUATION_MODE
    EVACUATION_MODE = False
    print(f"EVACUATION: Evacuation cleared at tick {tick_count}.")


# Signage feedback 

def update_sign_status(label: str, status: str):
    """
    Called by bim/signage.py after updating a sign Pset.
    Updates the simulation's edge weights for the next tick.
    BLOCKED → occupants strongly avoid moving toward this node.
    """
    sign_status[label] = status
    for node, data in G.nodes(data=True):
        if data["label"] == label:
            G.nodes[node]["sign_blocked"] = (status == "BLOCKED")
            break


def set_room_attractiveness(label: str, value: float):
    """
    Agent can raise (>1.0) or lower (<1.0) attractiveness of a room.
    0.0 = completely unattractive (but not physically blocked).
    """
    for node, data in G.nodes(data=True):
        if data["label"] == label:
            G.nodes[node]["attractiveness"] = max(0.0, value)
            break


# Movement 

def _edge_probability(from_node: int, to_node: int) -> float:
    """
    Calculates the probability weight for moving from_node → to_node.
    Factors: attractiveness, sign_blocked, capacity headroom, edge weight.
    """
    data    = G.nodes[to_node]
    max_occ = get_max_occupancy(to_node)
    current = occupancy.get(to_node, 0)

    if data["node_type"] == "room" and current >= max_occ:
        return 0.001

    if data.get("sign_blocked", False):
        return 0.01

    # Base weight: attractiveness / edge travel cost
    edge_weight    = G.edges[from_node, to_node].get("weight", 1)
    attractiveness = data.get("attractiveness", 1.0)
    headroom       = 1.0 - (current / max_occ) if max_occ > 0 else 1.0
    headroom       = max(0.01, headroom)

    return attractiveness * headroom / edge_weight


def move_occupants():
    """
    One simulation tick — probabilistic movement per Peter's model.
    If EVACUATION_MODE is active, all occupants move along shortest paths to EXIT_IDS.
    """
    global occupancy, tick_count
    new_state = {node: 0 for node in G.nodes}

    if EVACUATION_MODE:
        for node, count in occupancy.items():
            if count == 0:
                continue

            if node in EXIT_IDS:
                new_state[node] += count
                continue

            path_info = get_exit_path(node)
            path = path_info.get("path_nodes", [])

            if len(path) > 1:
                next_node = path[1]
            else:
                next_node = node

            new_state[next_node] += count
    else:
        
        for node in G.nodes:
            count = occupancy[node]
            if count == 0:
                continue

            for _ in range(count):
                neighbours = list(G.neighbors(node))
                candidates = [node] + neighbours

                stay_weight = G.nodes[node].get("attractiveness", 1.0) * 0.5
                weights     = [stay_weight]
                for nb in neighbours:
                    weights.append(_edge_probability(node, nb))

                total = sum(weights)
                if total <= 0:
                    next_node = node
                else:
                    norm_weights = [w / total for w in weights]
                    next_node    = random.choices(candidates,
                                                  weights=norm_weights)[0]

                new_state[next_node] += 1

    occupancy  = new_state
    tick_count += 1


# Event handling 

def trigger_event(node_label: str,
                  event_type: str = "overcapacity") -> dict:
    global active_event
    node_id = next(
        (n for n, d in G.nodes(data=True) if d["label"] == node_label),
        None)
    if node_id is None:
        return {"error": f"'{node_label}' not in graph"}
    active_event = {
        "type"        : event_type,
        "node_id"     : node_id,
        "node_label"  : node_label,
        "floor"       : G.nodes[node_id]["floor"],
        "triggered_at": datetime.now().strftime("%H:%M:%S"),
    }
    event_log.append({"tick": tick_count, "event": str(active_event)})
    print(f"EVENT: {event_type} at {node_label} | tick {tick_count}")
    return active_event


def clear_event():
    global active_event
    active_event = None


# Snapshot 

def get_sensor_snapshot() -> dict:
    total    = sum(occupancy.values())
    by_floor = {}
    for node, count in occupancy.items():
        fl = G.nodes[node]["floor"]
        by_floor[fl] = by_floor.get(fl, 0) + count

    alerts = []
    for node, count in occupancy.items():
        ntype = G.nodes[node]["node_type"]
        if ntype != "room" or count == 0:
            continue
    
        if not is_occupancy_alert_relevant(node):
            continue
        max_occ = get_max_occupancy(node)
        if count > max_occ:
            alerts.append({
                "node_id" : node,
                "label"   : G.nodes[node]["label"],
                "floor"   : G.nodes[node]["floor"],
                "current" : count,
                "max"     : max_occ,
                "ratio"   : round(count / max_occ, 2),
                "severity": "OVER",
            })
        elif count == max_occ:
            alerts.append({
                "node_id" : node,
                "label"   : G.nodes[node]["label"],
                "floor"   : G.nodes[node]["floor"],
                "current" : count,
                "max"     : max_occ,
                "ratio"   : 1.0,
                "severity": "WARNING",
            })
        elif count >= max_occ * 0.8:
            alerts.append({
                "node_id" : node,
                "label"   : G.nodes[node]["label"],
                "floor"   : G.nodes[node]["floor"],
                "current" : count,
                "max"     : max_occ,
                "ratio"   : round(count / max_occ, 2),
                "severity": "WARNING",
            })

    return {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "tick"     : tick_count,
        "total_occ": total,
        "at_exits" : sum(occupancy.get(e, 0) for e in EXIT_IDS),
        "evacuation_mode": EVACUATION_MODE,
        "occupancy": {G.nodes[n]["label"]: c
                      for n, c in occupancy.items() if c > 0},
        "by_floor" : by_floor,
        "event"    : active_event,
        "alerts"   : alerts,
    }


def get_room_status(node_label: str) -> dict:
    for node, data in G.nodes(data=True):
        if data["label"] == node_label:
            count   = occupancy.get(node, 0)
            max_occ = get_max_occupancy(node)
            path    = get_exit_path(node)
            ratio   = round(count / max_occ, 2) if max_occ else 0
            if count > max_occ:
                severity = "OVER"
            elif count == max_occ:
                severity = "WARNING"
            elif count >= max_occ * 0.8:
                severity = "WARNING"
            else:
                severity = "OK"
            return {
                "label"        : node_label,
                "floor"        : data["floor"],
                "area_m2"      : data["area_m2"],
                "current_occ"  : count,
                "max_occ"      : max_occ,
                "ratio"        : ratio,
                "severity"     : severity,
                "attractiveness": data.get("attractiveness", 1.0),
                "sign_blocked" : data.get("sign_blocked", False),
                "exit_path"    : path.get("path_labels", []),
                "travel_weight": path.get("path_weight", 0),
            }
    return {"error": f"'{node_label}' not in graph"}


if __name__ == "__main__":
    initialise_occupants(80, seed=42)
    snap = get_sensor_snapshot()
    print(f"\nInitial state: {snap['total_occ']} occupants")
    for fl, cnt in snap["by_floor"].items():
        print(f"  {fl:25s}: {cnt}")

    print(f"\nRunning 5 ticks...")
    for i in range(5):
        move_occupants()
        snap  = get_sensor_snapshot()
        overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
        warns = [a for a in snap["alerts"] if a["severity"] == "WARNING"]
        print(f"  Tick {i+1:02d}: total={snap['total_occ']} "
              f"over={len(overs)} "
              f"warnings={len(warns)}")

    print("\nTesting Evacuation Mode...")
    trigger_global_evacuation()
    for i in range(5):
        move_occupants()
        snap = get_sensor_snapshot()
        print(f"  Evac Tick {i+1:02d}: occupants at exit={snap['at_exits']}/{snap['total_occ']}")

    print("\n✅ sensor_sim verified")