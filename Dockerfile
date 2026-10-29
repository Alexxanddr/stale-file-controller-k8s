FROM python:3.13-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/Alexxanddr/stale-file-controller-k8s"
RUN apt-get update && apt-get install -y --no-install-recommends nfs-common util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY stale ./stale
RUN pip install --no-cache-dir .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["stale-file-controller"]
