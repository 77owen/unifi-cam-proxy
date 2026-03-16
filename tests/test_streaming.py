"""
Tests for unifi/streaming.py module.

Tests the following components:
- ClockSyncProcessor: FLV stream processing and clock sync packet injection
- VideoStreamer: FFmpeg subprocess and socket management
- StreamManager: Multi-stream management

This file mocks the flvlib3 dependency to avoid requiring the actual package.
which is not available on PyPI.
"""

import asyncio
import socket
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

# Mock flvlib3 module before importing unifi.streaming
sys.modules['flvlib3'] = MagicMock()
sys.modules['flvlib3.astypes'] = MagicMock()
sys.modules['flvlib3.primitives'] = MagicMock()
sys.modules['flvlib3.tags'] = MagicMock()

from unifi.streaming import (
    ClockSyncProcessor,
    FFmpegError,
    StreamConnectionError,
    StreamError,
    StreamManager,
    VideoStreamer,
)


class TestClockSyncProcessor(unittest.TestCase):
    """Test cases for ClockSyncProcessor class."""
    def test_initialization(self):
        """Test ClockSyncProcessor initialization."""
        processor = ClockSyncProcessor()
        self.assertFalse(processor.write_timestamps)
        self.assertIsNone(processor.last_ts)
        self.assertFalse(processor._initialized)
        self.assertEqual(processor._buffer, b"")
    def test_initialization_with_timestamps(self):
        """Test ClockSyncProcessor initialization with write_timestamps enabled."""
        logger = MagicMock()
        processor = ClockSyncProcessor(write_timestamps=True, logger=logger)
        self.assertTrue(processor.write_timestamps)
        self.assertEqual(processor.logger, logger)
    def test_process_header_valid_flv(self):
        """Test processing valid FLV header."""
        processor = ClockSyncProcessor()
        # Valid FLV header: FLV + version + flags + header size
        flv_header = b"FLV\x01\x05\x00\x00\x00\x09"
        output, remaining = processor.process_header(flv_header)
        self.assertTrue(processor._initialized)
        self.assertEqual(output[:3], b"FLV")
        self.assertEqual(remaining, b"")
    def test_process_header_incomplete_data(self):
        """Test processing incomplete header data."""
        processor = ClockSyncProcessor()
        # Incomplete header (less than 9 bytes)
        incomplete = b"FLV\x01"
        output, remaining = processor.process_header(incomplete)
        self.assertFalse(processor._initialized)
        self.assertEqual(output, b"")
        self.assertEqual(remaining, incomplete)
    def test_process_header_invalid_flv(self):
        """Test processing invalid FLV header raises error."""
        processor = ClockSyncProcessor()
        # Invalid header (doesn't start with FLV)
        invalid_header = b"XXX\x01\x05\x00\x00\x00\x09"
        with self.assertRaises(StreamError) as context:
            processor.process_header(invalid_header)
        self.assertIn("Not a valid FLV file", str(context.exception))
    def test_process_packet_incomplete_data(self):
        """Test processing incomplete packet data."""
        processor = ClockSyncProcessor()
        # Less than 12 bytes
        incomplete = b"\x00" * 10
        output, remaining, needs_more = processor.process_packet(incomplete)
        self.assertEqual(output, b"")
        self.assertEqual(remaining, incomplete)
        self.assertTrue(needs_more)
    def test_process_packet_with_header_remaining(self):
        """Test that header data is returned in remaining when packet is incomplete."""
        processor = ClockSyncProcessor()
        # 12 bytes header but not enough for full packet
        header_only = b"\x09\x00\x00\x10\x00\x00\x00\x00\x00\x00\x00\x00"
        output, remaining, needs_more = processor.process_packet(header_only)
        self.assertEqual(output, b"")
        self.assertEqual(remaining, header_only)
        self.assertTrue(needs_more)


