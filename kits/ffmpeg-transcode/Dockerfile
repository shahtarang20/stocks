# syntax=docker/dockerfile:1
# ComputeCoin Kit: ffmpeg-transcode. Build context is the kits/ directory:
#   docker build -f kits/ffmpeg-transcode/Dockerfile kits
# BASE_IMAGE: a minimal Linux image pinned by digest (name@sha256:...) with python3. FFMPEG_URL / FFMPEG_SHA256 name a
# static ffmpeg build tarball (contains ffmpeg and ffprobe). OWNER DECISION: GPL build (libx264/libx265 CPU fallback)
# or LGPL build (NVENC only, no CPU fallback); see README. 'PENDING' fails the build.
ARG BASE_IMAGE
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS fetch
ARG FFMPEG_URL=PENDING
ARG FFMPEG_SHA256=PENDING
RUN test "${FFMPEG_URL}" != "PENDING" && test "${FFMPEG_SHA256}" != "PENDING"
RUN python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/ffmpeg.tar.xz')" "${FFMPEG_URL}" \
 && echo "${FFMPEG_SHA256}  /ffmpeg.tar.xz" | sha256sum -c - \
 && mkdir /ff && tar -xJf /ffmpeg.tar.xz -C /ff --strip-components=1 \
 && mkdir /ffbin && cp /ff/bin/ffmpeg /ff/bin/ffprobe /ffbin/ \
 && cp /ff/LICENSE* /ff/GPL* /ff/LGPL* /ffbin/ 2>/dev/null || true

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY --from=fetch /ffbin /opt/ffmpeg
COPY _common/kitrt.py /app/kitrt.py
COPY ffmpeg-transcode/entrypoint.py /app/entrypoint.py
COPY ffmpeg-transcode/params.schema.json /app/params.schema.json
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod -R a-w /opt/ffmpeg /app
# The NVIDIA container runtime injects the NVENC library when the video capability is requested.
ENV FFMPEG_BIN=/opt/ffmpeg/ffmpeg FFPROBE_BIN=/opt/ffmpeg/ffprobe PARAMS_SCHEMA=/app/params.schema.json \
    PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]
