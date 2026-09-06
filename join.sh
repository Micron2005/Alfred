#!/bin/bash
# Run this on a laptop or any other machine: it finds the desktop on the
# network by itself and appears in Alfred's page as a new machine. Give it a
# job there. Optional: ./join.sh nats://DESKTOP-IP:4222
# To have it start on boot instead, run ./install.sh worker once.
cd "$(dirname "$0")"
exec python3 run_node.py ${1:+--bus "$1"}
