FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

LABEL org.opencontainers.image.title="Synthetic TLF Review Demo" \
      org.opencontainers.image.description="Loopback-oriented fictional portfolio demonstration"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

COPY requirements-demo-lock.txt ./
RUN python -m pip install --no-deps --only-binary=:all: \
    --requirement requirements-demo-lock.txt \
    && python -m pip check

COPY *.py ./
COPY static ./static
COPY demo ./demo
COPY evaluation ./evaluation
COPY assets ./assets
COPY configs/study_config.synthetic.json ./study_config.json

RUN addgroup --system app \
    && adduser --system --ingroup app --home /app app \
    && mkdir -p /app/data /app/demo-data \
    && chown -R app:app /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/projects', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
