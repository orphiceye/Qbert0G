#!/usr/bin/env python3
"""
Example gRPC client for QRNG service.

Demonstrates how to:
- Connect to the gRPC server
- Request quantum random bytes
- Pass the API key via gRPC metadata
- Interpret raw bytes on the client side
"""

import sys
import struct
from pathlib import Path

import grpc

# Add proto directory to path
sys.path.insert(0, str(Path(__file__).parent))
from proto import qrng_pb2, qrng_pb2_grpc


def request_bytes(stub, api_key: str, num_bytes: int):
    """Request raw quantum random bytes from the server."""
    print(f"\n=== Requesting {num_bytes} bytes ===")

    request = qrng_pb2.RandomRequest(num_bytes=num_bytes)
    metadata = [("api-key", api_key)]

    try:
        response = stub.GetRandomBytes(request, metadata=metadata)

        print(f"  Received {len(response.data)} bytes")
        print(f"  Device:    {response.device_id}")
        print(f"  Timestamp: {response.timestamp}")

        return response.data

    except grpc.RpcError as e:
        print(f"Error: {e.code()} - {e.details()}")
        return None


def main():
    """Main example: request bytes and show client-side interpretation."""
    SERVER_ADDRESS = "localhost:50051"
    API_KEY = "your-api-key-here"  # Change this!

    if API_KEY == "your-api-key-here":
        print("ERROR: Please set your API key in the script!")
        print("Edit example_client.py and change API_KEY variable.")
        sys.exit(1)

    print(f"Connecting to QRNG gRPC server at {SERVER_ADDRESS}...")

    with grpc.insecure_channel(SERVER_ADDRESS) as channel:
        stub = qrng_pb2_grpc.QuantumRNGStub(channel)
        print("Connected!")

        # --- uint8: one value per byte ---
        data = request_bytes(stub, API_KEY, num_bytes=10)
        if data:
            print(f"  As uint8:  {list(data)}")

        # --- uint16 little-endian: two bytes per value ---
        data = request_bytes(stub, API_KEY, num_bytes=20)
        if data:
            values = [struct.unpack('<H', data[i:i+2])[0] for i in range(0, len(data), 2)]
            print(f"  As uint16: {values}")

        # --- hex blocks: 4 bytes per block ---
        data = request_bytes(stub, API_KEY, num_bytes=20)
        if data:
            block_size = 4
            blocks = [data[i:i+block_size].hex() for i in range(0, len(data), block_size)]
            print(f"  As hex:    {blocks}")

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
