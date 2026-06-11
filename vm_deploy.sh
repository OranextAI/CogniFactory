#!/bin/bash
# =============================================================================
# CogniFactory VM deploy: Ollama + qwen2.5vl + backend code + requirements
# Run on the VM as: sudo bash vm_deploy.sh
# =============================================================================
set -euo pipefail

APP_USER="llmuser"
APP_HOME="/home/${APP_USER}"
REPO_DIR="${APP_HOME}/CogniFactory"
BACKEND_DIR="${REPO_DIR}/backend"
VIDEOS_DIR="${REPO_DIR}/frontend/public/videos"
CONDA_ENV="cogni_env"
OLLAMA_MODEL="qwen2.5vl"
OLLAMA_BASE_URL="http://localhost:11434"
SERVICE_NAME="cognifactory"

log() { echo -e "\n\033[1;36m=== $* ===\033[0m"; }

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo bash $0"
  exit 1
fi

log "Stopping ${SERVICE_NAME} if running"
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

log "Installing system deps (ffmpeg, curl, git)"
apt update
apt install -y curl wget git build-essential ffmpeg

log "Installing Ollama (idempotent)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
else
  echo "ollama already installed: $(ollama --version || true)"
fi

log "Enabling + starting Ollama systemd service"
systemctl enable ollama
systemctl restart ollama
sleep 5

log "Pulling VLM model: ${OLLAMA_MODEL} (this takes several minutes, ~6GB)"
ollama pull "${OLLAMA_MODEL}"
ollama list

log "Pulling latest backend code"
if [ -d "${REPO_DIR}/.git" ]; then
  sudo -u "${APP_USER}" git -C "${REPO_DIR}" fetch --all
  sudo -u "${APP_USER}" git -C "${REPO_DIR}" pull --ff-only || echo "WARN: git pull not fast-forward; resolve manually"
else
  echo "WARN: ${REPO_DIR} is not a git repo. Skipping git pull."
fi

log "Installing Python requirements in conda env: ${CONDA_ENV}"
# Run pip install inside the user's conda env
sudo -u "${APP_USER}" -H bash -lc "
  source /opt/miniconda3/etc/profile.d/conda.sh 2>/dev/null \
    || source ${APP_HOME}/miniconda3/etc/profile.d/conda.sh 2>/dev/null \
    || source ${APP_HOME}/anaconda3/etc/profile.d/conda.sh 2>/dev/null \
    || { echo 'ERROR: could not find conda.sh. Edit this script with the correct path.'; exit 1; }
  conda activate ${CONDA_ENV}
  cd ${BACKEND_DIR}
  pip install --upgrade pip
  pip install -r requirements.txt
  python -c 'import flask, langchain_ollama, langchain_chroma; print(\"python deps OK\")'
"

log "Ensuring videos directory exists"
mkdir -p "${VIDEOS_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${VIDEOS_DIR}"

log "Writing backend .env (OLLAMA_BASE_URL, OLLAMA_MODEL)"
cat > "${BACKEND_DIR}/.env" <<EOF
OLLAMA_BASE_URL=${OLLAMA_BASE_URL}
OLLAMA_MODEL=${OLLAMA_MODEL}
EOF
chown "${APP_USER}:${APP_USER}" "${BACKEND_DIR}/.env"

log "Patching systemd unit to export OLLAMA_* (if unit exists)"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ -f "${UNIT_FILE}" ]; then
  # Remove any pre-existing Environment= lines for these vars, then insert new ones under [Service]
  sed -i '/^Environment=OLLAMA_BASE_URL=/d;/^Environment=OLLAMA_MODEL=/d' "${UNIT_FILE}"
  sed -i "/^\[Service\]/a Environment=OLLAMA_BASE_URL=${OLLAMA_BASE_URL}\nEnvironment=OLLAMA_MODEL=${OLLAMA_MODEL}" "${UNIT_FILE}"
  systemctl daemon-reload
  echo "Patched ${UNIT_FILE}:"
  grep -E '^(ExecStart|Environment|WorkingDirectory|User)=' "${UNIT_FILE}" || true
else
  echo "WARN: ${UNIT_FILE} not found. App.py reads os.getenv(OLLAMA_MODEL), so you must export the vars before 'python app.py'."
fi

log "Smoke test: ask Ollama for model list"
curl -fsS "${OLLAMA_BASE_URL}/api/tags" | head -c 500; echo

log "Starting ${SERVICE_NAME}"
if [ -f "${UNIT_FILE}" ]; then
  systemctl start "${SERVICE_NAME}"
  sleep 3
  systemctl --no-pager -l status "${SERVICE_NAME}" | head -25 || true
else
  echo "No systemd unit found. Launch manually with:"
  echo "  conda activate ${CONDA_ENV} && cd ${BACKEND_DIR} && OLLAMA_BASE_URL=${OLLAMA_BASE_URL} OLLAMA_MODEL=${OLLAMA_MODEL} python app.py"
fi

log "DONE. Backend should be at http://20.51.200.142:5000"
echo "Videos expected in: ${VIDEOS_DIR}"
echo "Model: ${OLLAMA_MODEL}  |  Ollama URL: ${OLLAMA_BASE_URL}"
