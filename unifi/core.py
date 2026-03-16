import asyncio
import logging
import os
import re
import ssl

import backoff
import websockets


class RetryableError(Exception):
    """Exception for retryable errors that should trigger reconnection."""
    pass


class PermanentError(Exception):
    """Exception for non-retryable errors that should stop execution."""
    pass


def get_ssl_context(cert_path: str) -> ssl.SSLContext:
    """
    Create an SSL context for requests.
    
    Respects the UNIFI_VERIFY_SSL environment variable:
    - UNIFI_VERIFY_SSL=true (default): Verify SSL certificates
    - UNIFI_VERIFY_SSL=false: Disable SSL verification for self-signed certs
    
    Args:
        cert_path: Path to the client certificate file
        
    Returns:
        Configured SSL context
    """
    verify_ssl = os.environ.get("UNIFI_VERIFY_SSL", "true").lower() == "true"
    
    ssl_context = ssl.create_default_context()
    ssl_context.load_cert_chain(cert_path, cert_path)
    
    if not verify_ssl:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    
    return ssl_context


def sanitize_url(url: str) -> str:
    """
    Redact credentials from URLs before logging.
    
    Handles RTSP, HTTP, HTTPS URLs with user:password@host format.
    Replaces password with '****' while preserving username.
    
    Args:
        url: The URL to sanitize
        
    Returns:
        URL with password redacted
        
    Examples:
        >>> sanitize_url("rtsp://admin:secret@192.168.1.1:554/stream")
        'rtsp://admin:****@192.168.1.1:554/stream'
        >>> sanitize_url("http://user:pass@example.com/api?password=secret")
        'http://user:****@example.com/api?password=****'
    """
    # Pattern to match password in URL credentials (user:pass@host)
    # Matches both RTSP and HTTP(S) URLs
    url = re.sub(
        r'(rtsp|https?|rtmp)://([^:]+):([^@]+)@',
        r'\1://\2:****@',
        url
    )
    
    # Also redact password in query parameters
    url = re.sub(
        r'([?&])(password|pass|pwd)=([^&]+)',
        r'\1\2=****',
        url,
        flags=re.IGNORECASE
    )
    
    return url


class Core(object):
    """
    Core WebSocket manager for UniFi Protect connection.
    
    Handles:
    - WebSocket connection with exponential backoff
    - Graceful error handling and reconnection
    - Proper cleanup on shutdown
    """
    
    def __init__(self, args, camera, logger):
        self.host = args.host
        self.token = args.token
        self.mac = args.mac
        self.logger = logger
        self.cam = camera

        # Set up ssl context for requests (configurable via UNIFI_VERIFY_SSL)
        self.ssl_context = get_ssl_context(args.cert)
        
        # Shutdown flag for graceful termination
        self._shutdown_requested = False

    def request_shutdown(self):
        """Request a graceful shutdown of the connection."""
        self._shutdown_requested = True
        self.logger.info("Shutdown requested")

    async def run(self) -> None:
        """
        Main run loop with improved error handling.
        
        Handles:
        - WebSocket connection with exponential backoff
        - Specific exception types with appropriate responses
        - Graceful shutdown
        """
        if self._shutdown_requested:
            self.logger.info("Shutdown requested, not starting connection")
            return
            
        uri = "wss://{}:7442/camera/1.0/ws?token={}".format(self.host, self.token)
        headers = {"camera-mac": self.mac}
        has_connected = False

        @backoff.on_predicate(
            backoff.expo,
            lambda retryable: retryable and not self._shutdown_requested,
            factor=2,
            jitter=None,
            max_value=10,
            logger=self.logger,
        )
        async def connect():
            nonlocal has_connected
            
            if self._shutdown_requested:
                self.logger.info("Shutdown requested, stopping reconnection")
                return False
                
            self.logger.info(f"Creating ws connection to {sanitize_url(uri)}")
            try:
                ws = await websockets.connect(
                    uri,
                    extra_headers=headers,
                    ssl=self.ssl_context,
                    subprotocols=["secure_transfer"],
                )
                has_connected = True
            except websockets.exceptions.InvalidStatusCode as e:
                if e.status_code == 403:
                    self.logger.error(
                        f"The token '{self.token}'"
                        " is invalid. Please generate a new one and try again."
                    )
                    raise PermanentError(f"Invalid token: {e}")
                # Hitting rate-limiting
                elif e.status_code == 429:
                    self.logger.warning("Rate limited, backing off")
                    return True
                raise
            except asyncio.exceptions.TimeoutError:
                self.logger.info(f"Connection to {self.host} timed out.")
                return True
            except ConnectionRefusedError:
                self.logger.info(f"Connection to {self.host} refused.")
                return True
            except ssl.SSLError as e:
                self.logger.error(f"SSL error connecting to {self.host}: {e}")
                raise PermanentError(f"SSL error: {e}")
            except OSError as e:
                self.logger.error(f"OS error connecting to {self.host}: {e}")
                return True
            except Exception as e:
                self.logger.error(f"Unexpected error connecting to {self.host}: {e}")
                raise

            tasks = [
                asyncio.create_task(self.cam._run(ws)),
                asyncio.create_task(self.cam.run()),
            ]
            try:
                await asyncio.gather(*tasks)
            except RetryableError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                return True
            except asyncio.CancelledError:
                self.logger.info("Core tasks cancelled")
                raise
            except Exception as e:
                self.logger.error(f"Unexpected error in main loop: {e}")
                raise
            finally:
                await self.cam.close()

        await connect()
