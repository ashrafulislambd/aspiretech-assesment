import uuid
import logging

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# The collection name inside Qdrant where all lead embeddings are stored
COLLECTION_NAME = "leads"

# all-MiniLM-L6-v2 is a lightweight, fast model well-suited for semantic
# similarity on short business text. It outputs 384-dimensional vectors
# and runs entirely on CPU — no GPU or external API required.
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384

# Module-level singleton — the model is loaded once and reused for every request
_embedding_model: SentenceTransformer | None = None


def _get_embedding_model() -> SentenceTransformer:
    """Loads the embedding model on first call and returns the cached instance on all subsequent calls."""
    global _embedding_model
    if _embedding_model is None:
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL_NAME}")
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _embedding_model


def get_qdrant_client(host: str, port: int) -> QdrantClient:
    """Creates and returns a Qdrant client pointed at the given host and port."""
    return QdrantClient(host=host, port=port)


def ensure_collection_exists(client: QdrantClient) -> None:
    """Creates the leads collection in Qdrant if it does not already exist."""
    existing_collection_names = [c.name for c in client.get_collections().collections]

    if COLLECTION_NAME not in existing_collection_names:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBEDDING_DIMENSION, distance=Distance.COSINE),
        )
        logger.info(f"Created Qdrant collection: '{COLLECTION_NAME}'")
    else:
        logger.info(f"Qdrant collection '{COLLECTION_NAME}' already exists.")


def embed_message(message: str) -> list[float]:
    """Converts a lead message into a 384-dimensional vector using all-MiniLM-L6-v2."""
    model = _get_embedding_model()
    embedding = model.encode(message, convert_to_numpy=True)
    return embedding.tolist()


def find_similar_lead(
    client: QdrantClient,
    message_embedding: list[float],
    similarity_threshold: float,
) -> dict | None:
    """
    Searches Qdrant for the closest past lead to the given embedding.

    Returns the matching lead's metadata and similarity score if the top result
    is above the threshold. Returns None if no match is close enough.
    """
    query_response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=message_embedding,
        limit=1,
        with_payload=True,
    )
    search_results = query_response.points

    if not search_results:
        return None

    top_result = search_results[0]
    similarity_score = top_result.score

    if similarity_score < similarity_threshold:
        logger.info(
            f"Closest past lead scored {similarity_score:.2f} — below threshold {similarity_threshold}, no match."
        )
        return None

    logger.info(f"Similar past lead found with similarity score: {similarity_score:.2f}")
    return {
        "score": similarity_score,
        "payload": top_result.payload,
    }


def store_lead_embedding(
    client: QdrantClient,
    message_embedding: list[float],
    lead_metadata: dict[str, str | int],
) -> None:
    """
    Saves a lead's embedding and key metadata to Qdrant.

    The metadata stored as payload (name, company, score, tier) lets future
    similar-lead matches include useful context in the AI prompt.
    """
    point = PointStruct(
        id=str(uuid.uuid4()),
        vector=message_embedding,
        payload=lead_metadata,
    )
    client.upsert(collection_name=COLLECTION_NAME, points=[point])
    logger.info(f"Stored embedding for lead: {lead_metadata.get('email', 'unknown')}")
