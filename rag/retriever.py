# rag/retriever.py
# Retrieves relevant ADB Vol2 passages from ChromaDB.
# Includes targeted care-home-specific queries that reliably hit:
#   - p.37  Section 2.33 "Residential care homes — General provisions"
#   - p.39  Clause 2.43  "Bedrooms should not contain more than one bed"
#   - p.25  Table 2.1    "Travel distance limits"
#   - p.17  Table B1     "Purpose Group 2a classification"
#   - p.221 Index        "Residential care homes 2.37"
#
# Fixes applied:
#
#   1. retrieve_regulations() was defined TWICE. Python silently kept
#      only the second definition, so the caching implementation right
#      above it — _REGULATION_CACHE and all — was dead code that never
#      ran. Every call re-embedded the query and re-hit ChromaDB even
#      for an identical repeated query within the same tick. Merged
#      into one definition; caching now actually executes.
#
#   2. retrieve_care_home_regulations() accepted current_occ/max_occ
#      but never referenced them anywhere in the function body — a
#      room at 4/3 and 40/3 retrieved identically. Removed from this
#      function's signature; get_adb_context() (which DOES use them,
#      for its header text) is unaffected.
#
#   3. The violation_type == "bedroom" branch could never fire through
#      the real call path — get_adb_context() only ever sets
#      violation_type to "pre_emptive" or room_type's own value, never
#      the literal string "bedroom". Removed the dead branch; bedroom
#      coverage already comes from the room_type == "room" branch,
#      which does fire in practice (confirmed in the self-test output).
#
#   4. query_keys were built generic-first (general_provisions,
#      purpose_group) then room-type-specific keys appended after, and
#      truncated to ordered[:3] before running. For any corridor/lobby
#      scenario this meant travel_distance — the single most relevant
#      concept for that room type — was silently dropped every time,
#      purely due to list position. Confirmed directly in the module's
#      own self-test: p.25 (Table 2.1, travel distance) and p.37
#      (Section 2.33, general provisions) never appeared once across
#      four examples specifically built to surface them. Query order
#      is now room-type-specific keys FIRST, generic context keys
#      last, and the cap raised from 3 to 4 so both specific and
#      generic queries usually survive rather than only ever the
#      generic ones.
#
#   5. Text snippets truncated by raw character count, visibly cutting
#      mid-word in the module's own output ("separate bed", "editi").
#      Now truncates at the last word boundary before the limit.

import os
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import warnings
warnings.filterwarnings("ignore", message=".*unauthenticated.*")
warnings.filterwarnings("ignore", message=".*HF Hub.*")

import chromadb
from sentence_transformers import SentenceTransformer

DB_PATH         = os.path.join(os.path.dirname(__file__), "adb_vectordb")
COLLECTION_NAME = "approved_document_b"
MODEL_NAME      = "all-MiniLM-L6-v2"

# Lazy-load to avoid slow startup when not needed
_client     = None
_collection = None
_model      = None

_REGULATION_CACHE = {}


def _init():
    global _client, _collection, _model
    if _collection is not None:
        return
    _client = chromadb.PersistentClient(path=DB_PATH)
    _model  = SentenceTransformer(MODEL_NAME)
    try:
        _collection = _client.get_collection(name=COLLECTION_NAME)
        print(f"RAG: loaded '{COLLECTION_NAME}' ({_collection.count()} chunks)")
    except Exception as e:
        raise RuntimeError(
            f"ChromaDB collection '{COLLECTION_NAME}' not found. "
            f"Run build_rag.py first. Error: {e}"
        )


