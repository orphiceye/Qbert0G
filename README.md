# Qbert0G gRPC QRNG Service

High-performance gRPC service for quantum random number generation using Crypta Labs devices (Firefly and QCicada).

## Features

- **High Performance**: gRPC with Protocol Buffers for efficient binary data transfer
- **Low Latency**: Minimal overhead — raw bytes over HTTP/2
- **API Key Management**: Per-key rate limiting, daily byte limits, per-request byte limits
- **Device Failover**: Automatic fallback to available devices
- **Usage Tracking**: Per-key request and byte counts in SQLite

## Architecture

```
Client (Python/Go/Java/etc.)
    ↓ gRPC (HTTP/2 + Protobuf)
gRPC Server (port 50051)
    ↓
Device Manager (thread-safe)
    ↓
Quantum Devices (Firefly/QCicada)
```

## Quick Start

### 1. Installation

```bash
cd /home/qbert/service/qrng-grpc

# Install dependencies
pip install -r requirements.txt

# Install pyqcc (obtain wheel from Crypta Labs)
pip install /path/to/pyqcc-x.y.z-py3-none-any.whl
```

### 2. Generate Protobuf Code

```bash
make proto
```

This creates:
- `proto/qrng_pb2.py` - Message classes
- `proto/qrng_pb2_grpc.py` - Service stubs

### 3. Configuration

```bash
# Copy example config
cp config.yaml.example config.yaml

# Edit config.yaml
# - Set admin_api_key for bootstrap admin
# - Configure your devices (paths, types, modes)
# - Adjust service settings (port, limits, etc.)
```

### 4. Run the Service

```bash
python run.py
```

The server will:
1. Initialize the database (`qrng_grpc.db`)
2. Connect to configured QRNG devices
3. Start listening on port 50051

## API

### Protocol Buffer Definition

See [proto/qrng.proto](proto/qrng.proto) for the complete definition.

**Request (`RandomRequest`):**
- `num_bytes` (`uint32`): Number of raw bytes to return
- API key: passed via gRPC metadata key `api-key` (not in the message body)

**Response (`RandomResponse`):**
- `data` (`bytes`): Raw quantum random bytes
- `timestamp` (`uint64`): Server timestamp (Unix epoch, microseconds)
- `device_id` (`string`): Device that served the request

### Python Client Example

```python
import grpc
from proto import qrng_pb2, qrng_pb2_grpc

channel = grpc.insecure_channel('localhost:50051')
stub = qrng_pb2_grpc.QuantumRNGStub(channel)

request = qrng_pb2.RandomRequest(num_bytes=100)
metadata = [("api-key", "your-api-key-here")]

try:
    response = stub.GetRandomBytes(request, metadata=metadata)

    print(f"Received {len(response.data)} bytes")
    print(f"From device: {response.device_id}")
    print(f"Timestamp: {response.timestamp}")

    # Interpret raw bytes as needed on the client side:
    import struct
    uint8_array  = list(response.data)
    uint16_array = [struct.unpack('<H', response.data[i:i+2])[0]
                    for i in range(0, len(response.data), 2)]

except grpc.RpcError as e:
    print(f"Error: {e.code()} - {e.details()}")
```

### grpcurl Example

```bash
grpcurl -plaintext \
  -proto proto/qrng.proto \
  -H 'api-key: YOUR_API_KEY' \
  -d '{"num_bytes": 1024}' \
  localhost:50051 qrng.QuantumRNG/GetRandomBytes
```

## API Key Management

### Bootstrap Admin Key (via config.yaml)

Set `admin_api_key` in `config.yaml` before first startup:

```yaml
admin_api_key: "your-secure-bootstrap-key"
```

On first startup this creates an admin key in the database. If you change the value and restart, the new key is added as a second admin — the old one is not removed. To revoke the old key use `manage_keys.py disable` or `delete` (see below).

### Managing Keys with manage_keys.py

Use the included CLI tool to manage keys. Must be run from the project directory.

**List all keys:**
```bash
python manage_keys.py list
```

**Create a user key:**
```bash
python manage_keys.py create --name "my-client" --device firefly-1
```
The raw key is printed once at creation and cannot be retrieved again. All key details (ID, name, device, limits) are also printed.

**Create with custom limits:**
```bash
python manage_keys.py create --name "high-volume" --device firefly-1 \
  --rate-limit 500 --daily-bytes 524288000 --max-bytes 65536
```

**Create an admin key:**
```bash
python manage_keys.py create --name "ops-admin" --device "*" --admin
```

