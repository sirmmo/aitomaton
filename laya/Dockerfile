# aitomaton-laya — the System-1 decision tier (same recipe as /srv/laya-reflex).
# CPU torch wheels: the default PyPI torch drags CUDA, ~2.5GB for nothing here.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HF_HOME=/cache
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir laya fastapi uvicorn
COPY app.py /app/app.py
WORKDIR /app
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
