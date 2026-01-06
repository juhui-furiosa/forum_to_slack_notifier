#!/bin/bash
set -euo pipefail

cd /home/aurum/slack_notifier

set -a
source .env
set +a

./venv/bin/python main.py # 주소변경 필요할수도
