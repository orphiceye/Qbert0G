.PHONY: help proto clean install run

help:
	@echo "QRNG gRPC Service - Makefile"
	@echo ""
	@echo "Available targets:"
	@echo "  make proto    - Generate Python code from protobuf definitions"
	@echo "  make install  - Install Python dependencies"
	@echo "  make clean    - Clean generated files and caches"
	@echo "  make run      - Run the gRPC server"
	@echo "  make config   - Copy example config to config.yaml"

proto:
	@echo "Generating protobuf code..."
	python -m grpc_tools.protoc \
		-I. \
		--python_out=. \
		--grpc_python_out=. \
		proto/qrng.proto
	@echo "Done! Generated proto/qrng_pb2.py and proto/qrng_pb2_grpc.py"

install:
	@echo "Installing dependencies..."
	pip install -r requirements.txt
	@echo "Done!"
	@echo ""
	@echo "NOTE: You still need to install pyqcc manually:"
	@echo "  pip install /path/to/pyqcc-x.y.z-py3-none-any.whl"

clean:
	@echo "Cleaning generated files and caches..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	rm -rf proto/*_pb2.py proto/*_pb2_grpc.py 2>/dev/null || true
	@echo "Done!"

config:
	@if [ -f config.yaml ]; then \
		echo "config.yaml already exists. Not overwriting."; \
	else \
		cp config.yaml.example config.yaml; \
		echo "Created config.yaml from example. Please edit it!"; \
	fi

run:
	@if [ ! -f config.yaml ]; then \
		echo "ERROR: config.yaml not found. Run 'make config' first."; \
		exit 1; \
	fi
	python run.py
