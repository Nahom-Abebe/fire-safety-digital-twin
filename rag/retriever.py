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
#
#   6. Two further issues, confirmed directly against real test runs:
#      near-duplicate chunks from the same page split at different
#      boundaries both surviving as if genuinely distinct sources
#      (p.25 appearing twice from one passage), and index/table-of-
#      contents chunks (e.g. p.221 — a list of topics and page
#      numbers) ranking ahead of substantive clause text purely
#      because they're short and semantically broad. Both fixed via
#      _dedup_and_rank() (page-level dedup + _looks_like_index()
#      distance penalty) — and, since retrieve_regulations() is also
#      called directly by the agent's get_regulations tool separately
#      from the care-home-specific multi-query path, it now fetches a
#      larger candidate pool from ChromaDB and applies the same
#      dedup/ranking itself, so a direct call gets the same quality
#      guarantees rather than only the care-home wrapper benefiting.
#
#   7. Metadata-based topic filtering (build_rag.py now tags every
#      chunk with a lightweight keyword-derived "topics" field) was
#      stored but never used for retrieval — this wires it in.
#      ChromaDB's native `where` filtering needs scalar metadata
#      fields, not the comma-joined multi-value string topics are
#      stored as, so this isn't a ChromaDB-side filter — it's a
#      Python-side distance BOOST (the inverse of _INDEX_PENALTY)
#      applied during the same _dedup_and_rank() pass, using the
#      metadata that's already returned with each candidate. A chunk
#      whose stored topics include the query's expected topic ranks
#      ahead of an equally-close chunk that doesn't. For
#      retrieve_care_home_regulations()'s per-strategy queries the
#      expected topic is known exactly (the _CARE_HOME_QUERIES key IS
#      a _TOPIC_KEYWORDS category) and passed explicitly. For a
#      direct retrieve_regulations() call with no topic hint (e.g. the
#      agent's get_regulations tool, or a raw call), the topic is
#      inferred from the query text itself using the same keyword
#      matching build_rag.py uses to tag chunks — weaker than an
#      explicit hint, but still better than no topic awareness at
#      all. Requires chunks rebuilt with the current build_rag.py to
#      have any effect — a chunk with no "topics" metadata (from an
#      older vector store) simply gets no boost, never an error.

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

# Kept in sync with build_rag.py's _TOPIC_KEYWORDS — same six
# categories, same keyword lists. Duplicated rather than imported
# (build_rag.py is a root-level script, not part of the rag package)
# so retriever.py has no fragile cross-directory import dependency.
_TOPIC_KEYWORDS = {
    "travel_distance"    : ["travel distance", "table 2.1", "dead end"],
    "bedroom_occupancy"  : ["bedroom", "single or double bed"],
    "general_provisions" : ["general provisions", "fire safety strategy",
                            "residential care home"],
    "horizontal_escape"  : ["horizontal escape", "inner room",
                            "progressive horizontal evacuation"],
    "stair_access"       : ["protected stairway", "escape stair",
                            "vertical escape"],
    "purpose_group"      : ["purpose group", "sleeping accommodation"],
}

_TOPIC_BOOST = 0.3   # subtracted from distance for a topic match —
                     # the inverse of _INDEX_PENALTY


def _infer_query_topics(text: str) -> set:
    """Best-effort topic guess from free query text, for a
    retrieve_regulations() call with no explicit boost_topic hint."""
    lower = text.lower()
    return {topic for topic, keywords in _TOPIC_KEYWORDS.items()
            if any(kw in lower for kw in keywords)}


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


