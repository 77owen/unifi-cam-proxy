"""
UniFi Camera Proxy - Camera implementations.

This package contains camera implementations for the unifi-cam-proxy.
Currently supports only the Reolink RLC-410-5MP camera.
"""

from unifi.cams.rlc410 import RLC410Camera

__all__ = ["RLC410Camera"]
