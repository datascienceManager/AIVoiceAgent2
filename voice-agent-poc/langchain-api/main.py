# ============================================================
#  langchain-api/main.py
#  Orchestration service — the brain of the voice agent
#  FastAPI + LangChain + Qdrant + MongoDB
# ============================================================

import os
import uuid
import httpx
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_qdrant import QdrantVectorStore
from langchain.chains import ConversationalRetrievalChain
from langchain.memory import ConversationBufferWindowMemory
from langchain_core.messages import HumanMessage, AIMessage

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis

# ── Config from environment ──────────────────────────────────
OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_EMBED      = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
QDRANT_URL        = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "voice_conversations")
MONGODB_URL       = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
MONGODB_DB        = os.getenv("MONGODB_DB", "voice_agent")
REDIS_URL         = os.getenv("REDIS_URL", "redis://redis:6379")
WHISPER_URL       = os.getenv("WHISPER_URL", "http://whisper:9000")
TTS_URL           = os.getenv("TTS_URL", "http://tts:5002")
REDIS_PASSWORD    = os.getenv("REDIS_PASSWORD", "")
SUPPORTED_LANGS   = os.getenv("SUPPORTED_LANGUAGES", "ar,fr,en").split(",")

app = FastAPI(title="Voice Agent Orchestrator", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Shared clients (initialized on startup) ──────────────────
mongo_client: AsyncIOMotorClient = None
redis_client = None
qdrant_client: QdrantClient = None
vector_store: QdrantVectorStore = None
embeddings: OllamaEmbeddings = None


# ── Pydantic models ──────────────────────────────────────────

class TextTurnRequest(BaseModel):
    session_id: str
    text: str
    language: Optional[str] = "auto"   # "ar" | "fr" | "en" | "auto"
    user_id: Optional[str] = None

class TurnResponse(BaseModel):
    session_id: str
    user_text: str
    agent_text: str
    detected_language: str
    audio_url: Optional[str] = None

class SessionCreate(BaseModel):
    user_id: Optional[str] = None
    preferred_language: Optional[str] = "ar"


# ── Startup / Shutdown ───────────────────────────────────────

@app.on_event("startup")
async def startup():
    global mongo_client, redis_client, qdrant_client, vector_store, embeddings

    # MongoDB
    mongo_client = AsyncIOMotorClient(MONGODB_URL)

    # Redis
    redis_url = REDIS_URL
    if REDIS_PASSWORD:
        redis_url = REDIS_URL.replace("redis://", f"redis://:{ REDIS_PASSWORD}@")
    redis_client = await aioredis.from_url(redis_url, decode_responses=True)

    # Qdrant
    qdrant_client = QdrantClient(url=QDRANT_URL)
    _ensure_qdrant_collection()

    # Embeddings + vector store
    embeddings = OllamaEmbeddings(
        model=OLLAMA_EMBED,
        base_url=OLLAMA_BASE_URL,
    )
    vector_store = QdrantVectorStore(
        client=qdrant_client,
        collection_name=QDRANT_COLLECTION,
        embedding=embeddings,
    )

    print(f"[startup] Orchestrator ready. Model={OLLAMA_MODEL}, Langs={SUPPORTED_LANGS}")


@app.on_event("shutdown")
async def shutdown():
    if mongo_client:
        mongo_client.close()
    if redis_client:
        await redis_client.close()


def _ensure_qdrant_collection():
    """Create vector collection if it doesn't exist yet."""
    existing = [c.name for c in qdrant_client.get_collections().collections]
    if QDRANT_COLLECTION not in existing:
        qdrant_client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )
        print(f"[qdrant] Created collection: {QDRANT_COLLECTION}")


# ── Health check ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "model": OLLAMA_MODEL, "languages": SUPPORTED_LANGS}


# ── Session management ───────────────────────────────────────

