# build_rag.py
# Builds the ChromaDB vector database from the ADB PDF.

import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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

    lines = [l.strip() for l in text.split('\n')]
    lines = [l for l in lines
            if l and not re.match(r'^[A-Z0-9]{1,4}$', l)]
    return '\n'.join(lines)


# Sentence-aware chunking 

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


# Lightweight topic tagging 

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
    CHUNK_SIZE   = 500    

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

    cache_file = os.path.join("rag", "_query_cache.json")
    if os.path.exists(cache_file):
        os.remove(cache_file)
        print("Cleared stale query cache (rag/_query_cache.json)")

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