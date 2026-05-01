"""
Configuration management for QRNG gRPC Service.

Loads settings from a YAML configuration file.
"""

import os
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
import yaml


@dataclass
class DeviceConfig:
    """Configuration for a single QRNG device."""
    id: str
    type: str  # "firefly" or "qcicada"
    path: str  # e.g., "/dev/ttyACM0" for Firefly, "/dev/ttyUSB0" for QCicada
    baud_rate: int = 115200
    timeout: float = 5.0
    enabled: bool = True
    post_processing_mode: int = 0        # 0=enabled (SHA256), 1=raw noise, 2=raw samples
    streaming_mode: bool = False         # True = keep device in continuous mode permanently
    streaming_idle_timeout: float = 0.0  # Minutes idle before stopping stream (0 = disabled)


@dataclass
class ServiceConfig:
    """Service-level configuration."""
    host: str = "0.0.0.0"
    port: int = 50051  # Default gRPC port
    request_timeout: float = 5.0
    max_bytes_per_request: int = 16384
    database_path: str = "./qrng_grpc.db"  # Separate database for gRPC service
    failover_enabled: bool = True
    default_rate_limit: int = 200              # requests per minute per API key
    default_daily_byte_limit: int = 104857600  # 100 MB in bytes
    max_message_size: int = 16777216           # 16 MB max message size


@dataclass
class Config:
    """Root configuration object."""
    service: ServiceConfig = field(default_factory=ServiceConfig)
    devices: list[DeviceConfig] = field(default_factory=list)
    admin_api_key: Optional[str] = None  # Bootstrap admin key

    @classmethod
    def load(cls, config_path: str = None) -> "Config":
        """Load configuration from YAML file."""
        if config_path is None:
            config_path = os.environ.get("QRNG_GRPC_CONFIG", "config.yaml")

        path = Path(config_path)
        if not path.exists():
            # Return default config if file doesn't exist
            return cls()

        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Create Config from dictionary."""
        service_data = data.get("service", {})
        service = ServiceConfig(
            host=service_data.get("host", "0.0.0.0"),
            port=service_data.get("port", 50051),
            request_timeout=service_data.get("request_timeout", 5.0),
            max_bytes_per_request=service_data.get("max_bytes_per_request", 16384),
            database_path=service_data.get("database_path", "./qrng_grpc.db"),
            failover_enabled=service_data.get("failover_enabled", True),
            default_rate_limit=service_data.get("default_rate_limit", 200),
            default_daily_byte_limit=service_data.get("default_daily_byte_limit", 104857600),
            max_message_size=service_data.get("max_message_size", 16777216),
        )

        devices = []
        for dev_data in data.get("devices", []):
            # Validate post_processing_mode if provided
            post_proc_mode = dev_data.get("post_processing_mode", 0)
            if post_proc_mode not in (0, 1, 2):
                raise ValueError(
                    f"Device {dev_data['id']}: post_processing_mode must be 0, 1, or 2. "
                    f"Got: {post_proc_mode}"
                )

            devices.append(DeviceConfig(
                id=dev_data["id"],
                type=dev_data["type"],
                path=dev_data["path"],
                baud_rate=dev_data.get("baud_rate", 115200),
                timeout=dev_data.get("timeout", 5.0),
                enabled=dev_data.get("enabled", True),
                post_processing_mode=post_proc_mode,
                streaming_mode=dev_data.get("streaming_mode", False),
                streaming_idle_timeout=dev_data.get("streaming_idle_timeout", 0.0),
            ))

        return cls(
            service=service,
            devices=devices,
            admin_api_key=data.get("admin_api_key"),
        )


# Global config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def reload_config(config_path: str = None) -> Config:
    """Reload configuration from file."""
    global _config
    _config = Config.load(config_path)
    return _config
