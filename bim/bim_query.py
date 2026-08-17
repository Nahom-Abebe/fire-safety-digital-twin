# bim/bim_query.py
#
# Fixes applied:
#   - check_occupancy_compliance() previously defaulted a missing
#     max_occ to 0, meaning any space whose JSON entry lacked that
#     field would report FAIL for any nonzero occupancy — indistin-
#     guishable from a genuine violation. Now reports a distinct
#     "no capacity data" state instead of silently treating "unknown"
#     as "zero."
#   - get_space_by_name() used s["long_name"] directly, so one space
#     entry missing that key would crash the whole lookup with a
#     KeyError instead of returning the function's own designed
#     {"error": ...} response. Now uses .get() with a safe default.
#   - The module-level JSON load now reports a clear, specific error
#     if global_ids_v2.json is missing or malformed, instead of every
#     importer crashing on a raw traceback before anything prints.
#   - check_occupancy_compliance() no longer returns the same adb_ref
#     for every space. _adb_ref_for_space() classifies the space by
#     room type and returns the matching clause — the SAME clause
#     text already used elsewhere in this project (fix_and_bake.py's
#     scenario definitions, animation_baker.py's board text), so the
#     live compliance check and the demo scenarios cite consistently
#     rather than the live path always citing one generic reference.
#
#     Classification order:
#       1. An explicit "room_type"/"category"/"use" field on the space
#          dict, if global_ids_v2.json ever carries one — authoritative.
#       2. Keyword matching against the space's long_name.
#       3. Falls back to the generic Table B1 reference, same as before,
#          for anything that doesn't match a known category.
#
#     The keyword list below only covers "lounge"/"bedroom" — the two
#     room types confirmed to exist in this project's own scenario
#     text. If global_ids_v2.json's actual long_name values differ
#     (e.g. specific room names rather than "Bedroom"), extend
#     ROOM_TYPE_KEYWORDS below to match them.

import json, os

_path = os.path.join(os.path.dirname(__file__), "global_ids_v2.json")

try:
    with open(_path, encoding="utf-8") as f:
        _data = json.load(f)
    SPACES = _data["spaces"]
    SIGNS  = _data["signs"]
    ALARMS = _data["alarms"]
    FLOORS = _data["floors"]
except FileNotFoundError:
    raise FileNotFoundError(
        f"bim/bim_query.py: global_ids_v2.json not found at {_path}. "
        f"This file is required at import time — every module that "
        f"imports bim_query will fail until it exists."
    )
except json.JSONDecodeError as e:
    raise ValueError(
        f"bim/bim_query.py: global_ids_v2.json is not valid JSON: {e}"
    )
except KeyError as e:
    raise KeyError(
        f"bim/bim_query.py: global_ids_v2.json is missing required "
        f"top-level key {e} — expected 'spaces', 'signs', 'alarms', "
        f"and 'floors'."
    )

# Room-type -> ADB reference. Text matches what's already cited
# elsewhere in this project so the live compliance path and the demo
# scenarios agree with each other. Extend the keyword lists once the
# real long_name values in global_ids_v2.json are known — corridors,
# stairs, and any other room types currently fall through to GENERIC.
GENERIC_ADB_REF  = "ADB Vol2 Table B1 — Purpose Group 2a"
ROOM_TYPE_ADB_REFS = {
    "bedroom": "ADB Clause 2.43 — bedroom max occupancy care home",
    "lounge" : "ADB Vol2 Table B1 — Purpose Group 2a "
               "(communal space occupancy)",
}
ROOM_TYPE_KEYWORDS = {
    "bedroom": ["bedroom", "bed room"],
    "lounge" : ["lounge", "communal", "day room", "dayroom"],
}


def _adb_ref_for_space(space: dict) -> str:
    """
    Classifies a space and returns the matching ADB reference. Checks
    an explicit type field first if one exists on the space dict,
    then falls back to keyword matching on long_name, then GENERIC.
    """
    explicit = (space.get("room_type") or space.get("category")
                or space.get("use") or "").lower()
    if explicit in ROOM_TYPE_ADB_REFS:
        return ROOM_TYPE_ADB_REFS[explicit]

    name = space.get("long_name", "").lower()
    for room_type, keywords in ROOM_TYPE_KEYWORDS.items():
        if any(kw in name for kw in keywords):
            return ROOM_TYPE_ADB_REFS[room_type]

    return GENERIC_ADB_REF


def get_space_by_name(long_name: str) -> dict:
    for gid, s in SPACES.items():
        if s.get("long_name", "").lower() == long_name.lower():
            return s
    return {"error": f"'{long_name}' not found"}


def get_all_signs() -> list:
    return list(SIGNS.values())


def get_sign(sign_id: str) -> dict:
    return SIGNS.get(sign_id, {"error": f"Sign '{sign_id}' not found"})


def check_occupancy_compliance(long_name: str, current: int) -> dict:
    space = get_space_by_name(long_name)
    if "error" in space:
        return space

    adb_ref = _adb_ref_for_space(space)

    if "max_occ" not in space:
        return {
            "space": long_name, "floor": space.get("floor"),
            "current": current, "max": None,
            "compliant": None,
            "status": "UNKNOWN — no capacity data for this space",
            "adb_ref": adb_ref,
        }

    max_occ = space["max_occ"]
    return {
        "space": long_name, "floor": space.get("floor"),
        "current": current, "max": max_occ,
        "compliant": current <= max_occ,
        "status": "PASS" if current <= max_occ else "FAIL",
        "adb_ref": adb_ref,
    }


if __name__ == "__main__":
    print(f"Spaces: {len(SPACES)}, Signs: {len(SIGNS)}, Alarms: {len(ALARMS)}")
    print(get_space_by_name("Lounge"))
    print(check_occupancy_compliance("Lounge", 150))
    print(check_occupancy_compliance("Lounge", 50))