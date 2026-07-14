# build_rag.py
# Builds the ChromaDB vector database from the ADB PDF.
# Run once: python build_rag.py
# Requires: pip install chromadb sentence-transformers pypdf

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def build():
    PDF_PATH = "data/approved_document_b_vol2.pdf"
    DB_PATH  = "rag/adb_vectordb"
    COLLECTION_NAME = "approved_document_b"
    CHUNK_SIZE   = 500    # characters per chunk
    CHUNK_OVERLAP = 100

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

    # Extract text with page-level section hints
    print("Extracting text...")
    raw_pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        raw_pages.append((i+1, text))

    # Chunk text
    chunks, metadatas, ids = [], [], []
    chunk_id = 0
    for page_num, text in raw_pages:
        text = text.strip()
        if len(text) < 50:
            continue
        for start in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP):
            chunk = text[start:start + CHUNK_SIZE].strip()
            if len(chunk) < 50:
                continue
            chunks.append(chunk)
            metadatas.append({
                "page"        : page_num,
                "section_hint": f"ADB Vol2 p.{page_num}",
            })
            ids.append(f"chunk_{chunk_id:05d}")
            chunk_id += 1

    print(f"Chunks created: {len(chunks)}")

    # Embed
    print("Loading embedding model (first run may download ~80MB)...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    print("Embedding chunks (this takes 1–3 minutes)...")
    embeddings = model.encode(chunks, show_progress_bar=True).tolist()

    # Store
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

    # Batch insert
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