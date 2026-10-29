ARG BASE_IMAGE=stale-file-controller:test
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends mergerfs && rm -rf /var/lib/apt/lists/*
COPY target.py /target.py
CMD ["python", "/target.py"]
