# =============================================================================
# slop-harness — Dataset generation harness for fine-tuning slop jockey LLM
# =============================================================================
# No GPU needed — harness only calls the LLM API
# =============================================================================

FROM python:3.11-slim

WORKDIR /harness

# Install dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Copy source
COPY . .

# Default env — override with --env or -e at runtime
ENV LLM_BASE_URL=http://localhost:1234/v1
ENV LLM_MODEL=local-model
ENV BATCH_SIZE=1000
ENV TOTAL_INTERACTIONS=100000
ENV OUTPUT_DIR=/harness/data
ENV CONCURRENT_REQUESTS=20
ENV VIBE_PROB=0.05

CMD ["python", "-m", "slop_harness.harness"]
