"""
Pure Python streaming module for UniFi Camera Proxy.

This module provides robust streaming from FFmpeg to UniFi Protect without using fragile shell pipelines.
It handles:
- FFmpeg subprocess management with proper PIPE handling
- TCP socket connection to UniFi Protect with reconnection logic
- Clock sync processing inline (no separate process)
- Error handling and graceful shutdown
- Exponential backoff for reconnections
"""

import asyncio
import logging
import signal
import socket
import struct
import time
from typing import Optional, Tuple

from flvlib3.astypes import FLVObject
from flvlib3.primitives import make_ui8, make_ui32
from flvlib3.tags import create_script_tag


class StreamError(Exception):
    """Base exception for streaming errors."""
    pass


class StreamConnectionError(StreamError):
    """Raised when connection to UniFi Protect fails."""
    pass


class FFmpegError(StreamError):
    """Raised when FFmpeg process fails."""
    pass


class ClockSyncProcessor:
    """
    Processes FLV stream data and injects clock synchronization packets.
    
    This replaces the separate clock_sync.py process with an inline implementation.
    """
    
    def __init__(self, write_timestamps: bool = False, logger: Optional[logging.Logger] = None):
        self.write_timestamps = write_timestamps
        self.logger = logger or logging.getLogger(__name__)
        self.last_ts: Optional[float] = None
        self.start_time = time.time()
        self._initialized = False
        self._buffer = b""
    
    def _write_timestamp_trailer(self, is_packet: bool, ts: float) -> bytes:
        """Write 15 byte trailer for timestamp."""
        trailer = bytearray()
        trailer.extend(make_ui8(0))
        if is_packet:
            trailer.extend(bytes([1, 95, 144, 0, 0, 0, 0, 0, 0, 0, 0]))
        else:
            trailer.extend(bytes([0, 43, 17, 0, 0, 0, 0, 0, 0, 0, 0]))
        trailer.extend(make_ui32(int(ts * 1000 * 100)))
        return bytes(trailer)
    
    def _create_clock_sync_packet(self, timestamp: int, now: float) -> bytes:
        """Create clock synchronization packet."""
        data = FLVObject()
        data["streamClock"] = int(timestamp)
        data["streamClockBase"] = 0
        data["wallClock"] = now * 1000
        return create_script_tag("onClockSync", data, timestamp)
    
    def _create_mpma_packet(self) -> bytes:
        """Create MPMA (Multi-Parameter Media Adaptation) packet."""
        data = FLVObject()
        data["cs"] = FLVObject()
        data["cs"]["cur"] = 1500000
        data["cs"]["max"] = 1500000
        data["cs"]["min"] = 1500000
        
        data["m"] = FLVObject()
        data["m"]["cur"] = 1500000
        data["m"]["max"] = 1500000
        data["m"]["min"] = 1500000
        data["r"] = 0
        
        data["sp"] = FLVObject()
        data["sp"]["cur"] = 1500000
        data["sp"]["max"] = 1500000
        data["sp"]["min"] = 1500000
        data["t"] = 75000.0
        return create_script_tag("onMpma", data, 0)
    
    def process_header(self, data: bytes) -> Tuple[bytes, bytes]:
        """
        Process FLV header and return (processed_data, remaining_data).
        """
        if len(data) < 9:
            return b"", data
        
        header = data[:3]
        if header != b"FLV":
            raise StreamError("Not a valid FLV file")
        
        # Process header
        output = bytearray()
        output.extend(header)
        output.extend(data[3:4])  # Flags byte 1
        # Skip byte 4, write custom bitmask
        output.extend(make_ui8(7))
        output.extend(data[5:9])  # Header remaining bytes
        
        self._initialized = True
        return bytes(output), data[9:]
    
    def process_packet(self, data: bytes) -> Tuple[bytes, bytes, bool]:
        """
        Process a single FLV packet.
        
        Returns: (output_data, remaining_data, needs_more)
        - output_data: Processed data to send
        - remaining_data: Data that couldn't be processed (incomplete packet)
        - needs_more: True if more data is needed to complete the packet
        """
        if len(data) < 12:
            return b"", data, True
        
        header = data[:12]
        packet_type = header[0]
        
        # Get payload size
        high, low = struct.unpack(">BH", header[1:4])
        payload_size = (high << 16) + low
        
        # Get timestamp
        low_high = header[4:8]
        combined = bytes([low_high[3]]) + low_high[:3]
        timestamp = struct.unpack(">i", combined)[0]
        
        total_packet_size = 12 + payload_size + 4  # header + payload + prev size
        if len(data) < total_packet_size:
            return b"", data, True
        
        now = time.time()
        output = bytearray()
        
        # Insert clock sync packets every 5 seconds
        if self.write_timestamps and (not self.last_ts or now - self.last_ts >= 5):
            self.last_ts = now
            
            # Clock sync packet
            output.extend(self._create_clock_sync_packet(timestamp, now))
            output.extend(self._write_timestamp_trailer(False, now - self.start_time))
            
            # MPMA packet
            output.extend(self._create_mpma_packet())
            output.extend(self._write_timestamp_trailer(False, now - self.start_time))
        
        # Write original packet
        output.extend(data[:12 + payload_size + 3])
        output.extend(self._write_timestamp_trailer(packet_type == 9, now - self.start_time))
        
        return bytes(output), data[total_packet_size:], False


