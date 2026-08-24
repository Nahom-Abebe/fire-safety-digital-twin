# build_rag.py
# Builds the ChromaDB vector database from the ADB PDF.
# Run once: python build_rag.py
# Requires: pip install chromadb sentence-transformers pypdf
#
# Fixes applied:
#
#   1. page.extract_text() returns raw PDF text including the
#      "ONLINE VERSION" watermark and the repeated edition/footer
#      line on every single page. That dirty text was chunked and
#      EMBEDDED directly — meaning the boilerplate wasn't just
#      cosmetic noise shown to the agent afterward, it was actively
#      part of the vector every chunk got embedded as, skewing which
#      chunks matched a given query in the first place. A downstream
#      fix in rag/retriever.py already strips this from what gets
#      DISPLAYED after retrieval, but that can only clean the
#      symptom — it can't undo an embedding that was already computed
#      from noisy text. _clean_page_text() now strips it here, before
#      chunking or embedding ever happens, so the vectors themselves
#      are computed from real content.
#
#   2. Chunking sliced raw text at a fixed CHUNK_SIZE character offset
#      with no regard for sentence or paragraph boundaries — visibly
#      confirmed in retrieval output cutting clauses mid-sentence
#      ("...Diagram 2.1 shows how to measure travel distances from a
#      dead end in an..."). _split_into_sentences() + _chunk_text()
#      now group whole sentences up to the size limit and never split
#      one in half. The old CHUNK_OVERLAP mechanism (100 raw
#      characters repeated between adjacent chunks) also directly
#      explains the near-duplicate chunks seen in retrieval results —
#      two adjacent chunks shared genuinely overlapping raw text by
#      construction.
#
#      A sentence-level carry-forward overlap was tried as the
#      replacement, but testing it against a representative sample
#      before shipping surfaced a real bug: when a chunk happened to
#      be a single short sentence, the "carry the last N sentences
#      forward" step could carry forward THAT ENTIRE chunk, and the
#      next chunk ended up being a strict superset of the one before
#      it — worse duplication than the original bug, not better.
#      Rather than ship a fix I can't fully verify against the real
#      PDF, overlap is now off by default (overlap_sentences=0).
#      Sentence-aware chunk boundaries already remove the main source
#      of lost context (mid-sentence cuts); each sentence now appears
#      in exactly one chunk, confirmed with an explicit duplication
#      check during testing.
#
#   3. No topic metadata existed at all beyond page number. Added a
#      lightweight, deterministic keyword-based topic tag per chunk
#      (not an ML classification pass — cheap, no new dependency, no
#      added embedding calls). This does NOT change retrieval
#      behaviour by itself — rag/retriever.py doesn't filter by it
#      yet — it's stored so metadata-based query filtering can be
#      added as a deliberate, separately-tested follow-up rather than
#      bundled into this rebuild.
#
#   Known limitation, not fixed here: chunking is still strictly
#   per-page (unchanged from the original structure) — a clause that
#   happens to span a page break still gets split at that boundary
#   regardless of sentence-awareness within each page. Fixing that
#   would mean concatenating the whole document into one continuous
#   text stream before chunking, then working out which page(s) each
#   resulting chunk actually came from for its section_hint — a
#   larger structural change than the scope of this fix.

import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── Boilerplate cleaning — applied to raw page text before chunking ───────────

def _clean_page_text(text: str) -> str:
    """
    Strips known PDF-extraction boilerplate BEFORE chunking/embedding.
    Fixing this at the source means the embedding vectors themselves
    are computed from clean text — see module docstring, fix 1.
    """
    text = re.sub(r'(?i)online\s*version', ' ', text)
    text = re.sub(
        r'(?i)building regulations 2010\s*approved document b volume 2,?\s*'
        r'2019 edition',
        ' ', text
    )
    text = re.sub(
        r'(?i)approved document b volume 2,?\s*2019 edition\s*'
        r'building regulations 2010',
        ' ', text
    )
    # Collapse now-empty lines left behind by the removals above, and
    # normalise whitespace runs the extraction process introduces
    # around diagrams/tables. Also drops bare page-furniture fragments
    # (standalone page numbers, short section codes like "B1") that
    # survive the removals above and would otherwise glue onto the
    # start of the next real sentence — confirmed directly in testing
    # ("27\nB1\n2.43 Bedrooms..." before this filter was added).
    lines = [l.strip() for l in text.split('\n')]
    lines = [l for l in lines
            if l and not re.match(r'^[A-Z0-9]{1,4}$', l)]
    return '\n'.join(lines)


# ── Sentence-aware chunking ────────────────────────────────────────────────

