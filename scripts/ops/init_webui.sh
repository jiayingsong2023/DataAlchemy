#!/bin/bash
# Initialize WebUI: Generate SSL certificate and user database

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "========================================"
echo "DataAlchemy WebUI Initialization"
echo "========================================"

# 1. Generate self-signed SSL certificate
echo ""
echo "📜 Generating self-signed SSL certificate..."
cd "$PROJECT_DIR/webui"
uv run python generate_cert.py

# 2. Initialize user database
echo ""
echo "👤 Initializing user database..."
cd "$PROJECT_DIR"
uv run python -c "
import sys
sys.path.insert(0, 'src')
from utils.user_db import init_user_db
init_user_db()
print('✅ User database initialized')
print('   Default user: admin / admin123')
"

echo ""
echo "========================================"
echo "✅ Initialization complete!"
echo ""
echo "Start WebUI with:"
echo "  cd $PROJECT_DIR/webui && uv run python app.py"
echo ""
echo "Access: https://localhost:8443"
echo "Login:  admin / admin123"
echo "========================================"
