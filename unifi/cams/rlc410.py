"""
UniFi Camera Proxy for Reolink RLC-410-5MP

This module provides a simplified camera implementation specifically for the
Reolink RLC-410-5MP camera, supporting:
- RTSP video streaming (main and sub streams)
- Motion detection via HTTP API polling
- Audio streaming support
"""

import argparse
import asyncio
import atexit
import json
import logging
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import aiohttp
import packaging
import reolinkapi
import websockets
from yarl import URL

from unifi.core import RetryableError, get_ssl_context, sanitize_url

AVClientRequest = AVClientResponse = dict[str, Any]


class RLC410Camera:
    """
    UniFi Camera Proxy for Reolink RLC-410-5MP.
    
    This class combines the UniFi Protect protocol implementation with
    Reolink-specific functionality for video streaming and motion detection.
    """
    
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        self.args = args
        self.logger = logger

        # Feature flag for Python streaming (default: true - Python streaming is now the default)
        # Set UNIFI_USE_PYTHON_STREAMING=false to fall back to legacy shell pipeline
        self._use_python_streaming = os.environ.get(
            "UNIFI_USE_PYTHON_STREAMING", "true"
        ).lower() in ("true", "1", "yes")

        # Protocol state
        self._msg_id: int = 0
        self._init_time: float = time.time()
        self._streams: dict[str, str] = {}
        self._motion_snapshot: Optional[Path] = None
        self._motion_event_id: int = 0
        self._motion_event_ts: Optional[float] = None
        self._ffmpeg_handles: dict[str, subprocess.Popen] = {}

        # StreamManager for Python-based streaming (when feature flag is enabled)
        # Lazy import to avoid dependency issues when feature is disabled
        self._stream_manager = None
        if self._use_python_streaming:
            try:
                from unifi.streaming import StreamManager
                self._stream_manager = StreamManager(logger=logger)
                self.logger.info("Python streaming enabled via UNIFI_USE_PYTHON_STREAMING")
            except ImportError as e:
                self.logger.warning(
                    f"Failed to import StreamManager, falling back to shell pipeline: {e}"
                )
                self._use_python_streaming = False

        # SSL context for requests (configurable via UNIFI_VERIFY_SSL)
        self._ssl_context = get_ssl_context(args.cert)
        self._session: Optional[websockets.legacy.client.WebSocketClientProtocol] = None
        atexit.register(self.close_streams)

        self._needs_flv_timestamps: bool = False

        # Reolink-specific initialization
        self.snapshot_dir: str = tempfile.mkdtemp()
        self._motion_lock = asyncio.Lock()  # Async lock for atomic motion detection operations
        self.motion_in_progress: bool = False
        self.substream = args.substream
        
        # Shutdown flag for graceful termination
        self._shutdown_requested = False
        
        # Watchdog state for stream health monitoring
        self._last_stream_activity: dict[str, float] = {}
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_interval = 30.0  # Check stream health every 30 seconds
        self._stream_timeout = 60.0  # Consider stream unhealthy after 60s of no activity
        
        # Retry configuration with exponential backoff limits
        self._max_motion_retries = 10  # Maximum retries for motion API
        self._motion_backoff_base = 1.0  # Base backoff time in seconds
        self._motion_backoff_max = 60.0  # Maximum backoff time in seconds
        
        # Initialize Reolink API connection
        self.cam = reolinkapi.Camera(
            ip=args.ip,
            username=args.username,
            password=args.password,
        )
        self.stream_fps = self._get_stream_info()

    # =========================================================================
    # Argument Parsing
    # =========================================================================
    
    @classmethod
    def add_parser(cls, parser: argparse.ArgumentParser) -> None:
        """Add RLC-410 specific arguments to the argument parser."""
        parser.add_argument(
            "--ffmpeg-args",
            default="-c:v copy -ar 32000 -ac 1 -codec:a aac -b:a 32k",
            help="Transcoding args for `ffmpeg -i <src> <args> <dst>`",
        )
        parser.add_argument(
            "--rtsp-transport",
            default="tcp",
            choices=["tcp", "udp", "http", "udp_multicast"],
            help="RTSP transport protocol used by stream",
        )
        parser.add_argument("--username", "-u", required=True, help="Camera username")
        parser.add_argument("--password", "-p", required=True, help="Camera password")
        parser.add_argument(
            "--channel",
            default=0,
            type=int,
            help="Camera channel (default: 0)",
        )
        parser.add_argument(
            "--stream",
            default="main",
            type=str,
            choices=["main", "sub"],
            help="Stream profile for higher quality stream (default: main)",
        )
        parser.add_argument(
            "--substream",
            "-s",
            default="sub",
            type=str,
            choices=["main", "sub"],
            help="Stream profile for lower quality stream (default: sub)",
        )

    # =========================================================================
    # Reolink-Specific Methods
    # =========================================================================
    
    def _get_stream_info(self) -> tuple[int, int]:
        """Get frame rate info from the camera for both streams."""
        info = self.cam.get_recording_encoding()
        return (
            info[0]["value"]["Enc"]["mainStream"]["frameRate"],
            info[0]["value"]["Enc"]["subStream"]["frameRate"],
        )

    async def get_snapshot(self) -> Path:
        """Capture a snapshot from the camera."""
        img_file = Path(self.snapshot_dir, "screen.jpg")
        url = (
            f"http://{self.args.ip}"
            f"/cgi-bin/api.cgi?cmd=Snap&channel={self.args.channel}"
            f"&rs=6PHVjvf0UntSLbyT&user={self.args.username}"
            f"&password={self.args.password}"
        )
        self.logger.info(f"Grabbing snapshot: {sanitize_url(url)}")
        await self._fetch_to_file(url, img_file)
        return img_file

    async def get_stream_source(self, stream_index: str) -> str:
        """Get the RTSP URL for the specified stream."""
        if stream_index == "video1":
            stream = self.args.stream
        else:
            stream = self.args.substream

        return (
            f"rtsp://{self.args.username}:{self.args.password}@{self.args.ip}:554"
            f"/Preview_{int(self.args.channel) + 1:02}_{stream}"
        )

    def get_extra_ffmpeg_args(self, stream_index: str) -> str:
        """Get FFmpeg arguments specific to the stream, including H264 metadata timing.
        
        Note: Audio is only available on the sub stream (Preview_01_sub).
        The main stream (Preview_01_main) has no audio track.
        """
        if stream_index == "video1":
            fps = self.stream_fps[0]
            # Main stream has no audio - only video processing
            return f'-c:v copy -vbsf "h264_metadata=tick_rate={fps*2}"'
        else:
            fps = self.stream_fps[1]
            # Sub stream has audio - include audio encoding
            return (
                "-ar 32000 -ac 1 -codec:a aac -b:a 32k -c:v copy -vbsf"
                f' "h264_metadata=tick_rate={fps*2}"'
            )

    async def run(self) -> None:
        """Run the motion detection polling loop with exponential backoff.
        
        This method polls the Reolink camera's motion detection API and triggers
        motion events in UniFi Protect. It includes:
        - Exponential backoff with maximum retry limits
        - Atomic motion state transitions to prevent race conditions
        - Graceful shutdown handling
        """
        url = (
            f"http://{self.args.ip}"
            f"/api.cgi?cmd=GetMdState&user={self.args.username}"
            f"&password={self.args.password}"
        )
        encoded_url = URL(url, encoded=True)
        body = (
            f'[{{ "cmd":"GetMdState", "param":{{ "channel":{self.args.channel} }} }}]'
        )
        
        retry_count = 0
        backoff_time = self._motion_backoff_base
        
        while not self._shutdown_requested:
            self.logger.info(f"Connecting to motion events API: {sanitize_url(url)}")
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(None)
                ) as session:
                    # Reset backoff on successful connection
                    retry_count = 0
                    backoff_time = self._motion_backoff_base
                    
                    while not self._shutdown_requested:
                        try:
                            async with session.post(encoded_url, data=body) as resp:
                                if resp.status != 200:
                                    self.logger.warning(
                                        f"Motion API returned status {resp.status}"
                                    )
                                    continue
                                    
                                data = await resp.read()

                                try:
                                    json_body = json.loads(data)
                                    if "value" in json_body[0]:
                                        state = json_body[0]["value"]["state"]
                                        # Use lock for atomic check-and-act on motion state
                                        with self._motion_lock:
                                            if state == 1 and not self.motion_in_progress:
                                                self.motion_in_progress = True
                                                self.logger.info("Trigger motion start")
                                                # Release lock before async operation
                                        if state == 1 and not self._motion_event_ts:
                                            await self.trigger_motion_start()
                                            continue
                                            
                                        with self._motion_lock:
                                            if state == 0 and self.motion_in_progress:
                                                self.motion_in_progress = False
                                                self.logger.info("Trigger motion end")
                                                
                                        if state == 0 and self._motion_event_ts:
                                            await self.trigger_motion_stop()
                                    else:
                                        self.logger.error(
                                            "Motion API request responded with "
                                            "unexpected JSON, retrying. "
                                            f"JSON: {data}"
                                        )

                                except json.JSONDecodeError as err:
                                    self.logger.error(
                                        "Motion API request returned invalid "
                                        "JSON, retrying. "
                                        f"Error: {err}, "
                                        f"Response: {data}"
                                    )
                                    
                        except aiohttp.ClientError as err:
                            # Handle per-request errors without breaking the session
                            self.logger.warning(
                                f"Motion API request failed: {err}. "
                                f"Retrying in {backoff_time:.1f}s..."
                            )
                            await asyncio.sleep(backoff_time)
                            continue
                            
            except aiohttp.ClientError as err:
                retry_count += 1
                if retry_count > self._max_motion_retries:
                    self.logger.error(
                        f"Motion API failed after {self._max_motion_retries} retries. "
                        f"Last error: {err}. Waiting {self._motion_backoff_max}s before retry."
                    )
                    # Reset retry count but wait max backoff time
                    retry_count = 0
                    backoff_time = self._motion_backoff_max
                else:
                    self.logger.error(
                        f"Motion API connection failed (attempt {retry_count}/"
                        f"{self._max_motion_retries}): {err}. "
                        f"Retrying in {backoff_time:.1f}s..."
                    )
                
                await asyncio.sleep(backoff_time)
                # Exponential backoff with cap
                backoff_time = min(backoff_time * 2, self._motion_backoff_max)
                
            except asyncio.CancelledError:
                self.logger.info("Motion detection loop cancelled")
                break
                
            except Exception as err:
                self.logger.exception(
                    f"Unexpected error in motion detection loop: {err}"
                )
                await asyncio.sleep(backoff_time)
                backoff_time = min(backoff_time * 2, self._motion_backoff_max)

    # =========================================================================
    # Motion Detection
    # =========================================================================
    
    async def trigger_motion_start(self) -> None:
        """Trigger a motion start event with atomic state management.
        
        Uses asyncio.Lock to prevent race conditions when multiple
        concurrent calls occur.
        """
        # Use async lock for atomic check-and-act pattern
        async with self._motion_lock:
            if self._motion_event_ts is not None:
                # Motion already in progress, skip
                return
            
            payload: dict[str, Any] = {
                "clockBestMonotonic": 0,
                "clockBestWall": 0,
                "clockMonotonic": int(self.get_uptime()),
                "clockStream": int(self.get_uptime()),
                "clockStreamRate": 1000,
                "clockWall": int(round(time.time() * 1000)),
                "edgeType": "start",
                "eventId": self._motion_event_id,
                "eventType": "motion",
                "levels": {"0": 47},
                "motionHeatmap": "",
                "motionSnapshot": "",
            }

            self.logger.info(
                f"Triggering motion start (idx: {self._motion_event_id})"
            )
            await self.send(
                self.gen_response("EventAnalytics", payload=payload),
            )
            # Set timestamp inside lock to prevent race
            self._motion_event_ts = time.time()

        # Capture snapshot outside lock to avoid blocking
        motion_snapshot_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False) as motion_snapshot_file:
                motion_snapshot_path = motion_snapshot_file.name
            # File is now closed, copy the snapshot
            shutil.copyfile(await self.get_snapshot(), motion_snapshot_path)
            self.logger.debug(f"Captured motion snapshot to {motion_snapshot_path}")
            self._motion_snapshot = Path(motion_snapshot_path)
        except FileNotFoundError:
            self.logger.debug("Snapshot file not found, skipping motion snapshot")
            if motion_snapshot_path:
                try:
                    os.unlink(motion_snapshot_path)
                except OSError:
                    pass
        except Exception as e:
            self.logger.warning(f"Failed to capture motion snapshot: {e}")
            if motion_snapshot_path:
                try:
                    os.unlink(motion_snapshot_path)
                except OSError:
                    pass

    async def trigger_motion_stop(self) -> None:
        """Trigger a motion stop event with atomic state management.
        
        Uses asyncio.Lock to prevent race conditions when multiple
        concurrent calls occur.
        """
        # Use async lock for atomic check-and-act pattern
        async with self._motion_lock:
            motion_start_ts = self._motion_event_ts
            if motion_start_ts is None:
                # No motion in progress, skip
                return
            
            payload: dict[str, Any] = {
                "clockBestMonotonic": int(self.get_uptime()),
                "clockBestWall": int(round(motion_start_ts * 1000)),
                "clockMonotonic": int(self.get_uptime()),
                "clockStream": int(self.get_uptime()),
                "clockStreamRate": 1000,
                "clockWall": int(round(time.time() * 1000)),
                "edgeType": "stop",
                "eventId": self._motion_event_id,
                "eventType": "motion",
                "levels": {"0": 49},
                "motionHeatmap": "heatmap.png",
                "motionSnapshot": "motionsnap.jpg",
            }
            self.logger.info(
                f"Triggering motion stop (idx: {self._motion_event_id})"
            )
            await self.send(
                self.gen_response("EventAnalytics", payload=payload),
            )
            self._motion_event_id += 1
            # Clear timestamp inside lock to prevent race
            self._motion_event_ts = None

    # =========================================================================
    # Video Streaming (FFMPEG)
    # =========================================================================
    
    def get_base_ffmpeg_args(self, stream_index: str = "") -> str:
        """Get base FFmpeg arguments for all streams."""
        base_args = [
            "-avoid_negative_ts",
            "make_zero",
            "-fflags",
            "+genpts+discardcorrupt",
            "-use_wallclock_as_timestamps 1",
        ]

        try:
            output = subprocess.check_output(["ffmpeg", "-h", "full"])
            if b"stimeout" in output:
                base_args.append("-stimeout 15000000")
            else:
                base_args.append("-timeout 15000000")
        except subprocess.CalledProcessError:
            self.logger.exception("Could not check for ffmpeg options")

        return " ".join(base_args)

    async def start_video_stream(
        self, stream_index: str, stream_name: str, destination: tuple[str, int]
    ):
        """Start an FFmpeg process to stream video to UniFi Protect.
        
        Uses Python streaming (StreamManager) if UNIFI_USE_PYTHON_STREAMING=true,
        otherwise falls back to shell pipeline for backward compatibility.
        """
        if self._use_python_streaming:
            await self._start_video_stream_python(stream_index, stream_name, destination)
        else:
            await self._start_video_stream_shell(stream_index, stream_name, destination)

    async def _start_video_stream_shell(
        self, stream_index: str, stream_name: str, destination: tuple[str, int]
    ):
        """Start an FFmpeg process using shell pipeline (legacy method).
        
        .. deprecated::
            This shell pipeline method is deprecated and will be removed in a future version.
            Use Python streaming (UNIFI_USE_PYTHON_STREAMING=true) instead.
        
        This is the original implementation using shell=True with piped commands.
        Kept for backward compatibility when UNIFI_USE_PYTHON_STREAMING=false.
        """
        self.logger.warning(
            f"DEPRECATED: Using legacy shell pipeline for {stream_index}. "
            "This method is deprecated and will be removed in a future version. "
            "Python streaming is now the default. To use Python streaming, ensure "
            "UNIFI_USE_PYTHON_STREAMING is not set to 'false'."
        )
        has_spawned = stream_index in self._ffmpeg_handles
        is_dead = has_spawned and self._ffmpeg_handles[stream_index].poll() is not None

        if not has_spawned or is_dead:
            source = await self.get_stream_source(stream_index)
            cmd = (
                "ffmpeg -nostdin -loglevel error -y"
                f" {self.get_base_ffmpeg_args(stream_index)} -rtsp_transport"
                f' {self.args.rtsp_transport} -i "{source}"'
                f" {self.get_extra_ffmpeg_args(stream_index)} -metadata"
                f" streamName={stream_name} -f flv - | {sys.executable} -m"
                " unifi.clock_sync"
                f" {'--write-timestamps' if self._needs_flv_timestamps else ''} | nc"
                f" {destination[0]} {destination[1]}"
            )

            if is_dead:
                self.logger.warn(f"Previous ffmpeg process for {stream_index} died.")

            self.logger.info(
                f"Spawning ffmpeg for {stream_index} ({stream_name}): {cmd}"
            )
            self._ffmpeg_handles[stream_index] = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, shell=True
            )

    async def _start_video_stream_python(
        self, stream_index: str, stream_name: str, destination: tuple[str, int]
    ):
        """Start streaming using pure Python StreamManager.
        
        This method uses the StreamManager from unifi.streaming which handles:
        - FFmpeg subprocess management with asyncio.create_subprocess_exec()
        - TCP socket connection to UniFi Protect with reconnection logic
        - Clock synchronization via ClockSyncProcessor inline
        - Proper error handling and graceful shutdown
        
        Args:
            stream_index: Stream identifier (e.g., "video1", "video2")
            stream_name: Name of the stream for UniFi metadata
            destination: Tuple of (host, port) for UniFi Protect controller
        """
        if not self._stream_manager:
            self.logger.error("StreamManager not initialized")
            return

        # Check if stream is already active
        if self._stream_manager.is_streaming(stream_index):
            self.logger.info(f"Stream {stream_index} already active, skipping")
            return

        # Build FFmpeg command (without shell piping)
        source = await self.get_stream_source(stream_index)
        ffmpeg_cmd = (
            f"ffmpeg -nostdin -loglevel error -y"
            f" {self.get_base_ffmpeg_args(stream_index)} -rtsp_transport"
            f" {self.args.rtsp_transport} -i \"{source}\""
            f" {self.get_extra_ffmpeg_args(stream_index)} -metadata"
            f" streamName={stream_name} -f flv -"
        )

        self.logger.info(
            f"Starting Python stream for {stream_index} ({stream_name}) "
            f"to {destination[0]}:{destination[1]}"
        )
        self.logger.debug(f"FFmpeg command: {sanitize_url(ffmpeg_cmd)}")

        try:
            await self._stream_manager.start_stream(
                stream_index=stream_index,
                stream_name=stream_name,
                destination=destination,
                ffmpeg_cmd=ffmpeg_cmd,
                write_timestamps=self._needs_flv_timestamps,
            )
        except Exception as e:
            self.logger.error(f"Failed to start Python stream for {stream_index}: {e}")

    def stop_video_stream(self, stream_index: str):
        """Stop the video stream for a given stream index.
        
        Handles both shell-based and Python-based streaming.
        """
        # Stop Python stream if using StreamManager
        if self._use_python_streaming and self._stream_manager:
            if self._stream_manager.is_streaming(stream_index):
                self.logger.info(f"Stopping Python stream {stream_index}")
                # Note: stop_stream is async, but this method is sync for backward compat
                # We'll handle this via asyncio if needed, or use stop_all in close()
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(
                            self._stream_manager.stop_stream(stream_index)
                        )
                    else:
                        loop.run_until_complete(
                            self._stream_manager.stop_stream(stream_index)
                        )
                except RuntimeError:
                    pass  # No event loop available
                return

        # Stop shell-based stream (legacy)
        if stream_index in self._ffmpeg_handles:
            self.logger.info(f"Stopping shell stream {stream_index}")
            self._ffmpeg_handles[stream_index].kill()

    def close_streams(self):
        """Close all video streams.
        
        Handles both shell-based and Python-based streaming.
        """
        # Close Python streams
        if self._stream_manager:
            self.logger.info("Closing all Python streams")
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._stream_manager.stop_all())
                else:
                    loop.run_until_complete(self._stream_manager.stop_all())
            except RuntimeError:
                pass  # No event loop available

        # Close shell-based streams (legacy)
        for stream in list(self._ffmpeg_handles.keys()):
            self.stop_video_stream(stream)

    # =========================================================================
    # UniFi Protect Protocol Implementation
    # =========================================================================
    
    async def _run(self, ws) -> None:
        """Main run loop for WebSocket connection."""
        self._session = ws
        await self.init_adoption()
        while True:
            try:
                msg = await ws.recv()
            except websockets.exceptions.ConnectionClosedError:
                self.logger.info(f"Connection to {self.args.host} was closed.")
                raise RetryableError()

            if msg is not None:
                force_reconnect = await self.process(msg)
                if force_reconnect:
                    self.logger.info("Reconnecting...")
                    raise RetryableError()

    def gen_msg_id(self) -> int:
        """Generate a new message ID."""
        self._msg_id += 1
        return self._msg_id

    def gen_response(
        self, name: str, response_to: int = 0, payload: Optional[dict[str, Any]] = None
    ) -> AVClientResponse:
        """Generate a protocol response message."""
        if not payload:
            payload = {}
        return {
            "from": "ubnt_avclient",
            "functionName": name,
            "inResponseTo": response_to,
            "messageId": self.gen_msg_id(),
            "payload": payload,
            "responseExpected": False,
            "to": "UniFiVideo",
        }

    def get_uptime(self) -> float:
        """Get the uptime in seconds."""
        return time.time() - self._init_time

    async def send(self, msg: AVClientRequest) -> None:
        """Send a message over WebSocket."""
        self.logger.debug(f"Sending: {msg}")
        ws = self._session
        if ws:
            await ws.send(json.dumps(msg).encode())

    async def init_adoption(self) -> None:
        """Initialize adoption with UniFi Protect."""
        self.logger.info(
            f"Adopting with token [{self.args.token}] and mac [{self.args.mac}]"
        )
        await self.send(
            self.gen_response(
                "ubnt_avclient_hello",
                payload={
                    "adoptionCode": self.args.token,
                    "connectionHost": self.args.host,
                    "connectionSecurePort": 7442,
                    "fwVersion": self.args.fw_version,
                    "hwrev": 19,
                    "idleTime": 191.96,
                    "ip": self.args.ip,
                    "mac": self.args.mac,
                    "model": self.args.model,
                    "name": self.args.name,
                    "protocolVersion": 67,
                    "rebootTimeoutSec": 30,
                    "semver": "v4.4.8",
                    "totalLoad": 0.5474,
                    "upgradeTimeoutSec": 150,
                    "uptime": int(self.get_uptime()),
                    "features": await self.get_feature_flags(),
                },
            ),
        )

    async def get_feature_flags(self) -> dict[str, Any]:
        """Get feature flags for the camera."""
        return {
            "mic": True,
            "aec": [],
            "videoMode": ["default"],
            "motionDetect": ["enhanced"],
        }

    async def get_video_settings(self) -> dict[str, Any]:
        """Get video settings (override in subclass if needed)."""
        return {}

    async def change_video_settings(self, options) -> None:
        """Handle video settings changes (override in subclass if needed)."""
        return

    # =========================================================================
    # Protocol Message Handlers
    # =========================================================================
    
    async def process_hello(self, msg: AVClientRequest) -> None:
        """Process hello message from UniFi Protect."""
        controller_version = packaging.version.parse(
            msg["payload"].get("controllerVersion")
        )
        self._needs_flv_timestamps = controller_version >= packaging.version.parse(
            "1.21.4"
        )

    async def process_param_agreement(self, msg: AVClientRequest) -> AVClientResponse:
        """Process parameter agreement message."""
        return self.gen_response(
            "ubnt_avclient_paramAgreement",
            msg["messageId"],
            {
                "authToken": self.args.token,
                "features": await self.get_feature_flags(),
            },
        )

    async def process_upgrade(self, msg: AVClientRequest) -> None:
        """Process firmware upgrade request (simulated)."""
        url = msg["payload"]["uri"]
        headers = {"Range": "bytes=0-100"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as r:
                content = await r.content.readexactly(54)
                version = ""
                for i in range(0, 50):
                    b = content[4 + i]
                    if b != b"\x00":
                        version += chr(b)
                self.logger.debug(f"Pretending to upgrade to: {version}")
                self.args.fw_version = version

    async def process_isp_settings(self, msg: AVClientRequest) -> AVClientResponse:
        """Process ISP settings request."""
        payload = {
            "aeMode": "auto",
            "aeTargetPercent": 50,
            "aggressiveAntiFlicker": 0,
            "brightness": 50,
            "contrast": 50,
            "criticalTmpOfProtect": 40,
            "darkAreaCompensateLevel": 0,
            "denoise": 50,
            "enable3dnr": 1,
            "enableMicroTmpProtect": 1,
            "enablePauseMotion": 0,
            "flip": 0,
            "focusMode": "ztrig",
            "focusPosition": 0,
            "forceFilterIrSwitchEvents": 0,
            "hue": 50,
            "icrLightSensorNightThd": 0,
            "icrSensitivity": 0,
            "irLedLevel": 215,
            "irLedMode": "auto",
            "irOnStsBrightness": 0,
            "irOnStsContrast": 0,
            "irOnStsDenoise": 0,
            "irOnStsHue": 0,
            "irOnStsSaturation": 0,
            "irOnStsSharpness": 0,
            "irOnStsWdr": 0,
            "irOnValBrightness": 50,
            "irOnValContrast": 50,
            "irOnValDenoise": 50,
            "irOnValHue": 50,
            "irOnValSaturation": 50,
            "irOnValSharpness": 50,
            "irOnValWdr": 1,
            "mirror": 0,
            "queryIrLedStatus": 0,
            "saturation": 50,
            "sharpness": 50,
            "touchFocusX": 1001,
            "touchFocusY": 1001,
            "wdr": 1,
            "zoomPosition": 0,
        }
        payload.update(await self.get_video_settings())
        return self.gen_response(
            "ResetIspSettings",
            msg["messageId"],
            payload,
        )

    async def process_video_settings(self, msg: AVClientRequest) -> AVClientResponse:
        """Process video settings change request."""
        vid_dst = {
            "video1": ["file:///dev/null"],
            "video2": ["file:///dev/null"],
            "video3": ["file:///dev/null"],
        }

        if msg["payload"] is not None and "video" in msg["payload"]:
            for k, v in msg["payload"]["video"].items():
                if v:
                    if "avSerializer" in v:
                        vid_dst[k] = v["avSerializer"]["destinations"]
                        if "/dev/null" in vid_dst[k]:
                            self.stop_video_stream(k)
                        elif "parameters" in v["avSerializer"]:
                            self._streams[k] = stream = v["avSerializer"]["parameters"][
                                "streamName"
                            ]
                            try:
                                host, port = urllib.parse.urlparse(
                                    v["avSerializer"]["destinations"][0]
                                ).netloc.split(":")
                                await self.start_video_stream(
                                    k, stream, destination=(host, int(port))
                                )
                            except ValueError:
                                pass

        return self.gen_response(
            "ChangeVideoSettings",
            msg["messageId"],
            {
                "audio": {
                    "bitRate": 32000,
                    "channels": 1,
                    "description": "audio track",
                    "enableTemporalNoiseShaping": False,
                    "enabled": True,
                    "mode": 0,
                    "quality": 0,
                    "sampleRate": 11025,
                    "type": "aac",
                    "volume": 0,
                },
                "firmwarePath": "/lib/firmware/",
                "video": {
                    "enableHrd": False,
                    "hdrMode": 0,
                    "lowDelay": False,
                    "videoMode": "default",
                    "mjpg": {
                        "avSerializer": {
                            "destinations": [
                                "file:///tmp/snap.jpeg",
                                "file:///tmp/snap_av.jpg",
                            ],
                            "parameters": {
                                "audioId": 1000,
                                "enableTimestampsOverlapAvoidance": False,
                                "suppressAudio": True,
                                "suppressVideo": False,
                                "videoId": 1001,
                            },
                            "type": "mjpg",
                        },
                        "bitRateCbrAvg": 500000,
                        "bitRateVbrMax": 500000,
                        "bitRateVbrMin": None,
                        "description": "JPEG pictures",
                        "enabled": True,
                        "fps": 5,
                        "height": 720,
                        "isCbr": False,
                        "maxFps": 5,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": None,
                        "name": "mjpg",
                        "quality": 80,
                        "sourceId": 3,
                        "streamId": 8,
                        "streamOrdinal": 3,
                        "type": "mjpg",
                        "validBitrateRangeMax": 6000000,
                        "validBitrateRangeMin": 32000,
                        "width": 1280,
                    },
                    "video1": {
                        "M": 1,
                        "N": 30,
                        "avSerializer": {
                            "destinations": vid_dst["video1"],
                            "parameters": None
                            if "video1" not in self._streams
                            else {
                                "audioId": None,
                                "streamName": self._streams["video1"],
                                "suppressAudio": None,
                                "suppressVideo": None,
                                "videoId": None,
                            },
                            "type": "extendedFlv",
                        },
                        "bitRateCbrAvg": 1400000,
                        "bitRateVbrMax": 2800000,
                        "bitRateVbrMin": 48000,
                        "description": "Hi quality video track",
                        "enabled": True,
                        "fps": 15,
                        "gopModel": 0,
                        "height": 1080,
                        "horizontalFlip": False,
                        "isCbr": False,
                        "maxFps": 30,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": 6,
                        "name": "video1",
                        "sourceId": 0,
                        "streamId": 1,
                        "streamOrdinal": 0,
                        "type": "h264",
                        "validBitrateRangeMax": 2800000,
                        "validBitrateRangeMin": 32000,
                        "validFpsValues": [
                            1, 2, 3, 4, 5, 6, 8, 9, 10, 12, 15, 16, 18, 20, 24, 25, 30,
                        ],
                        "verticalFlip": False,
                        "width": 1920,
                    },
                    "video2": {
                        "M": 1,
                        "N": 30,
                        "avSerializer": {
                            "destinations": vid_dst["video2"],
                            "parameters": None
                            if "video2" not in self._streams
                            else {
                                "audioId": None,
                                "streamName": self._streams["video2"],
                                "suppressAudio": None,
                                "suppressVideo": None,
                                "videoId": None,
                            },
                            "type": "extendedFlv",
                        },
                        "bitRateCbrAvg": 500000,
                        "bitRateVbrMax": 1200000,
                        "bitRateVbrMin": 48000,
                        "currentVbrBitrate": 1200000,
                        "description": "Medium quality video track",
                        "enabled": True,
                        "fps": 15,
                        "gopModel": 0,
                        "height": 720,
                        "horizontalFlip": False,
                        "isCbr": False,
                        "maxFps": 30,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": 6,
                        "name": "video2",
                        "sourceId": 1,
                        "streamId": 2,
                        "streamOrdinal": 1,
                        "type": "h264",
                        "validBitrateRangeMax": 1500000,
                        "validBitrateRangeMin": 32000,
                        "validFpsValues": [
                            1, 2, 3, 4, 5, 6, 8, 9, 10, 12, 15, 16, 18, 20, 24, 25, 30,
                        ],
                        "verticalFlip": False,
                        "width": 1280,
                    },
                    "video3": {
                        "M": 1,
                        "N": 30,
                        "avSerializer": {
                            "destinations": vid_dst["video3"],
                            "parameters": None
                            if "video3" not in self._streams
                            else {
                                "audioId": None,
                                "streamName": self._streams["video3"],
                                "suppressAudio": None,
                                "suppressVideo": None,
                                "videoId": None,
                            },
                            "type": "extendedFlv",
                        },
                        "bitRateCbrAvg": 300000,
                        "bitRateVbrMax": 200000,
                        "bitRateVbrMin": 48000,
                        "currentVbrBitrate": 200000,
                        "description": "Low quality video track",
                        "enabled": True,
                        "fps": 15,
                        "gopModel": 0,
                        "height": 360,
                        "horizontalFlip": False,
                        "isCbr": False,
                        "maxFps": 30,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": 6,
                        "name": "video3",
                        "sourceId": 2,
                        "streamId": 4,
                        "streamOrdinal": 2,
                        "type": "h264",
                        "validBitrateRangeMax": 750000,
                        "validBitrateRangeMin": 32000,
                        "validFpsValues": [
                            1, 2, 3, 4, 5, 6, 8, 9, 10, 12, 15, 16, 18, 20, 24, 25, 30,
                        ],
                        "verticalFlip": False,
                        "width": 640,
                    },
                    "vinFps": 30,
                },
            },
        )

    async def process_device_settings(self, msg: AVClientRequest) -> AVClientResponse:
        """Process device settings request."""
        return self.gen_response(
            "ChangeDeviceSettings",
            msg["messageId"],
            {
                "name": self.args.name,
                "timezone": "PST8PDT,M3.2.0,M11.1.0",
            },
        )

    async def process_osd_settings(self, msg: AVClientRequest) -> AVClientResponse:
        """Process OSD settings request."""
        return self.gen_response(
            "ChangeOsdSettings",
            msg["messageId"],
            {
                "_1": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "_2": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "_3": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "_4": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "enableOverlay": 1,
                "logoScale": 50,
                "overlayColorId": 0,
                "textScale": 50,
                "useCustomLogo": 0,
            },
        )

    async def process_network_status(self, msg: AVClientRequest) -> AVClientResponse:
        """Process network status request."""
        return self.gen_response(
            "NetworkStatus",
            msg["messageId"],
            {
                "connectionState": 2,
                "connectionStateDescription": "CONNECTED",
                "defaultInterface": "eth0",
                "dhcpLeasetime": 86400,
                "dnsServer": "8.8.8.8 4.2.2.2",
                "gateway": "192.168.103.1",
                "ipAddress": self.args.ip,
                "linkDuplex": 1,
                "linkSpeedMbps": 100,
                "mode": "dhcp",
                "networkMask": "255.255.255.0",
            },
        )

    async def process_sound_led_settings(
        self, msg: AVClientRequest
    ) -> AVClientResponse:
        """Process sound/LED settings request."""
        return self.gen_response(
            "ChangeSoundLedSettings",
            msg["messageId"],
            {
                "ledFaceAlwaysOnWhenManaged": 1,
                "ledFaceEnabled": 1,
                "speakerEnabled": 1,
                "speakerVolume": 100,
                "systemSoundsEnabled": 1,
                "userLedBlinkPeriodMs": 0,
                "userLedColorFg": "blue",
                "userLedOnNoff": 1,
            },
        )

    async def process_change_isp_settings(
        self, msg: AVClientRequest
    ) -> AVClientResponse:
        """Process ISP settings change request."""
        payload = {
            "aeMode": "auto",
            "aeTargetPercent": 50,
            "aggressiveAntiFlicker": 0,
            "brightness": 50,
            "contrast": 50,
            "criticalTmpOfProtect": 40,
            "dZoomCenterX": 50,
            "dZoomCenterY": 50,
            "dZoomScale": 0,
            "dZoomStreamId": 4,
            "darkAreaCompensateLevel": 0,
            "denoise": 50,
            "enable3dnr": 1,
            "enableExternalIr": 0,
            "enableMicroTmpProtect": 1,
            "enablePauseMotion": 0,
            "flip": 0,
            "focusMode": "ztrig",
            "focusPosition": 0,
            "forceFilterIrSwitchEvents": 0,
            "hue": 50,
            "icrLightSensorNightThd": 0,
            "icrSensitivity": 0,
            "irLedLevel": 215,
            "irLedMode": "auto",
            "irOnStsBrightness": 0,
            "irOnStsContrast": 0,
            "irOnStsDenoise": 0,
            "irOnStsHue": 0,
            "irOnStsSaturation": 0,
            "irOnStsSharpness": 0,
            "irOnStsWdr": 0,
            "irOnValBrightness": 50,
            "irOnValContrast": 50,
            "irOnValDenoise": 50,
            "irOnValHue": 50,
            "irOnValSaturation": 50,
            "irOnValSharpness": 50,
            "irOnValWdr": 1,
            "lensDistortionCorrection": 1,
            "masks": None,
            "mirror": 0,
            "queryIrLedStatus": 0,
            "saturation": 50,
            "sharpness": 50,
            "touchFocusX": 1001,
            "touchFocusY": 1001,
            "wdr": 1,
            "zoomPosition": 0,
        }

        if msg["payload"]:
            await self.change_video_settings(msg["payload"])

        payload.update(await self.get_video_settings())
        return self.gen_response("ChangeIspSettings", msg["messageId"], payload)

    async def process_analytics_settings(
        self, msg: AVClientRequest
    ) -> AVClientResponse:
        """Process analytics settings request."""
        return self.gen_response(
            "ChangeAnalyticsSettings", msg["messageId"], msg["payload"]
        )

    async def process_snapshot_request(
        self, msg: AVClientRequest
    ) -> Optional[AVClientResponse]:
        """Process snapshot upload request with proper resource cleanup."""
        snapshot_type = msg["payload"]["what"]
        if snapshot_type in ["motionSnapshot", "smartDetectZoneSnapshot"]:
            path = self._motion_snapshot
        else:
            path = await self.get_snapshot()

        if path and path.exists():
            async with aiohttp.ClientSession() as session:
                # Use context manager to ensure file handle is properly closed
                try:
                    with path.open("rb") as f:
                        file_data = f.read()
                    files = {"payload": file_data}
                    files.update(msg["payload"].get("formFields", {}))
                    await session.post(
                        msg["payload"]["uri"],
                        data=files,
                        ssl=self._ssl_context,
                    )
                    self.logger.debug(f"Uploaded {snapshot_type} from {path}")
                except aiohttp.ClientError as e:
                    self.logger.error(f"Failed to upload snapshot: {e}")
                except OSError as e:
                    self.logger.error(f"Failed to read snapshot file {path}: {e}")
        else:
            self.logger.warning(
                f"Snapshot file {path} is not ready yet, skipping upload"
            )

        if msg["responseExpected"]:
            return self.gen_response("GetRequest", response_to=msg["messageId"])

    async def process_time(self, msg: AVClientRequest) -> AVClientResponse:
        """Process time synchronization request."""
        return self.gen_response(
            "ubnt_avclient_paramAgreement",
            msg["messageId"],
            {
                "monotonicMs": self.get_uptime(),
                "wallMs": int(round(time.time() * 1000)),
                "features": {},
            },
        )

    async def _fetch_to_file(self, url: str, dst: Path) -> bool:
        """Fetch a URL and save to file."""
        try:
            async with aiohttp.request("GET", url) as resp:
                if resp.status != 200:
                    self.logger.error(f"Error retrieving file {resp.status}")
                    return False
                with dst.open("wb") as f:
                    f.write(await resp.read())
                    return True
        except aiohttp.ClientError:
            return False

    async def process(self, msg: bytes) -> bool:
        """Process an incoming message from UniFi Protect."""
        m = json.loads(msg)
        fn = m["functionName"]

        self.logger.info(f"Processing [{fn}] message")
        self.logger.debug(f"Message contents: {m}")

        if (("responseExpected" not in m) or (m["responseExpected"] is False)) and (
            fn
            not in [
                "GetRequest",
                "ChangeVideoSettings",
                "UpdateFirmwareRequest",
                "Reboot",
                "ubnt_avclient_hello",
            ]
        ):
            return False

        res: Optional[AVClientResponse] = None

        if fn == "ubnt_avclient_time":
            res = await self.process_time(m)
        elif fn == "ubnt_avclient_hello":
            await self.process_hello(m)
        elif fn == "ubnt_avclient_paramAgreement":
            res = await self.process_param_agreement(m)
        elif fn == "ResetIspSettings":
            res = await self.process_isp_settings(m)
        elif fn == "ChangeVideoSettings":
            res = await self.process_video_settings(m)
        elif fn == "ChangeDeviceSettings":
            res = await self.process_device_settings(m)
        elif fn == "ChangeOsdSettings":
            res = await self.process_osd_settings(m)
        elif fn == "NetworkStatus":
            res = await self.process_network_status(m)
        elif fn == "AnalyticsTest":
            res = self.gen_response("AnalyticsTest", response_to=m["messageId"])
        elif fn == "ChangeSoundLedSettings":
            res = await self.process_sound_led_settings(m)
        elif fn == "ChangeIspSettings":
            res = await self.process_change_isp_settings(m)
        elif fn == "ChangeAnalyticsSettings":
            res = await self.process_analytics_settings(m)
        elif fn == "GetRequest":
            res = await self.process_snapshot_request(m)
        elif fn == "UpdateUsernamePassword":
            res = self.gen_response(
                "UpdateUsernamePassword", response_to=m["messageId"]
            )
        elif fn == "ChangeSmartDetectSettings":
            res = self.gen_response(
                "ChangeSmartDetectSettings", response_to=m["messageId"]
            )
        elif fn == "UpdateFirmwareRequest":
            await self.process_upgrade(m)
            return True
        elif fn == "Reboot":
            return True

        if res is not None:
            await self.send(res)

        return False

    async def close(self):
        """Clean up resources with proper shutdown sequence."""
        self.logger.info("Cleaning up instance")
        
        # Signal shutdown
        self._shutdown_requested = True
        
        # Cancel watchdog task if running
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        
        # Stop motion event if in progress
        try:
            await asyncio.wait_for(self.trigger_motion_stop(), timeout=5.0)
        except asyncio.TimeoutError:
            self.logger.warning("Timeout stopping motion event during shutdown")
        except Exception as e:
            self.logger.error(f"Error stopping motion event: {e}")
        
        # Close all streams
        self.close_streams()
        
        # Clean up snapshot directory
        try:
            if self.snapshot_dir and os.path.exists(self.snapshot_dir):
                shutil.rmtree(self.snapshot_dir)
        except Exception as e:
            self.logger.warning(f"Failed to clean up snapshot directory: {e}")
        
        self.logger.info("Cleanup complete")

    async def start_watchdog(self) -> None:
        """Start the stream health watchdog.
        
        The watchdog monitors stream health and automatically restarts
        failed streams. It logs health status periodically.
        """
        if self._watchdog_task and not self._watchdog_task.done():
            self.logger.warning("Watchdog already running")
            return
        
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        self.logger.info("Stream health watchdog started")

    async def _watchdog_loop(self) -> None:
        """Watchdog loop that monitors stream health.
        
        Checks stream activity and restarts unhealthy streams.
        Runs every _watchdog_interval seconds.
        """
        while not self._shutdown_requested:
            try:
                await asyncio.sleep(self._watchdog_interval)
                
                if self._shutdown_requested:
                    break
                
                # Check stream health if using Python streaming
                if self._use_python_streaming and self._stream_manager:
                    active_streams = self._stream_manager.get_active_streams()
                    
                    if not active_streams:
                        self.logger.debug("No active streams to monitor")
                        continue
                    
                    current_time = time.time()
                    
                    for stream_index in active_streams:
                        last_activity = self._last_stream_activity.get(stream_index, 0)
                        time_since_activity = current_time - last_activity
                        
                        if time_since_activity > self._stream_timeout:
                            self.logger.warning(
                                f"Stream {stream_index} appears unhealthy "
                                f"(no activity for {time_since_activity:.1f}s), "
                                "considering restart"
                            )
                            # The VideoStreamer already handles reconnection internally
                            # Just log the status for now
                        else:
                            self.logger.debug(
                                f"Stream {stream_index} healthy "
                                f"(last activity {time_since_activity:.1f}s ago)"
                            )
                    
                    # Log overall health status
                    self.logger.info(
                        f"Stream health check: {len(active_streams)} active streams"
                    )
                            
            except asyncio.CancelledError:
                self.logger.info("Watchdog loop cancelled")
                break
            except Exception as e:
                self.logger.error(f"Error in watchdog loop: {e}")
                # Continue running despite errors
                await asyncio.sleep(self._watchdog_interval)
        
        self.logger.info("Watchdog loop stopped")

    def update_stream_activity(self, stream_index: str) -> None:
        """Update the last activity timestamp for a stream.
        
        Called by the streaming code to indicate stream health.
        """
        self._last_stream_activity[stream_index] = time.time()
