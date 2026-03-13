"""
UniFi Camera Proxy for Reolink RLC-410-5MP

This module provides a simplified camera implementation specifically for the
Reolink RLC-410-5MP camera, supporting:
- RTSP video streaming (main and sub streams)
- Motion detection via HTTP API polling
- Audio streaming support
"""

import argparse
import atexit
import json
import logging
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import aiohttp
import packaging
import reolinkapi
import websockets
from yarl import URL

from unifi.core import RetryableError

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

        # Protocol state
        self._msg_id: int = 0
        self._init_time: float = time.time()
        self._streams: dict[str, str] = {}
        self._motion_snapshot: Optional[Path] = None
        self._motion_event_id: int = 0
        self._motion_event_ts: Optional[float] = None
        self._ffmpeg_handles: dict[str, subprocess.Popen] = {}

        # SSL context for requests
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE
        self._ssl_context.load_cert_chain(args.cert, args.cert)
        self._session: Optional[websockets.legacy.client.WebSocketClientProtocol] = None
        atexit.register(self.close_streams)

        self._needs_flv_timestamps: bool = False

        # Reolink-specific initialization
        self.snapshot_dir: str = tempfile.mkdtemp()
        self.motion_in_progress: bool = False
        self.substream = args.substream
        
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
        self.logger.info(f"Grabbing snapshot: {url}")
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
        """Run the motion detection polling loop."""
        url = (
            f"http://{self.args.ip}"
            f"/api.cgi?cmd=GetMdState&user={self.args.username}"
            f"&password={self.args.password}"
        )
        encoded_url = URL(url, encoded=True)
        body = (
            f'[{{ "cmd":"GetMdState", "param":{{ "channel":{self.args.channel} }} }}]'
        )
        
        while True:
            self.logger.info(f"Connecting to motion events API: {url}")
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(None)
                ) as session:
                    while True:
                        async with session.post(encoded_url, data=body) as resp:
                            data = await resp.read()

                            try:
                                json_body = json.loads(data)
                                if "value" in json_body[0]:
                                    if json_body[0]["value"]["state"] == 1:
                                        if not self.motion_in_progress:
                                            self.motion_in_progress = True
                                            self.logger.info("Trigger motion start")
                                            await self.trigger_motion_start()
                                    elif json_body[0]["value"]["state"] == 0:
                                        if self.motion_in_progress:
                                            self.motion_in_progress = False
                                            self.logger.info("Trigger motion end")
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
                self.logger.error(f"Motion API request failed, retrying. Error: {err}")

    # =========================================================================
    # Motion Detection
    # =========================================================================
    
    async def trigger_motion_start(self) -> None:
        """Trigger a motion start event."""
        if not self._motion_event_ts:
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
            self._motion_event_ts = time.time()

            # Capture snapshot at beginning of motion event for thumbnail
            motion_snapshot_path: str = tempfile.NamedTemporaryFile(delete=False).name
            try:
                shutil.copyfile(await self.get_snapshot(), motion_snapshot_path)
                self.logger.debug(f"Captured motion snapshot to {motion_snapshot_path}")
                self._motion_snapshot = Path(motion_snapshot_path)
            except FileNotFoundError:
                pass

    async def trigger_motion_stop(self) -> None:
        """Trigger a motion stop event."""
        motion_start_ts = self._motion_event_ts
        if motion_start_ts:
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
        """Start an FFmpeg process to stream video to UniFi Protect."""
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

    def stop_video_stream(self, stream_index: str):
        """Stop the FFmpeg process for a stream."""
        if stream_index in self._ffmpeg_handles:
            self.logger.info(f"Stopping stream {stream_index}")
            self._ffmpeg_handles[stream_index].kill()

    def close_streams(self):
        """Close all FFmpeg streams."""
        for stream in self._ffmpeg_handles:
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
        """Process snapshot upload request."""
        snapshot_type = msg["payload"]["what"]
        if snapshot_type in ["motionSnapshot", "smartDetectZoneSnapshot"]:
            path = self._motion_snapshot
        else:
            path = await self.get_snapshot()

        if path and path.exists():
            async with aiohttp.ClientSession() as session:
                files = {"payload": open(path, "rb")}
                files.update(msg["payload"].get("formFields", {}))
                try:
                    await session.post(
                        msg["payload"]["uri"],
                        data=files,
                        ssl=self._ssl_context,
                    )
                    self.logger.debug(f"Uploaded {snapshot_type} from {path}")
                except aiohttp.ClientError:
                    self.logger.exception("Failed to upload snapshot")
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
        """Clean up resources."""
        self.logger.info("Cleaning up instance")
        await self.trigger_motion_stop()
        self.close_streams()
