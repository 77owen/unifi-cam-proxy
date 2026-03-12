ARG version=3.9
ARG tag=${version}-slim-bookworm

FROM python:${tag} AS builder
WORKDIR /app
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true

RUN apt-get update && apt-get install -y --no-install-recommends \
        cargo \
        git \
        gcc \
        g++ \
        libjpeg-dev \
        libc6-dev \
        rustc \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install -U pip wheel setuptools maturin
COPY requirements.txt .
RUN pip install -r requirements.txt


FROM python:${tag}
WORKDIR /app

ARG version

COPY --from=builder \
        /usr/local/lib/python${version}/site-packages \
        /usr/local/lib/python${version}/site-packages

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        netcat-openbsd \
        libusb-1.0-0-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install . --no-cache-dir

COPY ./docker/entrypoint.sh /

ENTRYPOINT ["/entrypoint.sh"]
CMD ["unifi-cam-proxy"]