def _truncate(text: str, limit: int) -> str:
    """Truncates at the last word boundary before limit, not mid-word."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    return cut + "..."


# ── Core retrieval ────────────────────────────────────────────────────────────

def retrieve_regulations(query: str, n: int = 3) -> list:
    """
    General-purpose ADB retrieval. Returns top-n most relevant passages
    for any query. Each result: {text, section_hint, distance}

    Cached per (query, n) for the session — the ADB document doesn't
    change between ticks, so a repeated identical query returns the
    cached result instead of re-embedding and re-querying ChromaDB.
    """
    _init()

    cache_key = f"{query}|{n}"
    if cache_key in _REGULATION_CACHE:
        return _REGULATION_CACHE[cache_key]

    embedding = _model.encode([query]).tolist()
    results   = _collection.query(
        query_embeddings=embedding,
        n_results=n,
        include=["documents", "metadatas", "distances"]
    )
    passages = [
        {
            "text"        : results["documents"][0][i],
            "section_hint": results["metadatas"][0][i].get(
                "section_hint", "ADB Vol2"),
            "distance"    : round(results["distances"][0][i], 4),
        }
        for i in range(len(results["documents"][0]))
    ]

    _REGULATION_CACHE[cache_key] = passages
    return passages


# ── Care-home-specific retrieval ──────────────────────────────────────────────

# These queries are tuned from the test_rag.py output to reliably hit
# the exact care-home sections confirmed present in your ADB document.
_CARE_HOME_QUERIES = {

    "general_provisions": (
        "residential care home means of escape corridor general provisions 2.33"
    ),

    "bedroom_occupancy": (
        "bedroom should not contain more than one single double bed 2.43"
    ),

    "travel_distance": (
        "travel distance escape route single direction corridor Table 2.1"
    ),

    "purpose_group": (
        "Purpose Group 2a residential sleeping accommodation institutional"
    ),

    "horizontal_escape": (
        "horizontal escape residential care homes inner rooms dead end 2.37"
    ),

    "stair_access": (
        "protected stairway residential care access bedroom vertical escape"
    ),
}


def retrieve_care_home_regulations(violation_type: str = "overcrowding",
                                   room_type: str = "room") -> list:
    """
    Retrieves ADB clauses specifically relevant to care home occupancy
    management. Selects query strategies based on what type of room/
    space is affected and the nature of the violation.

    violation_type : "overcrowding" | "pre_emptive" | "corridor"
    room_type      : "room" | "corridor" | "stair" | "lobby"

    Returns a deduplicated list of the most relevant passages, each
    tagged with section_hint for citation.

    Query order is room-type-specific keys FIRST, generic context
    (general_provisions, purpose_group) LAST — see module docstring,
    fix 4. The old generic-first order meant travel_distance was
    silently dropped for every corridor/lobby scenario purely due to
    list position, not relevance.
    """
    _init()

    query_keys = []

    if room_type in ("corridor", "lobby"):
        query_keys += ["travel_distance", "horizontal_escape"]
    elif room_type == "room":
        query_keys += ["bedroom_occupancy", "travel_distance"]
    elif room_type == "stair":
        query_keys += ["stair_access", "travel_distance"]
    else:
        query_keys += ["travel_distance", "horizontal_escape"]

    if violation_type == "pre_emptive":
        query_keys += ["travel_distance", "horizontal_escape"]
    elif violation_type == "corridor":
        query_keys += ["horizontal_escape"]

    # Generic context — always relevant, but never at the expense of
    # the room-type-specific queries above, so added last.
    query_keys += ["general_provisions", "purpose_group"]

    # Remove duplicates while preserving priority order
    seen = set()
    ordered = []
    for k in query_keys:
        if k not in seen:
            seen.add(k)
            ordered.append(k)

    # Run top-4 (was top-3) most relevant query strategies
    all_results = []
    seen_texts  = set()
    for key in ordered[:4]:
        query   = _CARE_HOME_QUERIES[key]
        results = retrieve_regulations(query, n=2)
        for r in results:
            fingerprint = r["text"][:80]
            if fingerprint not in seen_texts:
                seen_texts.add(fingerprint)
                r["query_strategy"] = key
                all_results.append(r)

    all_results.sort(key=lambda x: x["distance"])
    return all_results[:4]


def get_adb_context(room_label: str,
                    room_type: str,
                    current_occ: int,
                    max_occ: int,
                    pre_emptive: bool = False) -> str:
    """
    Convenience wrapper used by the Claude agent. Builds a structured
    ADB context string with specific clause references the agent can
    cite directly in its board directive.

    room_label  : graph label e.g. '0-4', '0-A'
    room_type   : 'room' | 'corridor' | 'stair' | 'lobby'
    current_occ : current occupant count — used for the header text
                  and ratio calculation below, not for query selection
    max_occ     : ADB-defined maximum — same
    pre_emptive : True if approaching capacity (80%+) but not yet exceeded

    Returns formatted string: "[Section] Clause text..."
    """
    violation_type = "pre_emptive" if pre_emptive else room_type

    passages = retrieve_care_home_regulations(
        violation_type=violation_type,
        room_type=room_type,
    )

    if not passages:
        return "ADB Vol2 — no relevant passage retrieved for this query"

    ratio  = round(current_occ / max_occ * 100) if max_occ > 0 else 0
    status = "approaching capacity" if pre_emptive else "EXCEEDED"

    header = (
        f"Room {room_label} ({room_type}) — occupancy {status}: "
        f"{current_occ}/{max_occ} ({ratio}%)\n"
        f"Relevant ADB Vol2 clauses:\n"
    )

    parts = []
    for p in passages:
        section = p["section_hint"]
        text    = _truncate(p["text"], 350)
        parts.append(f"[{section}] {text}")

    return header + "\n---\n".join(parts)


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  RAG RETRIEVER — Care Home Clause Test")
    print("=" * 60)

    print("\n1. General care home provisions:")
    for r in retrieve_care_home_regulations(
            violation_type="overcrowding", room_type="corridor"):
        print(f"  [{r['section_hint']}] dist={r['distance']} "
              f"strategy={r['query_strategy']}")
        print(f"  {_truncate(r['text'], 200)}")
        print()

    print("\n2. Bedroom overcrowding context:")
    print(get_adb_context("1-16", "room", 4, 3, pre_emptive=False))

    print("\n3. Corridor pre-emptive warning:")
    print(get_adb_context("0-A", "corridor", 120, 155, pre_emptive=True))

    print("\n4. Purpose Group 2a classification:")
    for r in retrieve_regulations(
            "Purpose Group 2a residential care sleeping accommodation", n=2):
        print(f"  [{r['section_hint']}] dist={r['distance']}")
        print(f"  {_truncate(r['text'], 200)}")
        print()