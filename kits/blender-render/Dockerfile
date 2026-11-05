# syntax=docker/dockerfile:1
# ComputeCoin Kit: blender-render. Build context is the kits/ directory:
#   docker build -f kits/blender-render/Dockerfile kits
# BASE_IMAGE: CUDA runtime image pinned by digest (name@sha256:...). BLENDER_URL and BLENDER_SHA256 identify the
# official Blender Linux tarball; both are required, the download is hash-checked, and 'PENDING' fails the build.
ARG BASE_IMAGE
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS fetch
ARG BLENDER_URL=PENDING
ARG BLENDER_SHA256=PENDING
RUN test "${BLENDER_URL}" != "PENDING" && test "${BLENDER_SHA256}" != "PENDING"
RUN python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/blender.tar.xz')" "${BLENDER_URL}" \
 && echo "${BLENDER_SHA256}  /blender.tar.xz" | sha256sum -c - \
 && mkdir /blender && tar -xJf /blender.tar.xz -C /blender --strip-components=1

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
# Runtime libraries Blender needs headless (names are for Debian/Ubuntu bases; verify against the chosen base, L4).
RUN apt-get update && apt-get install -y --no-install-recommends python3 libxi6 libxxf86vm1 libxfixes3 libxrender1 \
      libgl1 libxkbcommon0 libsm6 libegl1 libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /blender /opt/blender
COPY _common/kitrt.py /app/kitrt.py
COPY blender-render/entrypoint.py /app/entrypoint.py
COPY blender-render/blender_setup.py /app/blender_setup.py
COPY blender-render/params.schema.json /app/params.schema.json
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod -R a-w /opt/blender /app
ENV BLENDER_BIN=/opt/blender/blender PARAMS_SCHEMA=/app/params.schema.json PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]