@app.post("/sessions")
async def create_session(body: SessionCreate):
    session_id = str(uuid.uuid4())
    session = {
        "session_id": session_id,
        "user_id": body.user_id or "anonymous",
        "preferred_language": body.preferred_language,
        "created_at": datetime.utcnow().isoformat(),
        "turns": [],
    }
    db = mongo_client[MONGODB_DB]
    await db.sessions.insert_one(session)
    await redis_client.setex(f"session:{session_id}", 3600, body.preferred_language)
    return {"session_id": session_id}


# ── STT: audio → text ────────────────────────────────────────

@app.post("/stt")
async def speech_to_text(audio: UploadFile = File(...), language: str = "auto"):
    """
    Send audio to Whisper, get back transcript + detected language.
    Whisper auto-detects language when language="auto".
    """
    audio_bytes = await audio.read()
    whisper_lang = None if language == "auto" else language

    async with httpx.AsyncClient(timeout=60) as client:
        files = {"audio_file": (audio.filename, audio_bytes, audio.content_type)}
        params = {"encode": "true", "task": "transcribe", "word_timestamps": "false"}
        if whisper_lang:
            params["language"] = whisper_lang

        resp = await client.post(f"{WHISPER_URL}/asr", files=files, params=params)
        resp.raise_for_status()

    result = resp.json()
    return {
        "text": result.get("text", ""),
        "language": result.get("language", "unknown"),
    }


# ── TTS: text → audio URL ─────────────────────────────────────

@app.post("/tts")
async def text_to_speech(text: str, language: str = "ar"):
    """
    Send text to Coqui XTTS, get back audio bytes.
    Language codes: ar / fr / en
    """
    lang_map = {"ar": "ar", "fr": "fr-fr", "en": "en"}
    coqui_lang = lang_map.get(language, "en")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TTS_URL}/api/tts",
            params={"text": text, "language_idx": coqui_lang},
        )
        resp.raise_for_status()

    # Return raw audio — the caller (nginx / frontend) streams this
    return {"audio_bytes": resp.content.hex(), "content_type": "audio/wav"}


# ── Core: text turn (text in → text out) ─────────────────────

@app.post("/turn", response_model=TurnResponse)
async def process_turn(body: TextTurnRequest):
    """
    Main conversation endpoint.
    1. Detect language if auto
    2. Retrieve relevant context from Qdrant (RAG)
    3. Build prompt with conversation history from Redis
    4. Call Ollama LLM
    5. Store turn in MongoDB + embed in Qdrant
    6. Return agent response text
    """
    session_id = body.session_id
    user_text  = body.text.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Empty input text")

    # 1. Language detection (simple heuristic — replace with fastText for production)
    detected_lang = body.language
    if detected_lang == "auto":
        detected_lang = _detect_language_simple(user_text)

    # 2. Retrieve relevant past context from vector store
    relevant_docs = vector_store.similarity_search(user_text, k=4)
    context_chunks = "\n".join(d.page_content for d in relevant_docs)

    # 3. Load recent conversation history from Redis
    history_key = f"history:{session_id}"
    raw_history = await redis_client.lrange(history_key, -10, -1)  # last 5 turns
    history_text = "\n".join(raw_history) if raw_history else ""

    # 4. Build system prompt (language-aware)
    system_prompt = _build_system_prompt(detected_lang, context_chunks, history_text)

    # 5. Call Ollama
    llm = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, temperature=0.7)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_text},
    ]
    response = await asyncio.to_thread(llm.invoke, messages)
    agent_text = response.content.strip()

    # 6a. Store in MongoDB
    db = mongo_client[MONGODB_DB]
    turn = {
        "session_id": session_id,
        "user_id": body.user_id,
        "user_text": user_text,
        "agent_text": agent_text,
        "language": detected_lang,
        "timestamp": datetime.utcnow().isoformat(),
    }
    await db.turns.insert_one(turn)

    # 6b. Append to Redis history (for in-context memory)
    await redis_client.rpush(history_key, f"User: {user_text}\nAgent: {agent_text}")
    await redis_client.expire(history_key, 3600)

    # 6c. Embed turn and store in Qdrant (for long-term retrieval)
    combined_text = f"[{detected_lang}] User: {user_text}\nAgent: {agent_text}"
    await asyncio.to_thread(
        vector_store.add_texts,
        [combined_text],
        metadatas=[{"session_id": session_id, "language": detected_lang}],
    )

    return TurnResponse(
        session_id=session_id,
        user_text=user_text,
        agent_text=agent_text,
        detected_language=detected_lang,
    )