class TestVideoStreamer(unittest.TestCase):
    """Test cases for VideoStreamer class."""
    def test_initialization(self):
        """Test VideoStreamer initialization."""
        streamer = VideoStreamer(
            stream_index="video1",
            stream_name="main_stream",
            destination=("192.168.1.1", 7447),
            ffmpeg_cmd="ffmpeg -i rtsp://test",
            write_timestamps=True,
        )
        self.assertEqual(streamer.stream_index, "video1")
        self.assertEqual(streamer.stream_name, "main_stream")
        self.assertEqual(streamer.destination, ("192.168.1.1", 7447))
        self.assertEqual(streamer.ffmpeg_cmd, "ffmpeg -i rtsp://test")
        self.assertTrue(streamer.write_timestamps)
        self.assertFalse(streamer._running)
        self.assertIsNone(streamer._ffmpeg_proc)
        self.assertIsNone(streamer._socket)
    def test_initialization_with_logger(self):
        """Test VideoStreamer initialization with custom logger."""
        logger = MagicMock()
        streamer = VideoStreamer(
            stream_index="video1",
            stream_name="test",
            destination=("localhost", 7447),
            ffmpeg_cmd="ffmpeg",
            logger=logger,
        )
        self.assertEqual(streamer.logger, logger)
    def test_socket_connection_error_message(self):
        """Test StreamConnectionError contains proper message."""
        error = StreamConnectionError("Failed to connect to 192.168.1.1:7447 after 5 attempts")
        self.assertIn("Failed to connect", str(error))
        self.assertIn("192.168.1.1:7447", str(error))
    def test_ffmpeg_error_message(self):
        """Test FFmpegError contains proper message."""
        error = FFmpegError("FFmpeg process failed to start")
        self.assertIn("FFmpeg", str(error))


