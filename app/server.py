"""
gRPC Server implementation for QRNG Service.

Provides quantum random number generation via gRPC with the same
functionality as the FastAPI service but optimized for performance.
"""

import asyncio
import os
import signal
import time
import logging
from typing import Optional
from collections import defaultdict
from datetime import datetime, timedelta

import grpc
from grpc import aio

# Import generated protobuf code
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from proto import qrng_pb2, qrng_pb2_grpc

from .config import get_config
from .database import get_database
from .device_manager import get_device_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Simple in-memory rate limiter for gRPC requests.

    Tracks request counts per API key per minute.
    """

    def __init__(self):
        self.request_counts = defaultdict(list)
        self.lock = asyncio.Lock()

    async def check_rate_limit(self, key_id: str, limit: int) -> bool:
        """
        Check if request is within rate limit.

        Args:
            key_id: API key ID
            limit: Requests per minute allowed

        Returns:
            True if within limit, False if exceeded
        """
        async with self.lock:
            now = time.time()
            minute_ago = now - 60

            # Remove old timestamps
            self.request_counts[key_id] = [
                ts for ts in self.request_counts[key_id]
                if ts > minute_ago
            ]

            # Check if over limit
            if len(self.request_counts[key_id]) >= limit:
                return False

            # Add current request
            self.request_counts[key_id].append(now)
            return True


class QuantumRNGServicer(qrng_pb2_grpc.QuantumRNGServicer):
    """Implementation of the QuantumRNG gRPC service."""

    def __init__(self):
        self.rate_limiter = RateLimiter()

    async def GetRandomBytes(
        self,
        request: qrng_pb2.RandomRequest,
        context: grpc.aio.ServicerContext
    ) -> qrng_pb2.RandomResponse:
        """
        Handle GetRandomBytes RPC call.

        Returns quantum random bytes with metadata.
        """
        config = get_config()
        db = get_database()

        # Validate API key (passed via gRPC metadata key "api-key")
        metadata = dict(context.invocation_metadata())
        api_key = metadata.get("api-key", "")

        if not api_key:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "API key required (pass via metadata key 'api-key')"
            )

        key_info = await db.validate_api_key(api_key)
        if not key_info:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Invalid API key"
            )

        # Check rate limit
        rate_limit = key_info.get("rate_limit")
        if rate_limit is None:
            rate_limit = config.service.default_rate_limit

        if not await self.rate_limiter.check_rate_limit(key_info["id"], rate_limit):
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Rate limit exceeded. Limit: {rate_limit} requests per minute."
            )

        # Validate num_bytes
        num_bytes = request.num_bytes
        if num_bytes < 1:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "num_bytes must be at least 1"
            )

        # Enforce per-request byte limit
        max_bytes_per_request = key_info.get("max_bytes_per_request")
        if max_bytes_per_request is None:
            max_bytes_per_request = config.service.max_bytes_per_request

        if num_bytes > max_bytes_per_request:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"Request would generate {num_bytes} bytes, exceeding the limit of "
                f"{max_bytes_per_request} bytes per request"
            )

        # Check daily byte limit
        daily_byte_limit = key_info.get("daily_byte_limit")
        if daily_byte_limit is None:
            daily_byte_limit = config.service.default_daily_byte_limit

        today_usage = await db.get_usage_today(key_info["id"])
        if today_usage["bytes_served"] + num_bytes > daily_byte_limit:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Daily byte limit exceeded. Used: {today_usage['bytes_served']}, "
                f"Limit: {daily_byte_limit}"
            )

        # Read bytes from device
        device_manager = get_device_manager()
        primary_device = key_info["primary_device_id"]

        # Handle wildcard device (admin bootstrap)
        if primary_device == "*":
            if device_manager.devices:
                primary_device = list(device_manager.devices.keys())[0]
            else:
                await context.abort(
                    grpc.StatusCode.UNAVAILABLE,
                    "No devices available"
                )

        try:
            data, serving_device = await device_manager.read_bytes(
                primary_device,
                num_bytes,
                timeout=config.service.request_timeout
            )
        except TimeoutError:
            await context.abort(
                grpc.StatusCode.DEADLINE_EXCEEDED,
                "Request timed out waiting for device"
            )
        except Exception as e:
            logger.error(f"Device error: {e}")
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                f"Device error: {str(e)}"
            )

        # Record usage
        await db.record_usage(key_info["id"], num_bytes)

        # Build response
        response = qrng_pb2.RandomResponse(
            data=bytes(data),
            timestamp=int(time.time() * 1_000_000),
            device_id=serving_device,
        )

        return response


async def serve():
    """Start the gRPC server."""
    config = get_config()

    # Initialize database
    logger.info("Initializing database...")
    db = get_database()
    await db.connect()
    logger.info("Database connected")

    # Initialize device manager
    logger.info("Initializing devices...")
    device_manager = get_device_manager()
    await device_manager.initialize()
    logger.info(f"Initialized {len(device_manager.devices)} devices")

    # Create gRPC server
    server = aio.server(
        options=[
            ('grpc.max_send_message_length', config.service.max_message_size),
            ('grpc.max_receive_message_length', config.service.max_message_size),
        ]
    )

    # Add servicer
    qrng_pb2_grpc.add_QuantumRNGServicer_to_server(
        QuantumRNGServicer(), server
    )

    # Bind to address
    listen_addr = f"{config.service.host}:{config.service.port}"
    server.add_insecure_port(listen_addr)

    logger.info(f"Starting gRPC server on {listen_addr}")
    await server.start()
    logger.info("gRPC server started successfully")

    # Use asyncio signal handlers rather than catching KeyboardInterrupt.
    # KeyboardInterrupt can be raised mid-await inside grpc internals, leaving
    # the server in an inconsistent state and causing stop() to hang.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    await stop_event.wait()

    grace = config.service.request_timeout
    logger.info(f"Shutting down — allowing up to {grace}s for in-flight requests...")
    try:
        # stop(grace=N) stops accepting new RPCs and waits up to N seconds for
        # active handlers to finish before forcibly closing them.
        # The outer wait_for ensures we never hang if gRPC's C-core threads
        # stall internally (which caused the original shutdown hang).
        await asyncio.wait_for(server.stop(grace=grace), timeout=grace + 2.0)
        logger.info("Shutdown complete")
    except asyncio.TimeoutError:
        logger.warning("Graceful shutdown timed out, forcing exit")

    # os._exit bypasses threading._shutdown(), which would otherwise block
    # indefinitely on non-daemon gRPC/device I/O threads.
    os._exit(0)


def run():
    """Run the gRPC server."""
    asyncio.run(serve())


if __name__ == "__main__":
    run()
