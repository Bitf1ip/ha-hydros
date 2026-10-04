#!/usr/bin/env bash
set -euo pipefail

cd "/workspaces/ha-hydros"

if [ -d /config ] && [ ! -w /config ]; then
  sudo chown -R "$(id -u)":"$(id -g)" /config
fi

export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if command -v uv >/dev/null 2>&1; then
  if [ ! -d .venv ] || [ ! -x .venv/bin/python ]; then
    rm -rf .venv
    uv venv
  fi
  source .venv/bin/activate
  uv pip install --upgrade pip
  INSTALL_CMD=(uv pip install)
else
  VENV_PYTHON="$(pwd)/.venv/bin/python"
  if [ ! -d .venv ] || [ ! -x "$VENV_PYTHON" ]; then
    rm -rf .venv
    python3 -m venv .venv
  fi
  VENV_PYTHON="$(pwd)/.venv/bin/python"
  if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtual environment missing python interpreter" >&2
    exit 1
  fi
  source .venv/bin/activate
  "$VENV_PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$VENV_PYTHON" -m pip install --upgrade pip
  INSTALL_CMD=("$VENV_PYTHON" -m pip install)
fi

if [ -f requirements.txt ]; then
  "${INSTALL_CMD[@]}" -r requirements.txt
fi

if [ -f requirements_dev.txt ]; then
  "${INSTALL_CMD[@]}" -r requirements_dev.txt
fi


hass --script ensure_config -c /config
