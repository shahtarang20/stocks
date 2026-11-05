# syntax=docker/dockerfile:1
# ComputeCoin Kit: llm-serve (U11 Service replica). Build context is the kits/ directory:
#   docker build -f kits/llm-serve/Dockerfile kits
# BASE_IMAGE: CUDA image with PyTorch (a vLLM-compatible one) pinned by digest. FETCH_IMAGE: python image pinned by digest,
# used only to download and unpack the weights archive. ASSET_URL / ASSET_SHA256: tar of one Hugging Face model directory
# (config, tokenizer, safetensors), extracted under /weights/model. Hashes are checked at build time and again at start
# (kitrt.verify_manifest); 'PENDING' fails the build.
ARG BASE_IMAGE
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS fetch
ARG ASSET_URL=PENDING
ARG ASSET_SHA256=PENDING
RUN test "${ASSET_URL}" != "PENDING" && test "${ASSET_SHA256}" != "PENDING"
RUN mkdir /weights \
 && python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/asset.tar')" "${ASSET_URL}" \
 && echo "${ASSET_SHA256}  /asset.tar" | sha256sum -c - \
 && tar -xf /asset.tar -C /weights --no-same-owner \
 && test -z "$(find /weights -type l)" \
 && test -f /weights/model/config.json \
 && cd /weights && find . -type f -print0 | sort -z | xargs -0 sha256sum | sed 's|  \./|  |' > /weights.sha256

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY llm-serve/requirements.txt /tmp/requirements.txt
RUN grep -v '^torch-from-base-image' /tmp/requirements.txt > /tmp/r.txt && pip install --no-cache-dir -r /tmp/r.txt && rm /tmp/requirements.txt /tmp/r.txt
COPY --from=fetch /weights /opt/weights
COPY --from=fetch /weights.sha256 /opt/weights.sha256
COPY _common/kitrt.py /app/kitrt.py
COPY llm-serve/entrypoint.py /app/entrypoint.py
COPY llm-serve/params.schema.json /app/params.schema.json
# The service agent runs the image as 65532:65532 (--user override), so everything must be world-readable, not owner-only.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod -R a+rX,a-w /opt/weights /opt/weights.sha256 /app
ENV WEIGHTS_DIR=/opt/weights WEIGHTS_MANIFEST=/opt/weights.sha256 PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1 HOME=/tmp
# Service contract: the server listens on 8000 (service port) and answers GET /health (service health_path).
EXPOSE 8000
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]
