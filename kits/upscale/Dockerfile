# syntax=docker/dockerfile:1
# ComputeCoin Kit: upscale (Real-ESRGAN weights via spandrel). Build context is the kits/ directory:
#   docker build -f kits/upscale/Dockerfile kits
# BASE_IMAGE: CUDA PyTorch image pinned by digest. FETCH_IMAGE: python image pinned by digest, used to download weights.
# Weights are downloaded at build time and checked against recorded SHA-256 values; 'PENDING' fails the build.
ARG BASE_IMAGE
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS fetch
ARG X4PLUS_URL=PENDING
ARG X4PLUS_SHA256=PENDING
ARG X4PLUS_ANIME_URL=PENDING
ARG X4PLUS_ANIME_SHA256=PENDING
RUN test "${X4PLUS_URL}" != "PENDING" && test "${X4PLUS_SHA256}" != "PENDING" \
 && test "${X4PLUS_ANIME_URL}" != "PENDING" && test "${X4PLUS_ANIME_SHA256}" != "PENDING"
RUN mkdir /weights \
 && python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/weights/RealESRGAN_x4plus.pth')" "${X4PLUS_URL}" \
 && python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/weights/RealESRGAN_x4plus_anime_6B.pth')" "${X4PLUS_ANIME_URL}" \
 && echo "${X4PLUS_SHA256}  /weights/RealESRGAN_x4plus.pth" | sha256sum -c - \
 && echo "${X4PLUS_ANIME_SHA256}  /weights/RealESRGAN_x4plus_anime_6B.pth" | sha256sum -c - \
 && cd /weights && sha256sum RealESRGAN_x4plus.pth RealESRGAN_x4plus_anime_6B.pth > /weights.sha256

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY upscale/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=fetch /weights /opt/weights
COPY --from=fetch /weights.sha256 /opt/weights.sha256
COPY _common/kitrt.py /app/kitrt.py
COPY upscale/entrypoint.py /app/entrypoint.py
COPY upscale/params.schema.json /app/params.schema.json
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod -R a-w /opt/weights /app
ENV WEIGHTS_DIR=/opt/weights WEIGHTS_MANIFEST=/opt/weights.sha256 PARAMS_SCHEMA=/app/params.schema.json \
    PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 CUBLAS_WORKSPACE_CONFIG=:4096:8
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]
