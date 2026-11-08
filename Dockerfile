FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY tools tools
COPY config config
COPY static static
COPY templates templates
COPY data/parcelco data/parcelco
RUN useradd --create-home app \
    && mkdir -p /data/models \
    && chown -R app:app /data

ENV AGENT_DB=/data/agent.db
ENV FASTEMBED_CACHE_PATH=/data/models
USER app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=4)"

# One worker keeps a single copy of the models in memory; threads serve requests.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120", "app:create_app()"]
