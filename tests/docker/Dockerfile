FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ARG DEV_UID=1000
ARG DEV_GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        make \
        ninja-build \
        openssh-server \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${DEV_GID}" dev \
    && useradd --uid "${DEV_UID}" --gid "${DEV_GID}" --create-home --shell /bin/bash dev \
    && mkdir -p /home/dev/.ssh /run/sshd \
    && passwd -d dev \
    && chown -R dev:dev /home/dev/.ssh \
    && chmod 700 /home/dev/.ssh

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

EXPOSE 22

ENTRYPOINT ["/entrypoint.sh"]
