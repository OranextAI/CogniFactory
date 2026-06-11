#!/bin/bash

# =============================================================================
# Ollama + Qwen3-VL Installation Script for Azure VM
# =============================================================================
# This script installs Ollama and pulls the qwen3-vl vision model
# Run: sudo bash deploy_ollama_vlm.sh
# =============================================================================

set -e

echo "=========================================="
echo "🚀 Installing Ollama + Qwen3-VL on VM"
echo "=========================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "⚠️  Please run as root: sudo bash $0"
    exit 1
fi

# Update system
echo "📦 Updating system packages..."
apt update && apt upgrade -y

# Install required dependencies
echo "📦 Installing dependencies (curl, wget, etc.)..."
apt install -y curl wget git build-essential

# Install Ollama
echo "🤖 Installing Ollama..."
curl -fsSL https://ollama.com/install.sh | sh

# Start Ollama service
echo "▶️  Starting Ollama service..."
systemctl enable ollama
systemctl start ollama

# Wait for Ollama to be ready
echo "⏳ Waiting for Ollama to start..."
sleep 5

# Pull qwen3-vl model (Vision-Language Model)
echo "📥 Pulling qwen3-vl model (this may take a few minutes)..."
ollama pull qwen2.5vl

# Verify installation
echo "✅ Verifying installation..."
ollama list

# Create environment file for the backend
echo "📝 Creating environment configuration..."
cat > /opt/backend/ollama.env << 'EOF'
# Ollama Configuration for Azure VM
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5vl
EOF

echo "=========================================="
echo "✅ Ollama + Qwen3-VL installed successfully!"
echo "=========================================="
echo ""
echo "📋 Configuration:"
echo "   - Ollama URL: http://localhost:11434"
echo "   - Model: qwen2.5vl"
echo ""
echo "🔧 To use with your backend, set environment variables:"
echo "   export OLLAMA_BASE_URL=http://localhost:11434"
echo "   export OLLAMA_MODEL=qwen2.5vl"
echo ""
echo "📝 To test the model:"
echo "   ollama run qwen2.5vl"
echo ""