# ── Voice turn (audio in → audio out) ────────────────────────

@app.post("/voice-turn")
async def voice_turn(
    session_id: str,
    audio: UploadFile = File(...),
    language: str = "auto",
):
    """
    Full pipeline: audio → Whisper STT → LLM → Coqui TTS → audio
    """
    # STT
    audio_bytes = await audio.read()
    async with httpx.AsyncClient(timeout=60) as client:
        files = {"audio_file": (audio.filename, audio_bytes, audio.content_type)}
        params = {"encode": "true", "task": "transcribe"}
        if language != "auto":
            params["language"] = language
        stt_resp = await client.post(f"{WHISPER_URL}/asr", files=files, params=params)
        stt_resp.raise_for_status()
    stt_result = stt_resp.json()
    transcript = stt_result.get("text", "")
    detected_lang = stt_result.get("language", "ar")

    # Orchestrate text turn
    turn_result = await process_turn(TextTurnRequest(
        session_id=session_id,
        text=transcript,
        language=detected_lang,
    ))

    # TTS
    tts_result = await text_to_speech(turn_result.agent_text, language=detected_lang)

    return {
        "transcript": transcript,
        "agent_text": turn_result.agent_text,
        "detected_language": detected_lang,
        "audio_hex": tts_result["audio_bytes"],
        "content_type": tts_result["content_type"],
    }


# ── Session history ──────────────────────────────────────────

@app.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str, limit: int = 20):
    db = mongo_client[MONGODB_DB]
    cursor = db.turns.find(
        {"session_id": session_id},
        {"_id": 0}
    ).sort("timestamp", -1).limit(limit)
    turns = await cursor.to_list(length=limit)
    return {"session_id": session_id, "turns": list(reversed(turns))}


# ── Helpers ──────────────────────────────────────────────────

def _detect_language_simple(text: str) -> str:
    """
    Lightweight heuristic language detection.
    Replace with fastText model for production accuracy.
    """
    arabic_chars = sum(1 for c in text if "\u0600" <= c <= "\u06FF")
    if arabic_chars / max(len(text), 1) > 0.3:
        return "ar"
    french_markers = ["je ", "tu ", "il ", "elle ", "nous ", "vous ",
                      "les ", "des ", "une ", "est ", "que ", "pas "]
    text_lower = text.lower()
    if any(m in text_lower for m in french_markers):
        return "fr"
    return "en"


def _build_system_prompt(language: str, context: str, history: str) -> str:
    """Build a language-aware system prompt with RAG context injected."""
    lang_instructions = {
        "ar": "أنت مساعد صوتي ذكي. أجب دائماً باللغة العربية بأسلوب واضح ومهني.",
        "fr": "Tu es un assistant vocal intelligent. Réponds toujours en français de manière claire et professionnelle.",
        "en": "You are an intelligent voice assistant. Always respond in English clearly and professionally.",
    }
    instruction = lang_instructions.get(language, lang_instructions["en"])

    prompt = f"""{instruction}

Relevant context from previous conversations:
{context if context else 'No prior context available.'}

Recent conversation history:
{history if history else 'This is the start of the conversation.'}

Keep responses concise and suitable for voice output (2-4 sentences max).
Do not use markdown, bullet points, or special formatting in your response.
"""
    return prompt
