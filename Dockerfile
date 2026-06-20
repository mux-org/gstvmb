### BASE ###
FROM --platform=linux/amd64 ubuntu:24.04 as base

# Prevent Python from writing pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# REST API endpoint
ENV HOST=0.0.0.0
ENV PORT=8101

# Per-instance camera config (YAML). One container serves one camera; mount
# the file here at runtime (see config.example.yaml). Startup fails fast if
# it is missing or invalid.
ENV CONFIG_FILE=/app/config.yaml

WORKDIR /app
RUN touch ${CONFIG_FILE}

RUN apt-get update && apt-get install -y \
    # Python
    python3.12 python3.12-dev python3.12-venv python3-pip \
    # GStreamer core
    libgstreamer1.0-0 \
    libgstreamer1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    # GStreamer Python bindings
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    python3-gi \
    python3-gi-cairo \
    # Networking / RTP / WebRTC extras
    gstreamer1.0-nice \
    gstreamer1.0-rtsp \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
# --system-site-packages lets the venv see apt-installed Python bindings
# (notably python3-gi, which provides the `gi` module used by pipeline.py).
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN rm requirements.txt

# Install VimbaX
ENV GENICAM_GENTL64_PATH=/app/vimbax/cti
ARG VIMBAX_TAR
COPY ${VIMBAX_TAR} .
RUN mkdir vimbax \
    && tar -xf ${VIMBAX_TAR} -C vimbax --strip-components=1 \
    && rm ${VIMBAX_TAR}
RUN echo "/app/vimbax/api/lib" > /etc/ld.so.conf.d/vimbax.conf
RUN ldconfig -v

# Install/configure libgstvmbsrc
ENV GST_PLUGIN_PATH=/app/gst_plugins
ARG LIBGSTVMBSRC
RUN mkdir gst_plugins
COPY ${LIBGSTVMBSRC} gst_plugins/libgstvmbsrc.so

# Symlink camera simulator XML file if one is provided
# CAMSIM_XML defaults to Dockerfile since it's guaranteed to be there
ARG CAMSIM_XML=Dockerfile
COPY ${CAMSIM_XML} VimbaCameraSimulatorTL.xml
RUN if [ "$CAMSIM_XML" != "Dockerfile" ] && [ -n "$CAMSIM_XML" ]; then \
        mv vimbax/cti/VimbaCameraSimulatorTL.xml vimbax/cti/VimbaCameraSimulatorTL.xml.orig; \
        mv VimbaCameraSimulatorTL.xml vimbax/cti/VimbaCameraSimulatorTL.xml; \
    else \
        rm VimbaCameraSimulatorTL.xml; \
    fi

# Install app
COPY app app

# No camera config is baked in — mount $CONFIG_FILE at runtime so one image
# serves any camera. See config.example.yaml for the schema.

### DEV ###
FROM base as dev

RUN apt-get update && apt-get install -y \
    # Dev helpers
    vim \
    iproute2 \
    ethtool \
    # Useful utilities
    gstreamer1.0-libav \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

    # Install vmbpy
RUN pip install --no-cache-dir vimbax/api/python/vmbpy-1.2.1-py3-none-manylinux_2_27_x86_64.whl



### PROD ###
FROM base as prod

# Trim the VimbaX tree but keep api/lib — libgstvmbsrc.so links against
# libVmbC.so at runtime via /etc/ld.so.conf.d/vimbax.conf.
RUN rm -f vimbax/README.txt \
    && rm -rf vimbax/doc \
    && rm -rf vimbax/api/dotnet \
    && rm -rf vimbax/api/examples \
    && rm -rf vimbax/api/python \
    && rm -rf vimbax/api/source

# Single worker: VmbPipeline owns the camera in-process via app.state, so
# multiple workers would each hold their own pipeline and fight over the
# device. To scale, move pipeline ownership to a dedicated process the API
# talks to (e.g. over a socket), then increase --workers.
CMD ["sh", "-c", \
     "exec gunicorn \
      --workers 1 \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind $HOST:$PORT \
      --forwarded-allow-ips '*' \
      app.main:app"]