class VideoStreamer:
    """
    Manages FFmpeg subprocess and streams video to UniFi Protect.
    
    This class replaces the fragile shell pipeline:
        ffmpeg | clock_sync | nc
    
    With a robust Python implementation that handles:
    - FFmpeg subprocess management
    - TCP socket connection with reconnection
    - Clock sync processing inline
    - Graceful error handling and recovery
    """
    
    def __init__(
        self,
        stream_index: str,
        stream_name: str,
        destination: Tuple[str, int],
        ffmpeg_cmd: str,
        write_timestamps: bool = False,
        logger: Optional[logging.Logger] = None,
    ):
        self.stream_index = stream_index
        self.stream_name = stream_name
        self.destination = destination
        self.ffmpeg_cmd = ffmpeg_cmd
        self.write_timestamps = write_timestamps
        self.logger = logger or logging.getLogger(__name__)
        
        self._ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self._socket: Optional[socket.socket] = None
        self._clock_sync = ClockSyncProcessor(write_timestamps, logger)
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None
        
    async def start(self) -> None:
        """Start the video stream."""
        self._running = True
        await self._run_stream()
    
    async def stop(self) -> None:
        """Stop the video stream gracefully."""
        self._running = False
        self.logger.info(f"Stopping stream {self.stream_index}")
        
        # Close socket first
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        
        # Terminate FFmpeg gracefully
        if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
            try:
                self._ffmpeg_proc.terminate()
                await asyncio.wait_for(self._ffmpeg_proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.warning(f"FFmpeg didn't terminate, killing")
                self._ffmpeg_proc.kill()
            except Exception as e:
                self.logger.debug(f"Error stopping FFmpeg: {e}")
            self._ffmpeg_proc = None
    
    async def _connect_socket(self, retries: int = 5, backoff: float = 1.0) -> socket.socket:
        """
        Connect to UniFi Protect with exponential backoff.
        
        Args:
            retries: Maximum number of connection attempts
            backoff: Initial backoff time in seconds (doubles each retry)
        
        Returns:
            Connected socket
        
        Raises:
            StreamConnectionError: If connection fails after all retries
        """
        last_error = None
        for attempt in range(retries):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(10.0)  # Connection timeout
                await asyncio.get_event_loop().sock_connect(
                    sock, self.destination
                )
                self.logger.info(
                    f"Connected to {self.destination[0]}:{self.destination[1]} "
                    f"for stream {self.stream_index}"
                )
                return sock
            except (socket.error, OSError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < retries - 1:
                    wait_time = backoff * (2 ** attempt)
                    self.logger.warning(
                        f"Connection attempt {attempt + 1}/{retries} failed: {e}. "
                        f"Retrying in {wait_time:.1f}s..."
                    )
                    await asyncio.sleep(wait_time)
        
        raise StreamConnectionError(
            f"Failed to connect to {self.destination[0]}:{self.destination[1]} "
            f"after {retries} attempts: {last_error}"
        )
    
    async def _start_ffmpeg(self) -> asyncio.subprocess.Process:
        """
        Start FFmpeg subprocess with stdout pipe.
        
        Returns:
            FFmpeg subprocess with stdout pipe
        """
        self.logger.info(f"Starting FFmpeg for {self.stream_index}: {self.ffmpeg_cmd}")
        
        proc = await asyncio.create_subprocess_shell(
            self.ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        
        return proc
    
    async def _run_stream(self) -> None:
        """
        Main streaming loop.
        
        Handles:
        - FFmpeg subprocess management
        - Socket connection with reconnection
        - Clock sync processing
        - Error recovery
        """
        while self._running:
            try:
                # Start FFmpeg
                self._ffmpeg_proc = await self._start_ffmpeg()
                if not self._ffmpeg_proc.stdout:
                    raise FFmpegError("FFmpeg stdout is None")
                
                # Connect to UniFi Protect
                self._socket = await self._connect_socket()
                
                # Process stream
                await self._process_stream()
                
            except StreamConnectionError as e:
                self.logger.error(f"Connection error for {self.stream_index}: {e}")
                if self._running:
                    await asyncio.sleep(2.0)  # Wait before reconnecting
                    
            except FFmpegError as e:
                self.logger.error(f"FFmpeg error for {self.stream_index}: {e}")
                if self._running:
                    await asyncio.sleep(2.0)
                    
            except asyncio.CancelledError:
                self.logger.info(f"Stream {self.stream_index} cancelled")
                break
                
            except Exception as e:
                self.logger.exception(f"Unexpected error in stream {self.stream_index}: {e}")
                if self._running:
                    await asyncio.sleep(2.0)
            
            finally:
                # Cleanup
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                    self._socket = None
                
                if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
                    try:
                        self._ffmpeg_proc.kill()
                    except Exception:
                        pass
                    self._ffmpeg_proc = None
    
    async def _process_stream(self) -> None:
        """
        Process the FLV stream from FFmpeg and send to UniFi Protect.
        
        This method:
        1. Reads FLV data from FFmpeg stdout
        2. Processes it through clock sync
        3. Sends it to UniFi Protect via socket
        """
        if not self._ffmpeg_proc or not self._ffmpeg_proc.stdout or not self._socket:
            return
        
        reader = self._ffmpeg_proc.stdout
        buffer = b""
        header_processed = False
        
        try:
            while self._running:
                # Read data from FFmpeg
                try:
                    chunk = await reader.read(8192)
                    if not chunk:
                        self.logger.info(f"FFmpeg EOF for {self.stream_index}")
                        break
                    buffer += chunk
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"Error reading from FFmpeg: {e}")
                    break
                
                # Process FLV header if not done
                if not header_processed and len(buffer) >= 9:
                    output, buffer = self._clock_sync.process_header(buffer)
                    if output:
                        await self._send_data(output)
                    header_processed = True
                
                # Process packets
                while buffer:
                    output, buffer, needs_more = self._clock_sync.process_packet(buffer)
                    if output:
                        await self._send_data(output)
                    if needs_more:
                        break
                        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"Error processing stream {self.stream_index}: {e}")
    
    async def _send_data(self, data: bytes) -> None:
        """
        Send data to UniFi Protect via socket with error handling.
        """
        if not self._socket:
            raise StreamConnectionError("Socket not connected")
        
        try:
            loop = asyncio.get_event_loop()
            await loop.sock_sendall(self._socket, data)
        except (socket.error, OSError, BrokenPipeError, ConnectionResetError) as e:
            self.logger.warning(f"Socket error for {self.stream_index}: {e}")
            raise StreamConnectionError(f"Socket send failed: {e}")


class StreamManager:
    """
    Manages multiple video streams.
    
    This class provides a high-level interface for managing multiple
    video streams to UniFi Protect.
    """
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._streamers: dict[str, VideoStreamer] = {}
        self._tasks: dict[str, asyncio.Task] = {}
    
    async def start_stream(
        self,
        stream_index: str,
        stream_name: str,
        destination: Tuple[str, int],
        ffmpeg_cmd: str,
        write_timestamps: bool = False,
    ) -> None:
        """
        Start a video stream.
        
        Args:
            stream_index: Stream identifier (e.g., "video1", "video2")
            stream_name: Name of the stream for metadata
            destination: (host, port) tuple for UniFi Protect
            ffmpeg_cmd: FFmpeg command to execute
            write_timestamps: Whether to write timestamp trailers
        """
        # Stop existing stream if any
        await self.stop_stream(stream_index)
        
        streamer = VideoStreamer(
            stream_index=stream_index,
            stream_name=stream_name,
            destination=destination,
            ffmpeg_cmd=ffmpeg_cmd,
            write_timestamps=write_timestamps,
            logger=self.logger,
        )
        
        self._streamers[stream_index] = streamer
        self._tasks[stream_index] = asyncio.create_task(streamer.start())
        
        self.logger.info(f"Started stream {stream_index} ({stream_name})")
    
    async def stop_stream(self, stream_index: str) -> None:
        """
        Stop a video stream.
        
        Args:
            stream_index: Stream identifier to stop
        """
        # Cancel task first
        if stream_index in self._tasks:
            task = self._tasks[stream_index]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            del self._tasks[stream_index]
        
        # Stop streamer
        if stream_index in self._streamers:
            await self._streamers[stream_index].stop()
            del self._streamers[stream_index]
        
        self.logger.info(f"Stopped stream {stream_index}")
    
    async def stop_all(self) -> None:
        """Stop all video streams."""
        for stream_index in list(self._streamers.keys()):
            await self.stop_stream(stream_index)
    
    def is_streaming(self, stream_index: str) -> bool:
        """Check if a stream is active."""
        return stream_index in self._streamers and stream_index in self._tasks
    
    def get_active_streams(self) -> list[str]:
        """Get list of active stream indices."""
        return list(self._streamers.keys())
