"""
RAG Bot - NotebookLM-style, optimised for LM Studio
LM Studio exposes an OpenAI-compatible server at http://localhost:1234 by default.
  - Chat:   POST /v1/chat/completions
  - Models: GET  /v1/models

Run: pip install flask flask-cors PyMuPDF sentence-transformers faiss-cpu numpy requests
Then: python rag_bot.py
"""

import re
import numpy as np
import requests
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sentence_transformers import SentenceTransformer
from werkzeug.exceptions import HTTPException
import faiss

app = Flask(__name__, static_folder=".")
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────
EMBED_MODEL_NAME = "qwen/qwen3-1.7b"
CHUNK_SIZE       = 500   # characters per chunk
CHUNK_OVERLAP    = 80    # overlap between chunks
TOP_K            = 5     # retrieved chunks per query

# LM Studio defaults — user can override via /api/config
LMS_DEFAULT_BASE = "http://127.0.0.1:1234"

# ── State (in-memory, per-server-lifetime) ────────────────────────────────────
embed_model  = None
documents    = {}   # {doc_id: {"name": str, "chunks": [str]}}
index        = None # FAISS flat index
chunk_map    = []   # [(doc_id, chunk_text)]
llm_endpoint = LMS_DEFAULT_BASE   # base URL, e.g. http://localhost:1234
llm_model    = ""   # will be auto-detected from /v1/models if blank


def get_embed_model():
    global embed_model
    if embed_model is None:
        print("Loading embedding model…")
        embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return embed_model


def text_to_chunks(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    chunks, start = [], 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if len(c.strip()) > 30]


def rebuild_index():
    global index, chunk_map
    model = get_embed_model()
    chunk_map = []
    for doc_id, doc in documents.items():
        for chunk in doc["chunks"]:
            chunk_map.append((doc_id, chunk))

    if not chunk_map:
        index = None
        return

    texts = [c for _, c in chunk_map]
    embeddings = model.encode(texts, show_progress_bar=False).astype("float32")
    dim = embeddings.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(embeddings)


def retrieve(query: str, k: int = TOP_K) -> list[dict]:
    if index is None or not chunk_map:
        return []
    model = get_embed_model()
    q_emb = model.encode([query]).astype("float32")
    k = min(k, len(chunk_map))
    distances, indices = index.search(q_emb, k)
    results = []
    for dist, idx in zip(distances[0], indices[0]):
        doc_id, chunk = chunk_map[idx]
        results.append({
            "doc_name": documents[doc_id]["name"],
            "chunk": chunk,
            "score": float(dist),
        })
    return results


def get_active_model() -> str:
    """Return llm_model if set, else auto-detect the first loaded model from LM Studio."""
    if llm_model:
        return llm_model
    try:
        base = llm_endpoint.rstrip("/")
        resp = requests.get(f"{base}/v1/models", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            if models:
                return models[0]["id"]
    except Exception:
        pass
    return "local-model"   # fallback — LM Studio ignores this field anyway


def ask_llm(prompt: str) -> str:
    """
    Call LM Studio's OpenAI-compatible chat endpoint.
    LM Studio server: http://127.0.0.1:1234/v1/chat/completions
    """
    if not llm_endpoint:
        return "⚠️ No LLM endpoint configured. Set it via the Settings panel."

    base  = llm_endpoint.rstrip("/")
    url   = f"{base}/v1/chat/completions"
    model = get_active_model()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise research assistant. "
                    "Answer questions using ONLY the provided document context. "
                    "If the context does not contain enough information, say so clearly. "
                    "Be concise and cite which source the information comes from."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,   # lower = more factual / less hallucination
        "max_tokens": 1024,
        "stream": False,
    }

    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.ConnectionError:
        return (
            "⚠️ Cannot connect to LM Studio.\n"
            "Make sure the local server is running:\n"
            "  LM Studio → Local Server tab → Start Server"
        )
    except requests.exceptions.Timeout:
        return "⚠️ LM Studio timed out. The model may still be loading — try again in a moment."
    except Exception as e:
        return f"⚠️ LM Studio error: {e}"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index_page():
    return send_from_directory(".", "index.html")


@app.route("/api/config", methods=["POST"])
def set_config():
    global llm_endpoint, llm_model
    data = request.json or {}
    llm_endpoint = data.get("endpoint", LMS_DEFAULT_BASE).strip()
    llm_model    = data.get("model", "").strip()
    return jsonify({"status": "ok", "endpoint": llm_endpoint, "model": llm_model})


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({"endpoint": llm_endpoint, "model": llm_model})


@app.route("/api/models", methods=["GET"])
def list_models():
    """Proxy LM Studio's /v1/models so the frontend can populate a dropdown."""
    try:
        base = llm_endpoint.rstrip("/")
        resp = requests.get(f"{base}/v1/models", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m["id"] for m in data.get("data", [])]
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"models": [], "error": str(e)})


@app.route("/api/upload", methods=["POST"])
def upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    added = []
    for f in files:
        filename = f.filename or "unknown"
        ext = filename.rsplit(".", 1)[-1].lower()
        raw = f.read()

        if ext == "pdf":
            text = ""
            with fitz.open(stream=raw, filetype="pdf") as pdf:
                for page in pdf:
                    text += page.get_text()
        elif ext in ("txt", "md"):
            text = raw.decode("utf-8", errors="ignore")
        else:
            return jsonify({"error": f"Unsupported type: {ext}"}), 400

        doc_id = str(len(documents) + 1)
        chunks = text_to_chunks(text)
        documents[doc_id] = {"name": filename, "chunks": chunks}
        added.append({"id": doc_id, "name": filename, "chunks": len(chunks)})

    rebuild_index()
    return jsonify({"added": added, "total_docs": len(documents)})


@app.route("/api/documents", methods=["GET"])
def list_documents():
    return jsonify([
        {"id": did, "name": doc["name"], "chunks": len(doc["chunks"])}
        for did, doc in documents.items()
    ])


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def delete_document(doc_id):
    if doc_id not in documents:
        return jsonify({"error": "Not found"}), 404
    del documents[doc_id]
    rebuild_index()
    return jsonify({"status": "deleted"})


@app.errorhandler(Exception)
def handle_exception(e):
    code = 500
    if isinstance(e, HTTPException):
        code = e.code
    print("Server error:", repr(e))
    return jsonify({"error": str(e)}), code


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "Empty query"}), 400

    hits = retrieve(query)

    if not hits:
        context_block = "No documents have been uploaded yet."
    else:
        context_block = "\n\n---\n\n".join(
            f"[Source: {h['doc_name']}]\n{h['chunk']}" for h in hits
        )

    prompt = f"""You are a helpful research assistant. Answer the user's question using ONLY the provided context excerpts. If the answer is not in the context, say so clearly.

CONTEXT:
{context_block}

QUESTION: {query}

ANSWER:"""

    answer = ask_llm(prompt)

    return jsonify({
        "answer": answer,
        "sources": hits,
    })


if __name__ == "__main__":
    print("Starting RAG Bot on http://localhost:5000")
    print("Open index.html in your browser or visit http://localhost:5000")
    app.run(debug=True, port=5000)