**Update limits on an existing key:**
```bash
python manage_keys.py update --id <key-id> --rate-limit 100
python manage_keys.py update --id <key-id> --max-bytes 4096 --daily-bytes 10485760
python manage_keys.py update --id <key-id> --device firefly-2
```

**Disable / re-enable a key:**
```bash
python manage_keys.py disable --id <key-id>
python manage_keys.py enable --id <key-id>
```

**View usage stats:**
```bash
python manage_keys.py usage --id <key-id>
python manage_keys.py usage --id <key-id> --days 30
```

**Delete a key** (prompts for confirmation):
```bash
python manage_keys.py delete --id <key-id>
```

**Available device IDs** for `--device`: `firefly-1`, `firefly-2`, or `*` (any available device).

**Per-key limits** (all optional; omit to use the service-wide config default):

| Flag | Description |
|------|-------------|
| `--rate-limit RPM` | Max requests per minute |
| `--daily-bytes BYTES` | Max bytes served per day |
| `--max-bytes BYTES` | Max bytes per individual request |

## Configuration

### Service Settings

```yaml
service:
  host: "0.0.0.0"
  port: 50051                      # gRPC port
  request_timeout: 5.0             # Device wait timeout
  max_bytes_per_request: 16384     # Default max bytes per request (overridable per key)
  database_path: "./qrng_grpc.db"
  failover_enabled: true           # Device failover
  default_rate_limit: 200          # Requests/minute
  default_daily_byte_limit: 104857600  # 100 MB/day
  max_message_size: 16777216       # 16 MB max
```

### Device Configuration

```yaml
devices:
  - id: "firefly-1"
    type: "firefly"
    path: "/dev/ttyACM0"
    enabled: true
    post_processing_mode: 1  # 0=SHA256, 1=raw noise, 2=raw samples
```

## Error Handling

gRPC errors are returned as standard status codes:

| gRPC Status | Reason |
|-------------|--------|
| `UNAUTHENTICATED` | Missing or invalid API key |
| `RESOURCE_EXHAUSTED` | Rate limit or daily byte limit exceeded |
| `INVALID_ARGUMENT` | `num_bytes` is 0 or exceeds per-request limit |
| `UNAVAILABLE` | No devices available or device error |
| `DEADLINE_EXCEEDED` | Request timed out waiting for device |

## Monitoring

### Logs

The service logs to stdout:
```
2024-01-15 10:30:00 - app.server - INFO - Starting gRPC server on 0.0.0.0:50051
2024-01-15 10:30:01 - app.device_manager - INFO - Device firefly-1 connected successfully
```

### Usage Statistics

Query the database directly:
```sql
-- Today's usage
SELECT key_id, requests, bytes_served
FROM usage_records
WHERE date = '2024-01-15';

-- Top users
SELECT k.name, SUM(u.bytes_served) as total_bytes
FROM api_keys k
JOIN usage_records u ON k.id = u.key_id
GROUP BY k.id
ORDER BY total_bytes DESC;
```

## Development

### Project Structure

```
qrng-grpc/
├── proto/
│   ├── qrng.proto          # Protocol buffer definition
│   ├── qrng_pb2.py         # Generated (don't edit)
│   └── qrng_pb2_grpc.py    # Generated (don't edit)
├── app/
│   ├── __init__.py
│   ├── config.py           # Configuration management
│   ├── database.py         # API keys & usage tracking
│   ├── device_manager.py   # Device communication
│   └── server.py           # gRPC service implementation
├── example_client.py       # Example client
├── manage_keys.py          # API key management CLI
├── run.py                  # Entry point
├── config.yaml             # Configuration
├── requirements.txt        # Dependencies
└── README.md               # This file
```

### Regenerating Protobuf Code

After modifying `proto/qrng.proto`:

```bash
make proto
```

## Troubleshooting

### "No module named 'proto.qrng_pb2'"

Generate the protobuf code:
```bash
make proto
```

### "pyqcc not available"

Install pyqcc from the wheel file:
```bash
pip install /path/to/pyqcc-x.y.z-py3-none-any.whl
```

### Port 50051 already in use

Change the port in `config.yaml`:
```yaml
service:
  port: 50052  # Or any available port
```

### Device permission denied

Add user to dialout group:
```bash
sudo usermod -a -G dialout $USER
# Log out and back in
```

## License

Same as the parent QRNG service project.

## Support

For issues or questions:
1. Check device connections: `ls -l /dev/ttyACM* /dev/ttyUSB*`
2. Review logs for error messages
3. Verify config.yaml settings