class TestVideoStreamerAsync(unittest.TestCase):
    """Async test cases for VideoStreamer class."""
    def setUp(self):
        """Set up test fixtures."""
        self.streamer = VideoStreamer(
            stream_index="video1",
            stream_name="test_stream",
            destination=("192.168.1.1", 7447),
            ffmpeg_cmd="ffmpeg -i test",
        )
    def test_stop_without_start(self):
        """Test stopping a streamer that hasn't started."""
        # Should not raise any errors
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.streamer.stop())
        finally:
            loop.close()
    @patch('socket.socket')
    def test_connect_socket_success(self, mock_socket_class):
        """Test successful socket connection."""
        mock_socket = MagicMock()
        mock_socket_class.return_value = mock_socket
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Mock the async socket connect
            with patch.object(loop, 'sock_connect', new_callable=AsyncMock) as mock_connect:
                result = loop.run_until_complete(self.streamer._connect_socket(retries=1))
                mock_socket_class.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
                mock_socket.setsockopt.assert_called_once_with(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1
                )
                self.assertEqual(result, mock_socket)
        finally:
            loop.close()
    @patch('socket.socket')
    def test_connect_socket_retry_and_fail(self, mock_socket_class):
        """Test socket connection retry and eventual failure."""
        mock_socket = MagicMock()
        mock_socket_class.return_value = mock_socket
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Make sock_connect raise an exception
            with patch.object(loop, 'sock_connect', side_effect=socket.error("Connection refused")):
                with self.assertRaises(StreamConnectionError) as context:
                    loop.run_until_complete(self.streamer._connect_socket(retries=2, backoff=0.1))
                self.assertIn("Failed to connect", str(context.exception))
                self.assertIn("192.168.1.1:7447", str(context.exception))
        finally:
            loop.close()
    def test_send_data_without_socket(self):
        """Test sending data without a connected socket raises error."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with self.assertRaises(StreamConnectionError) as context:
                loop.run_until_complete(self.streamer._send_data(b"test data"))
            self.assertIn("Socket not connected", str(context.exception))
        finally:
            loop.close()


class TestStreamManager(unittest.TestCase):
    """Test cases for StreamManager class."""
    def test_initialization(self):
        """Test StreamManager initialization."""
        manager = StreamManager()
        self.assertEqual(manager._streamers, {})
        self.assertEqual(manager._tasks, {})
    def test_initialization_with_logger(self):
        """Test StreamManager initialization with custom logger."""
        logger = MagicMock()
        manager = StreamManager(logger=logger)
        self.assertEqual(manager.logger, logger)
    def test_is_streaming_false_when_empty(self):
        """Test is_streaming returns False when no streams exist."""
        manager = StreamManager()
        self.assertFalse(manager.is_streaming("video1"))
        self.assertFalse(manager.is_streaming("video2"))
    def test_stop_all_when_empty(self):
        """Test stop_all when no streams exist."""
        manager = StreamManager()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(manager.stop_all())
        finally:
            loop.close()


class TestStreamManagerAsync(unittest.TestCase):
    """Async test cases for StreamManager class."""
    def test_start_and_stop_stream(self):
        """Test starting and stopping a stream."""
        manager = StreamManager()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Start a stream (will fail because FFmpeg isn't available, but that's okay)
            with patch('unifi.streaming.VideoStreamer.start', new_callable=AsyncMock):
                loop.run_until_complete(manager.start_stream(
                    stream_index="video1",
                    stream_name="test",
                    destination=("localhost", 7447),
                    ffmpeg_cmd="echo test",
                ))
                # Verify stream is tracked
                self.assertTrue(manager.is_streaming("video1"))
                self.assertIn("video1", manager._streamers)
                # Stop the stream
                loop.run_until_complete(manager.stop_stream("video1"))
                # Verify stream is removed
                self.assertFalse(manager.is_streaming("video1"))
                self.assertNotIn("video1", manager._streamers)
        finally:
            loop.close()
    def test_stop_all_streams(self):
        """Test stopping all streams."""
        manager = StreamManager()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with patch('unifi.streaming.VideoStreamer.start', new_callable=AsyncMock):
                # Start multiple streams
                loop.run_until_complete(manager.start_stream(
                    stream_index="video1",
                    stream_name="test1",
                    destination=("localhost", 7447),
                    ffmpeg_cmd="echo test1",
                ))
                loop.run_until_complete(manager.start_stream(
                    stream_index="video2",
                    stream_name="test2",
                    destination=("localhost", 7447),
                    ffmpeg_cmd="echo test2",
                ))
                # Verify both are tracked
                self.assertTrue(manager.is_streaming("video1"))
                self.assertTrue(manager.is_streaming("video2"))
                # Stop all
                loop.run_until_complete(manager.stop_all())
                # Verify all are removed
                self.assertFalse(manager.is_streaming("video1"))
                self.assertFalse(manager.is_streaming("video2"))
                self.assertEqual(manager._streamers, {})
        finally:
            loop.close()
    def test_replace_existing_stream(self):
        """Test that starting a stream with same index replaces existing one."""
        manager = StreamManager()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with patch('unifi.streaming.VideoStreamer.start', new_callable=AsyncMock):
                # Start first stream
                loop.run_until_complete(manager.start_stream(
                    stream_index="video1",
                    stream_name="first",
                    destination=("localhost", 7447),
                    ffmpeg_cmd="echo first",
                ))
                first_streamer = manager._streamers["video1"]
                # Start stream with same index
                loop.run_until_complete(manager.start_stream(
                    stream_index="video1",
                    stream_name="second",
                    destination=("localhost", 7448),
                    ffmpeg_cmd="echo second",
                ))
                # Should have replaced the streamer
                self.assertNotEqual(manager._streamers["video1"], first_streamer)
                self.assertEqual(manager._streamers["video1"].stream_name, "second")
                # Cleanup
                loop.run_until_complete(manager.stop_all())
        finally:
            loop.close()


class TestExceptionClasses(unittest.TestCase):
    """Test custom exception classes in streaming module."""
    def test_stream_error_base(self):
        """Test StreamError base exception."""
        error = StreamError("Base stream error")
        self.assertIsInstance(error, Exception)
        self.assertEqual(str(error), "Base stream error")
    def test_stream_connection_error_inheritance(self):
        """Test StreamConnectionError inherits from StreamError."""
        error = StreamConnectionError("Connection failed")
        self.assertIsInstance(error, StreamError)
        self.assertIsInstance(error, Exception)
    def test_ffmpeg_error_inheritance(self):
        """Test FFmpegError inherits from StreamError."""
        error = FFmpegError("FFmpeg failed")
        self.assertIsInstance(error, StreamError)
        self.assertIsInstance(error, Exception)


if __name__ == '__main__':
    unittest.main()