def _clean_chunk_text(text: str) -> str:
    """
    Strips known PDF-extraction boilerplate from a chunk's text before
    it's returned to any caller — the "ONLINE VERSION" watermark
    repeated on every page, the standard edition/regulations footer,
    and the resulting blank/near-blank lines left behind once those
    are removed.

    Fix applied: _looks_like_index() already filters these same
    patterns, but only for its own internal ranking decision — the
    actual `text` field returned to every caller (including the
    agent's get_regulations/get_adb_violation_context tool results)
    still carried the raw boilerplate through untouched. That's real,
    unnecessary noise in the LLM's context window on every single
    citation, confirmed directly in this module's own test output
    (e.g. "Building Regulations 2010 Approved Document B Volume 2,
    2019 edition 27 B1 ... ONLINE VERSION ONLINE VERSION" prefixing
    genuine clause text). This does not touch chunk BOUNDARIES —
    where a chunk starts and ends is decided at ingestion time by
    build_rag.py, which this module never sees — it only removes
    known-junk lines from within whatever chunk was already returned.
    """
    _ARTIFACT_LINE_PATTERNS = (
        "online version",
        "building regulations 2010",
        "approved document b volume 2",
    )
    lines = text.split("\n")
    cleaned = [
        l for l in lines
        if l.strip() and not any(p in l.strip().lower()
                                 for p in _ARTIFACT_LINE_PATTERNS)
    ]
    return " ".join(l.strip() for l in cleaned)


def _looks_like_index(text: str) -> bool:
    """
    Rough heuristic distinguishing an index/table-of-contents entry
    (many short, list-like lines — a page number next to a topic name)
    from substantive clause prose. Confirmed against real chunks from
    this exact document via diagnose_rag_233.py. Not perfect, but
    reliable enough to demote rather than promote an index page when
    a genuine clause is also available.

    Fix applied: known PDF-extraction artifacts (the "ONLINE VERSION"
    watermark repeated on every page, blank lines from page breaks,
    the standard footer text) were counted as "short lines" the same
    as genuine index entries. A real clause page carrying this
    boilerplate could rack up enough short lines to be misclassified
    as an index — confirmed directly: Clause 2.43 (p.39), the single
    most load-bearing citation in this project since nearly every
    bedroom scenario is built around it, disappeared entirely from a
    test specifically checking bedroom-occupancy retrieval after this
    penalty was added, having been present in every prior run. These
    known artifact lines are now filtered out before the short-line
    ratio is computed, so they can't count against genuine content.
    """
    _ARTIFACT_PATTERNS = (
        "online version",
        "building regulations 2010",
        "approved document b volume 2",
    )
    lines = [l for l in text.split("\n")
            if l.strip() and not any(p in l.strip().lower()
                                     for p in _ARTIFACT_PATTERNS)]
    if len(lines) <= 8:
        return False
    short_line_ratio = (sum(1 for l in lines if len(l.strip()) < 40)
                        / max(len(lines), 1))
    return short_line_ratio > 0.5


# Added to an index-like chunk's distance before final ranking — not
# excluded outright (a poor source is still better than none if
# nothing substantive was found), but pushed behind genuine clause
# text of similar relevance. Distances observed in this collection
# run roughly 0.4-0.95, so this is enough to demote an index chunk
# below most real content without being an absolute exclusion.
_INDEX_PENALTY = 0.5


def _dedup_and_rank(candidates: list, n: int,
                    boost_topics: set = None) -> list:
    """
    Shared by retrieve_regulations() and retrieve_care_home_regulations()
    — dedups by page (section_hint) so two chunks from the same page,
    split at different boundaries by the chunker, don't both survive as
    if they were genuinely distinct sources (confirmed directly: p.25
    appearing twice from one underlying passage in a real test run),
    then demotes index-like chunks below substantive ones with
    _looks_like_index()/_INDEX_PENALTY, and boosts chunks whose stored
    topics metadata overlaps boost_topics with _TOPIC_BOOST, before
    taking the top n. boost_topics=None applies no boost — every
    candidate ranks on distance/index-penalty alone, same as before
    fix 7.
    """
    seen_texts = set()
    seen_pages = set()
    deduped    = []
    for c in candidates:
        fingerprint = c["text"][:80]
        page        = c.get("section_hint", "")
        if fingerprint in seen_texts:
            continue
        if page and page in seen_pages:
            continue
        seen_texts.add(fingerprint)
        if page:
            seen_pages.add(page)
        deduped.append(c)

    def _rank_key(x):
        score = x["distance"]
        if _looks_like_index(x["text"]):
            score += _INDEX_PENALTY
        if boost_topics:
            chunk_topics = set((x.get("topics") or "").split(","))
            if chunk_topics & boost_topics:
                score -= _TOPIC_BOOST
        return score

    deduped.sort(key=_rank_key)
    return deduped[:n]


