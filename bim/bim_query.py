# bim/bim_query.py
import json, os

_path = os.path.join(os.path.dirname(__file__), "global_ids_v2.json")
with open(_path, encoding="utf-8") as f:
    _data = json.load(f)

SPACES = _data["spaces"]
SIGNS  = _data["signs"]
ALARMS = _data["alarms"]
FLOORS = _data["floors"]


def get_space_by_name(long_name: str) -> dict:
    for gid, s in SPACES.items():
        if s["long_name"].lower() == long_name.lower():
            return s
    return {"error": f"'{long_name}' not found"}


def get_all_signs() -> list:
    return list(SIGNS.values())


def get_sign(sign_id: str) -> dict:
    return SIGNS.get(sign_id, {"error": f"Sign '{sign_id}' not found"})


def check_occupancy_compliance(long_name: str, current: int) -> dict:
    space = get_space_by_name(long_name)
    if "error" in space: return space
    max_occ = space.get("max_occ", 0)
    return {
        "space": long_name, "floor": space.get("floor"),
        "current": current, "max": max_occ,
        "compliant": current <= max_occ,
        "status": "PASS" if current <= max_occ else "FAIL",
        "adb_ref": "ADB Vol2 Table B1 — Purpose Group 2a",
    }


if __name__ == "__main__":
    print(f"Spaces: {len(SPACES)}, Signs: {len(SIGNS)}, Alarms: {len(ALARMS)}")
    print(get_space_by_name("Lounge"))
    print(check_occupancy_compliance("Lounge", 150))