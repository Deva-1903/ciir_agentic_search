# AgenticSearch container for Google Cloud Run (or any container host).
# Runs the existing FastAPI app unchanged. Cloud Run injects $PORT.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf

# Runtime libs torch/lxml need on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only torch FIRST so sentence-transformers doesn't pull the huge
# CUDA build (keeps the image ~1.5GB instead of ~5GB).
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the cross-encoder into the image so cold starts don't download it.
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2', max_length=512)"

# App code (pipeline unchanged). BASE_DIR resolves to /app at runtime.
COPY app ./app
COPY templates ./templates
COPY static ./static

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