# ── Core retrieval ────────────────────────────────────────────────────────────

def retrieve_regulations(query: str, n: int = 3,
                         boost_topic: str = None) -> list:
    """
    General-purpose ADB retrieval. Returns top-n most relevant passages
    for any query. Each result: {text, section_hint, distance, topics}

    Cached per (query, n, boost_topic) for the session — the ADB
    document doesn't change between ticks, so a repeated identical
    query returns the cached result instead of re-embedding and
    re-querying ChromaDB.

    Fix applied: this is called directly by the agent's get_regulations
    tool, separately from retrieve_care_home_regulations()'s
    multi-query pool — so it previously had no way to dedup near-
    duplicate page splits or demote index/table-of-contents chunks,
    even after those fixes were added to the care-home wrapper. Now
    fetches a larger candidate pool (3x n, minimum 8) from ChromaDB
    and applies the same _dedup_and_rank() logic before returning the
    top n, so a direct get_regulations call gets the same quality
    guarantees as the care-home-specific path.

    boost_topic : one of build_rag.py's _TOPIC_KEYWORDS categories
        (e.g. "bedroom_occupancy", "travel_distance"). A chunk whose
        stored metadata topics include this ranks ahead of an equally
        close chunk that doesn't. If not supplied, the topic is
        inferred from the query text itself via _infer_query_topics()
        — weaker than an explicit hint but still topic-aware. See
        module docstring, fix 7.
    """
    _init()

    cache_key = f"{query}|{n}|{boost_topic}"
    if cache_key in _REGULATION_CACHE:
        return _REGULATION_CACHE[cache_key]

    boost_topics = {boost_topic} if boost_topic else _infer_query_topics(query)

    pool_size = max(n * 3, 8)
    embedding = _model.encode([query]).tolist()
    results   = _collection.query(
        query_embeddings=embedding,
        n_results=pool_size,
        include=["documents", "metadatas", "distances"]
    )
    candidates = [
        {
            "text"        : _clean_chunk_text(results["documents"][0][i]),
            "section_hint": results["metadatas"][0][i].get(
                "section_hint", "ADB Vol2"),
            "distance"    : round(results["distances"][0][i], 4),
            "topics"      : results["metadatas"][0][i].get("topics", ""),
        }
        for i in range(len(results["documents"][0]))
    ]

    passages = _dedup_and_rank(candidates, n, boost_topics=boost_topics)

    _REGULATION_CACHE[cache_key] = passages
    return passages


# ── Care-home-specific retrieval ──────────────────────────────────────────────

