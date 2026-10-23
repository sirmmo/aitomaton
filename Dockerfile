# aitomaton — standard library only, so the image is just Python and the code.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 AITOMATON_DB=/state/aitomaton.db AITOMATON_STATIC=/app/static
# git: aitomaton clones the service's repo into the stairway experiment with the service's own credential
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY aitomaton /app/aitomaton
COPY static /app/static
VOLUME /state
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8790/health')"
CMD ["python", "-m", "aitomaton"]
