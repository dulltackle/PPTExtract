ARG TOOLCHAIN_PLATFORM=linux/amd64
FROM --platform=${TOOLCHAIN_PLATFORM} ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

ENV DEBIAN_FRONTEND=noninteractive
ARG UBUNTU_SNAPSHOT=20260821T000000Z

ADD --checksum=sha256:321b30ad5a1c3783cb3d73ae439f824f6d3874d76a93a62f4a984959b490aa7b \
    https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/pool/main/o/openssl/openssl_3.0.13-0ubuntu3.12_amd64.deb \
    /tmp/bootstrap-openssl.deb
ADD --checksum=sha256:6bac2a01979e210d9eac1d4d56747ec709ea60654744d66705dc3c36e7629e50 \
    https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/pool/main/c/ca-certificates/ca-certificates_20260601~24.04.1_all.deb \
    /tmp/bootstrap-ca-certificates.deb

RUN dpkg -i /tmp/bootstrap-openssl.deb /tmp/bootstrap-ca-certificates.deb \
    && rm /tmp/bootstrap-openssl.deb /tmp/bootstrap-ca-certificates.deb \
    && sed -i \
        "s|^URIs: .*|URIs: https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}|" \
        /etc/apt/sources.list.d/ubuntu.sources

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-liberation2=1:2.1.5-3 \
        fonts-noto-cjk=1:20230817+repack1-3 \
        libreoffice-core-nogui=4:24.2.7-0ubuntu0.24.04.6 \
        libreoffice-impress-nogui=4:24.2.7-0ubuntu0.24.04.6 \
        poppler-utils=24.02.0-1ubuntu9.9 \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

LABEL org.opencontainers.image.title="PPTExtract document toolchain" \
      org.opencontainers.image.version="1"
