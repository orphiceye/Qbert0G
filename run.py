#!/usr/bin/env python3
"""
Entry point for QRNG gRPC Service.

Usage:
    python run.py

Environment variables:
    QRNG_GRPC_CONFIG - Path to config.yaml (default: ./config.yaml)
"""

import sys
import logging
from pathlib import Path

# Add the project root to Python path
sys.path.insert(0, str(Path(__file__).parent))

from app.server import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
