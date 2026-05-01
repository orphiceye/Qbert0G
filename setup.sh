#!/bin/bash
# Setup script for QRNG gRPC Service

set -e

echo "========================================="
echo "QRNG gRPC Service - Setup"
echo "========================================="
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 is not installed"
    exit 1
fi

echo "1. Installing Python dependencies..."
pip install -r requirements.txt
echo "✓ Dependencies installed"
echo ""

echo "2. Generating protobuf code..."
python -m grpc_tools.protoc \
    -I. \
    --python_out=. \
    --grpc_python_out=. \
    proto/qrng.proto
echo "✓ Generated proto/qrng_pb2.py and proto/qrng_pb2_grpc.py"
echo ""

echo "3. Creating configuration file..."
if [ -f config.yaml ]; then
    echo "  config.yaml already exists (not overwriting)"
else
    cp config.yaml.example config.yaml
    echo "✓ Created config.yaml from example"
    echo ""
    echo "  IMPORTANT: Edit config.yaml before running:"
    echo "  - Set admin_api_key"
    echo "  - Configure your devices"
fi
echo ""

echo "========================================="
echo "Setup Complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Install pyqcc (obtain from Crypta Labs):"
echo "   pip install /path/to/pyqcc-x.y.z-py3-none-any.whl"
echo ""
echo "2. Edit config.yaml:"
echo "   - Set admin_api_key for bootstrap admin"
echo "   - Configure your device paths"
echo ""
echo "3. Run the server:"
echo "   python run.py"
echo ""
echo "4. Test with example client:"
echo "   python example_client.py"
echo ""
echo "See README.md for detailed documentation."
echo ""
