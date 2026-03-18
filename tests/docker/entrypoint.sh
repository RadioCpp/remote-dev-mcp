#!/bin/sh
set -eu

mkdir -p /home/dev/.ssh /run/sshd
chmod 700 /home/dev/.ssh

if [ -n "${AUTHORIZED_KEY:-}" ]; then
  printf '%s\n' "$AUTHORIZED_KEY" > /home/dev/.ssh/authorized_keys
fi

chmod 600 /home/dev/.ssh/authorized_keys
chown -R dev:dev /home/dev/.ssh

ssh-keygen -A

exec /usr/sbin/sshd -D -e
