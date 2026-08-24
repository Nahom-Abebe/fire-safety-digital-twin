# diagnose_rag_233.py
#
# Checks whether Section 2.33's actual clause text exists anywhere in
# the ChromaDB collection at all — bypassing semantic/embedding search
# entirely and doing a direct substring check over every stored chunk's
# raw text. This is the only way to tell apart two very different
# problems that look identical from retrieval results alone:
#
#   1. The passage IS in the collection, but the query strings used
#      elsewhere just aren't phrased close enough to it semantically
#      to rank in the top results — a retrieval-tuning problem.
#   2. The passage was never correctly chunked into the collection
#      when build_rag.py ingested the PDF, and it genuinely isn't
#      retrievable no matter how the query is worded — an ingestion
#      problem, fixed by re-running build_rag.py, not by tuning
#      queries.
#
# Run from the project root: python diagnose_rag_233.py

import os
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import chromadb

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "rag", "adb_vectordb")
COLLECTION_NAME = "approved_document_b"

print("=" * 60)
print("  RAG DIAGNOSTIC — Section 2.33 ingestion check")
print("=" * 60)

client = chromadb.PersistentClient(path=DB_PATH)
collection = client.get_collection(name=COLLECTION_NAME)

total = collection.count()
print(f"\nTotal chunks in collection: {total}")

# Pull every document's raw text + metadata. No embeddings, no query,
# no ranking — this is a direct read of everything actually stored.
all_data = collection.get(include=["documents", "metadatas"])
docs  = all_data["documents"]
metas = all_data["metadatas"]
print(f"Retrieved {len(docs)} chunks for direct text search")

# ── Direct substring search — bypasses semantic ranking entirely ──────────
SEARCH_TERMS = ["2.33", "general provisions for residential care"]

for term in SEARCH_TERMS:
    print(f"\n{'-' * 60}")
    print(f"Chunks containing the literal substring: {term!r}")
    print("-" * 60)

    matches = [(i, doc, meta) for i, (doc, meta) in enumerate(zip(docs, metas))
              if term.lower() in doc.lower()]

    print(f"Found: {len(matches)} chunk(s)\n")

    if not matches:
        continue

    for i, doc, meta in matches[:10]:
        section_hint = (meta or {}).get("section_hint", "?")

        # Rough heuristic distinguishing an index/table-of-contents
        # entry (many short lines, list-like) from substantive prose
        # (longer lines, fewer breaks). Not perfect — read the printed
        # text yourself to confirm — but a useful first filter.
        lines = doc.split("\n")
        short_line_ratio = (sum(1 for l in lines if len(l.strip()) < 40)
                            / max(len(lines), 1))
        looks_like_index = len(lines) > 8 and short_line_ratio > 0.5
        tag = "[LOOKS LIKE INDEX]" if looks_like_index else "[LOOKS SUBSTANTIVE]"

        print(f"--- Chunk {i} | p.{section_hint} {tag} ---")
        print(doc[:400].strip())
        print()

print(f"\n{'=' * 60}")
print("  INTERPRETATION")
print("=" * 60)
print("""
If every match above is tagged [LOOKS LIKE INDEX]:
  The real clause text for 2.33 was likely never ingested — only the
  table-of-contents entry mentioning it was. Fix: check build_rag.py's
  PDF-to-chunk pipeline; the actual Section 2.33 page may have been
  skipped, merged into a neighbouring chunk oddly, or lost in
  extraction. Re-running build_rag.py against the source PDF is the
  next step, likely with a look at chunk boundaries around that page.

If at least one match is tagged [LOOKS SUBSTANTIVE]:
  The real text IS in the collection — this is a retrieval-ranking
  problem, not an ingestion problem. Fix: the "general_provisions"
  query string in rag/retriever.py's _CARE_HOME_QUERIES needs
  rewording to sit closer, semantically, to that chunk's actual
  phrasing — read the printed chunk text above and adjust the query
  to use similar vocabulary.

If NO matches at all for either search term:
  Confirms outright: this text is not in the vector store in any form,
  index or otherwise. Re-run build_rag.py.
""")