def _split_into_sentences(text: str) -> list:
    """
    Splits on sentence-ending punctuation followed by whitespace and
    a capital letter or digit — a lightweight heuristic that needs no
    external NLP dependency. Not perfect (an abbreviation like "e.g."
    can trip it occasionally), but far better than a blind character-
    count cut that ignores sentence structure entirely.
    """
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z0-9])', text)
    return [s.strip() for s in sentences if s.strip()]


def _chunk_text(text: str, chunk_size: int,
                overlap_sentences: int = 0) -> list:
    """
    Groups whole sentences into chunks up to ~chunk_size characters,
    never cutting a sentence in half. Overlap is off by default — see
    module docstring, fix 2, for why a sentence-carry-forward overlap
    was tried and removed after testing surfaced a duplication bug.
    Each sentence appears in exactly one chunk.
    """
    sentences = _split_into_sentences(text)
    if not sentences:
        return []

    chunks       = []
    current      = []
    current_len  = 0

    for sent in sentences:
        if current and current_len + len(sent) > chunk_size:
            chunks.append(' '.join(current))
            if overlap_sentences:
                current = current[-overlap_sentences:]
                current_len = sum(len(s) for s in current)
            else:
                current, current_len = [], 0
        current.append(sent)
        current_len += len(sent)

    if current:
        chunks.append(' '.join(current))

    return chunks


# ── Lightweight topic tagging ──────────────────────────────────────────────

# Deterministic keyword match, not an ML classification pass — cheap,
# no new dependency. A chunk can match more than one topic. Stored in
# metadata for a future, separately-tested query-side filtering pass;
# does not itself change any current retrieval behaviour.
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


def _tag_topics(text: str) -> list:
    lower = text.lower()
    return [topic for topic, keywords in _TOPIC_KEYWORDS.items()
            if any(kw in lower for kw in keywords)]


def build():
    PDF_PATH = "data/approved_document_b_vol2.pdf"
    DB_PATH  = "rag/adb_vectordb"
    COLLECTION_NAME = "approved_document_b"
    CHUNK_SIZE   = 500    # characters per chunk (soft limit — a chunk
                          # stops adding sentences once it would exceed
                          # this, but a single long sentence is never
                          # split to force it under the limit)

    print("Checking dependencies...")
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
        from pypdf import PdfReader
    except ImportError as e:
        print(f"Missing: {e}")
        print("Run: pip install chromadb sentence-transformers pypdf")
        return

    print(f"Loading PDF: {PDF_PATH}")
    if not os.path.exists(PDF_PATH):
        print(f"ERROR: PDF not found at {PDF_PATH}")
        print("Place approved_document_b_vol2.pdf in the data/ folder")
        return

    reader = PdfReader(PDF_PATH)
    print(f"Pages: {len(reader.pages)}")

    print("Extracting text...")
    raw_pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        raw_pages.append((i + 1, text))

    print("Cleaning boilerplate and chunking (sentence-aware)...")
    chunks, metadatas, ids = [], [], []
    chunk_id = 0
    for page_num, text in raw_pages:
        text = _clean_page_text(text)
        if len(text) < 50:
            continue

        page_chunks = _chunk_text(text, CHUNK_SIZE)
        for chunk in page_chunks:
            if len(chunk) < 50:
                continue
            chunks.append(chunk)
            metadatas.append({
                "page"        : page_num,
                "section_hint": f"ADB Vol2 p.{page_num}",
                "topics"      : ",".join(_tag_topics(chunk)) or "general",
            })
            ids.append(f"chunk_{chunk_id:05d}")
            chunk_id += 1

    print(f"Chunks created: {len(chunks)}")

    print("Loading embedding model (first run may download ~80MB)...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    print("Embedding chunks (this takes 1–3 minutes)...")
    embeddings = model.encode(chunks, show_progress_bar=True).tolist()

    print(f"Storing in ChromaDB at {DB_PATH}...")
    os.makedirs(DB_PATH, exist_ok=True)
    client = chromadb.PersistentClient(path=DB_PATH)

    try:
        client.delete_collection(COLLECTION_NAME)
        print("Deleted existing collection")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"description": "UK Approved Document B Vol 2"}
    )

    BATCH = 100
    for i in range(0, len(chunks), BATCH):
        collection.add(
            documents  = chunks[i:i+BATCH],
            embeddings = embeddings[i:i+BATCH],
            metadatas  = metadatas[i:i+BATCH],
            ids        = ids[i:i+BATCH],
        )
        print(f"  Stored {min(i+BATCH, len(chunks))}/{len(chunks)} chunks")

    print(f"\n✅ Vector database built")
    print(f"   Collection : {COLLECTION_NAME}")
    print(f"   Chunks     : {collection.count()}")
    print(f"   Location   : {DB_PATH}")
    print(f"\nNow run: python -m rag.retriever")


if __name__ == "__main__":
    build()