# These queries are tuned from the test_rag.py output to reliably hit
# the exact care-home sections confirmed present in your ADB document.
_CARE_HOME_QUERIES = {

    # Fixed: rewritten from a guessed phrasing ("means of escape
    # corridor general provisions") that shared almost no vocabulary
    # with the actual clause text, to one built from the real chunk
    # confirmed present in the vector store (p.37, chunk 193, verified
    # via diagnose_rag_233.py — a direct substring check bypassing
    # semantic ranking entirely). The old query's "means of escape
    # corridor" framing was semantically much closer to the travel-
    # distance/horizontal-escape passages than to 2.33 itself, which
    # is actually about fire safety strategy depending on building
    # design, furnishing, staffing, and resident dependency level —
    # not escape routes directly. That mismatch is why this query
    # never surfaced the real clause across every prior test run.
    "general_provisions": (
        "fire safety strategy building design furnished staffed "
        "managed level of dependency residents care home"
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
    (general_provisions) LAST — see module docstring, fix 4. The old
    generic-first order meant travel_distance was silently dropped for
    every corridor/lobby scenario purely due to list position, not
    relevance.

    Fixes applied to result selection:
      - Dedup was only comparing the first 80 characters of each
        chunk's text. Two chunks from the SAME page, split at a
        different boundary by the chunker, start with different text
        and slipped past this check as if genuinely distinct sources —
        confirmed directly (p.25 appearing twice from one underlying
        passage in a real test run). Now also deduped by section_hint
        (page), keeping only the closest match per page, so results
        represent genuinely different sources rather than the same
        passage split twice.
      - Index/table-of-contents chunks (e.g. p.221 — a list of topics
        and page numbers, not actual clause text) were ranking ahead
        of substantive content purely because they're short and
        semantically broad. _looks_like_index() now adds
        _INDEX_PENALTY before final ranking, so real clause text wins
        when both are candidates. Not excluded outright — still
        surfaced if it's genuinely the only relevant result found.

    Fix 8 — boost didn't survive the final cross-query merge: each
    query_keys[key] call was boosted internally via
    retrieve_regulations(..., boost_topic=key), but the FINAL merge
    across all four queries' results re-sorted by raw distance alone
    with no boost applied, discarding that effect. Confirmed directly:
    a "room" (bedroom) violation's own bedroom_occupancy result
    (Clause 2.43) still ranked 3rd overall despite being boosted
    within its own sub-query, because general_provisions/travel_
    distance results from OTHER sub-queries had lower raw distances
    and the final merge didn't know to prefer the room-type's own
    primary topic. The final merge now boosts whichever topic is
    query_keys[0] — always the single most room-type-specific key by
    construction (bedroom_occupancy for "room", travel_distance for
    corridor/lobby, stair_access for "stair") — so it's the topic
    whose relevance is least ambiguous, and the boost now applies at
    the stage that actually decides the visible output order.

    Fix 9 — vector space overlap between room types: the generic tail
    previously always appended BOTH general_provisions AND
    purpose_group, and "room"'s own query set also included
    travel_distance (shared with corridor's query set) — meaning 3 of
    4 queries could be identical or near-identical between a bedroom
    and a corridor context, confirmed directly (p.37/p.54/p.17 shared
    across both in a real test run). purpose_group is background/
    definitional content, not specific to a particular room's
    occupancy violation, so it's dropped from the automatic tail
    (still reachable via a direct retrieve_regulations() call).
    travel_distance is no longer automatically added for "room" —
    bedroom_occupancy alone is now that context's specific query. This
    roughly halves the query overlap between room and corridor
    contexts (down to just general_provisions, which genuinely does
    apply everywhere in the real regulation — a shared building-wide
    provision, not a retrieval failure).
    """
    _init()

    query_keys = []

    if room_type in ("corridor", "lobby"):
        query_keys += ["travel_distance", "horizontal_escape"]
    elif room_type == "room":
        query_keys += ["bedroom_occupancy"]
    elif room_type == "stair":
        query_keys += ["stair_access", "travel_distance"]
    else:
        query_keys += ["travel_distance", "horizontal_escape"]

    if violation_type == "pre_emptive":
        query_keys += ["travel_distance", "horizontal_escape"]
    elif violation_type == "corridor":
        query_keys += ["horizontal_escape"]

    # Generic context — always relevant, but never at the expense of
    # the room-type-specific queries above, so added last. Only one
    # generic key now, not two — see fix 9.
    query_keys += ["general_provisions"]

    # Remove duplicates while preserving priority order. The first
    # entry is always the single most room-type-specific key by
    # construction — used as the final-merge boost topic below.
    seen = set()
    ordered = []
    for k in query_keys:
        if k not in seen:
            seen.add(k)
            ordered.append(k)

    primary_topic = ordered[0]

    # Fewer distinct queries now run for some room types (e.g. "room"
    # runs only 2) — fetch more per query so the candidate pool stays
    # a healthy size for dedup/ranking to work with either way.
    n_per_query = max(2, 6 // max(len(ordered), 1))

    all_results = []
    for key in ordered[:4]:
        query   = _CARE_HOME_QUERIES[key]
        results = retrieve_regulations(query, n=n_per_query, boost_topic=key)
        for r in results:
            r["query_strategy"] = key
            all_results.append(r)

    # Final merge boosts the room-type's own primary topic — see
    # fix 8. This is the stage that actually decides the visible
    # output order, unlike the per-query boosts above which only
    # affected each query's own internal top-n before merging.
    return _dedup_and_rank(all_results, 4, boost_topics={primary_topic})


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