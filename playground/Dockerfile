# The Accountability Playground: the x3d_mcp validator, a write-validate-repair
# loop and a sandboxed X_ITE viewer behind one web page. See docs/playground/.
#
# Build from the repository root:
#   docker build -f playground/Dockerfile -t x3d-playground .
# Run on a laptop (memory store; model calls need Vertex AI credentials):
#   docker run -p 8080:8080 -e PLAYGROUND_SESSION_KEY=dev -e PLAYGROUND_BOOTSTRAP_PASS=devpass \
#     -e PLAYGROUND_INSECURE_COOKIES=1 x3d-playground
#   PLAYGROUND_SESSION_KEY=dev python -m playground.passlink devpass   # open that path

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[playground]"

COPY playground/ ./playground/
# X_ITE is fetched at build time and checked against a pinned SHA-256.
RUN rm -rf playground/vendor && python -m playground.fetch_xite playground/vendor/x_ite

ARG VALIDATOR_COMMIT=unknown
ENV PYTHONPATH=/app/src:/app \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PLAYGROUND_VALIDATOR_COMMIT=${VALIDATOR_COMMIT}

RUN useradd --system --uid 10001 app && chown -R app /app
USER app
EXPOSE 8080
CMD ["python", "-m", "playground.app"]
