# test_rag.py
import sys
sys.path.insert(0, '.')
from rag.retriever import retrieve_regulations

queries = [
    'care home residential institution occupancy',
    'Purpose Group 2a sleeping accommodation bedroom',
    'residential care home means of escape corridor',
    'institutional residential occupancy load floor area',
    'bedroom travel distance escape route residential',
]

for q in queries:
    results = retrieve_regulations(q, n=2)
    print(f"Query: {q}")
    for r in results:
        section = r["section_hint"]
        text    = r["text"][:250].strip()
        dist    = r["distance"]
        print(f"  [{section}] dist={dist}")
        print(f"  {text}")
    print()