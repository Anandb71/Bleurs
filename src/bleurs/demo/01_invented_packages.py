# A RAG pipeline, in the style LLMs actually write them.
# Two of these imports are real. Two were invented by the model.
import json
import os

import langchain_vectorstore_utils as lvu
from openai_embeddings_toolkit import EmbeddingPipeline


def build_index(documents, index_path):
    pipeline = EmbeddingPipeline(model="text-embedding-3-small")
    vectors = pipeline.embed_batch(documents)

    store = lvu.build_faiss_index(vectors, metric="cosine")
    lvu.persist(store, os.path.join(index_path, "index.faiss"))

    with open(os.path.join(index_path, "meta.json"), "w") as handle:
        json.dump({"count": len(documents)}, handle)

    return store
