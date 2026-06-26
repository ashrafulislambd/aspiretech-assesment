FROM python:3.12-slim

WORKDIR /app

# Install curl so the docker-compose healthcheck on qdrant can run
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install CPU-only PyTorch first — avoids pulling the much larger CUDA build
# that sentence-transformers would otherwise select automatically
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the embedding model into the image at build time.
# Without this, the first container startup would spend ~30 seconds downloading it.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
