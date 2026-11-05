# syntax=docker/dockerfile:1
# ComputeCoin Kit: whisper-transcribe. Build context is the kits/ directory:
#   docker build -f kits/whisper-transcribe/Dockerfile kits
# BASE_IMAGE: CUDA image with PyTorch (and anything the README lists) pinned by digest. FETCH_IMAGE: python image pinned by
# digest, used only to download and unpack the asset archive. ASSET_URL / ASSET_SHA256: Whisper model converted to CTranslate2 format (a tar of the model directory, for example large-v3).
# Recorded hashes are checked at build time and again at run time (kitrt.verify_manifest); 'PENDING' fails the build.
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
 && cd /weights && find . -type f -print0 | sort -z | xargs -0 sha256sum | sed 's|  \./|  |' > /weights.sha256

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY whisper-transcribe/requirements.txt /tmp/requirements.txt
RUN grep -v '^torch-from-base-image' /tmp/requirements.txt > /tmp/r.txt && pip install --no-cache-dir -r /tmp/r.txt && rm /tmp/requirements.txt /tmp/r.txt
COPY --from=fetch /weights /opt/weights
COPY --from=fetch /weights.sha256 /opt/weights.sha256
COPY _common/kitrt.py /app/kitrt.py
COPY whisper-transcribe/entrypoint.py /app/entrypoint.py
COPY whisper-transcribe/params.schema.json /app/params.schema.json
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod -R a-w /opt/weights /app
ENV WEIGHTS_DIR=/opt/weights WEIGHTS_MANIFEST=/opt/weights.sha256 PARAMS_SCHEMA=/app/params.schema.json \
    PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 CUBLAS_WORKSPACE_CONFIG=:4096:8 HF_HUB_OFFLINE=1 HOME=/tmp
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]
