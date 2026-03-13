"""
UniFi Camera Proxy - Main entry point.

A simplified proxy for connecting Reolink RLC-410-5MP cameras to UniFi Protect.
"""

import argparse
import asyncio
import logging
import sys
from shutil import which

import coloredlogs
from pyunifiprotect import ProtectApiClient

from unifi.cams import RLC410Camera
from unifi.core import Core
from unifi.version import __version__


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="UniFi Camera Proxy for Reolink RLC-410-5MP"
    )
    parser.add_argument("--version", action="version", version=__version__)
    
    # Connection arguments
    parser.add_argument(
        "--host", "-H", required=True, 
        help="UniFi Protect NVR IP address and port (e.g., 192.168.1.1:7442)"
    )
    parser.add_argument(
        "--nvr-username", 
        required=False, 
        help="UniFi Protect NVR username (for automatic token generation)"
    )
    parser.add_argument(
        "--nvr-password", 
        required=False, 
        help="UniFi Protect NVR password (for automatic token generation)"
    )
    
    # Certificate and authentication
    parser.add_argument(
        "--cert", "-c", 
        required=True,
        default="client.pem",
        help="Client certificate path (default: client.pem)"
    )
    parser.add_argument(
        "--token", "-t", 
        required=False, 
        default=None,
        help="Adoption token (will be generated automatically if NVR credentials provided)"
    )
    
    # Camera identity
    parser.add_argument(
        "--mac", "-m", 
        default="AABBCCDDEEFF", 
        help="MAC address for this camera (default: AABBCCDDEEFF)"
    )
    parser.add_argument(
        "--ip", "-i",
        default="192.168.1.10",
        help="IP address of camera as displayed in UniFi Protect UI (default: 192.168.1.10)"
    )
    parser.add_argument(
        "--name", "-n",
        default="RLC-410-5MP",
        help="Name of camera in UniFi Protect (default: RLC-410-5MP)"
    )
    parser.add_argument(
        "--model",
        default="UVC G3",
        choices=[
            "UVC",
            "UVC AI 360",
            "UVC AI Bullet",
            "UVC AI THETA",
            "UVC AI DSLR",
            "UVC Pro",
            "UVC Dome",
            "UVC Micro",
            "UVC G3",
            "UVC G3 Battery",
            "UVC G3 Dome",
            "UVC G3 Micro",
            "UVC G3 Mini",
            "UVC G3 Instant",
            "UVC G3 Pro",
            "UVC G3 Flex",
            "UVC G4 Bullet",
            "UVC G4 Pro",
            "UVC G4 PTZ",
            "UVC G4 Doorbell",
            "UVC G4 Doorbell Pro",
            "UVC G4 Doorbell Pro PoE",
            "UVC G4 Dome",
            "UVC G4 Instant",
            "UVC G5 Bullet",
            "UVC G5 Dome",
            "UVC G5 Flex",
            "UVC G5 Pro",
            "AFi VC",
            "Vision Pro",
        ],
        help="Hardware model to identify as in UniFi Protect (default: UVC G3)"
    )
    parser.add_argument(
        "--fw-version", "-f",
        default="UVC.S2L.v4.23.8.67.0eba6e3.200526.1046",
        help="Firmware version to initiate connection with"
    )
    
    # Logging
    parser.add_argument(
        "--verbose", "-v", 
        action="store_true", 
        help="Enable debug logging"
    )

    # Add RLC-410 specific arguments
    RLC410Camera.add_parser(parser)
    
    return parser.parse_args()


async def generate_token(args, logger):
    """Generate an adoption token from UniFi Protect NVR."""
    try:
        protect = ProtectApiClient(
            args.host, 443, args.nvr_username, args.nvr_password, verify_ssl=False
        )
        await protect.update()
        response = await protect.api_request("cameras/manage-payload")
        return response["mgmt"]["token"]
    except Exception:
        logger.exception(
            "Could not automatically fetch token. "
            "Please provide --token or ensure --nvr-username and --nvr-password are correct."
        )
        return None
    finally:
        await protect.close_session()


async def run():
    """Main async entry point."""
    args = parse_args()

    core_logger = logging.getLogger("Core")
    class_logger = logging.getLogger("RLC410Camera")

    level = logging.INFO
    if args.verbose:
        level = logging.DEBUG

    for logger in [core_logger, class_logger]:
        coloredlogs.install(level=level, logger=logger)

    # Preflight checks
    for binary in ["ffmpeg", "nc"]:
        if which(binary) is None:
            core_logger.error(f"{binary} is not installed")
            sys.exit(1)

    # Generate token if not provided
    if not args.token:
        args.token = await generate_token(args, core_logger)

    if not args.token:
        core_logger.error("A valid token is required")
        sys.exit(1)

    # Create camera instance and start
    cam = RLC410Camera(args, class_logger)
    c = Core(args, cam, core_logger)
    await c.run()


def main():
    """Main entry point."""
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run())


if __name__ == "__main__":
    main()
