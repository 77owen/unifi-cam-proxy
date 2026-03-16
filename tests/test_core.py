"""
Tests for unifi/core.py module.

Tests the following components:
- sanitize_url(): URL credential redaction
- get_ssl_context(): SSL context creation with environment variable support
- Core class: WebSocket management and error handling
"""

import os
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from unifi.core import (
    Core,
    PermanentError,
    RetryableError,
    get_ssl_context,
    sanitize_url,
)


class TestSanitizeUrl(unittest.TestCase):
    """Test cases for URL sanitization function."""

    def test_rtsp_url_with_credentials(self):
        """Test sanitizing RTSP URL with username and password."""
        url = "rtsp://admin:secretpassword@192.168.1.100:554/stream1"
        expected = "rtsp://admin:****@192.168.1.100:554/stream1"
        self.assertEqual(sanitize_url(url), expected)

    def test_http_url_with_credentials(self):
        """Test sanitizing HTTP URL with username and password."""
        url = "http://user:mypassword@example.com/api/endpoint"
        expected = "http://user:****@example.com/api/endpoint"
        self.assertEqual(sanitize_url(url), expected)

    def test_https_url_with_credentials(self):
        """Test sanitizing HTTPS URL with username and password."""
        url = "https://admin:secret123@secure.example.com:8443/path"
        expected = "https://admin:****@secure.example.com:8443/path"
        self.assertEqual(sanitize_url(url), expected)

    def test_rtmp_url_with_credentials(self):
        """Test sanitizing RTMP URL with username and password."""
        url = "rtmp://streamer:pass123@streaming.example.com/live/stream"
        expected = "rtmp://streamer:****@streaming.example.com/live/stream"
        self.assertEqual(sanitize_url(url), expected)

    def test_url_without_credentials(self):
        """Test URL without credentials remains unchanged."""
        url = "rtsp://192.168.1.100:554/stream1"
        self.assertEqual(sanitize_url(url), url)

    def test_url_with_special_chars_in_password(self):
        """Test URL with special characters in password.
        
        Note: The regex only matches up to the first @ character,
        so passwords containing @ are partially redacted.
        """
        url = "rtsp://admin:p@ss:w0rd@192.168.1.100:554/stream"
        # The regex matches p@ss:w0rd as the password (up to the last @ before host)
        result = sanitize_url(url)
        # Password is redacted but contains @ characters
        self.assertIn("****", result)
        self.assertIn("192.168.1.100:554/stream", result)

    def test_url_with_password_in_query_params(self):
        """Test URL with password in query parameters is redacted."""
        url = "http://example.com/api?username=admin&password=secret123"
        expected = "http://example.com/api?username=admin&password=****"
        self.assertEqual(sanitize_url(url), expected)

    def test_url_with_pass_in_query_params(self):
        """Test URL with 'pass' in query parameters is redacted."""
        url = "http://example.com/api?user=admin&pass=mysecret"
        expected = "http://example.com/api?user=admin&pass=****"
        self.assertEqual(sanitize_url(url), expected)

    def test_url_with_pwd_in_query_params(self):
        """Test URL with 'pwd' in query parameters is redacted."""
        url = "http://example.com/login?pwd=topsecret"
        expected = "http://example.com/login?pwd=****"
        self.assertEqual(sanitize_url(url), expected)

    def test_url_with_case_insensitive_password_param(self):
        """Test password redaction is case-insensitive."""
        url = "http://example.com/api?PASSWORD=secret"
        expected = "http://example.com/api?PASSWORD=****"
        self.assertEqual(sanitize_url(url), expected)

    def test_url_with_both_credentials_and_query_password(self):
        """Test URL with both credential password and query parameter password."""
        url = "http://admin:secret@example.com/api?password=anothersecret"
        expected = "http://admin:****@example.com/api?password=****"
        self.assertEqual(sanitize_url(url), expected)

    def test_empty_url(self):
        """Test empty URL returns empty string."""
        self.assertEqual(sanitize_url(""), "")

    def test_url_with_empty_password(self):
        """Test URL with empty password.
        
        Note: The regex doesn't match empty passwords - it actual behavior is"""
        url = "rtsp://admin:@192.168.1.100:554/stream"
        # Actual behavior: empty passwords aren't matched
        result = sanitize_url(url)
        # Empty password means no replacement
        self.assertEqual(result, "rtsp://admin:@192.168.1.100:554/stream")


class TestGetSslContext(unittest.TestCase):
    """Test cases for SSL context creation."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a temporary certificate file for testing
        self.temp_cert = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
        # Write minimal cert content (not a valid cert, but sufficient for testing context creation)
        self.temp_cert.write("dummy cert content")
        self.temp_cert.close()
        self.cert_path = self.temp_cert.name

    def tearDown(self):
        """Clean up test fixtures."""
        # Remove temporary certificate file
        if os.path.exists(self.cert_path):
            os.unlink(self.cert_path)
        # Reset environment variable
        if 'UNIFI_VERIFY_SSL' in os.environ:
            del os.environ['UNIFI_VERIFY_SSL']

    @patch('ssl.create_default_context')
    def test_ssl_context_with_verification_enabled(self, mock_create_context):
        """Test SSL context creation with verification enabled (default)."""
        mock_context = MagicMock(spec=ssl.SSLContext)
        mock_create_context.return_value = mock_context
        
        # Ensure UNIFI_VERIFY_SSL is not set (defaults to true)
        os.environ.pop('UNIFI_VERIFY_SSL', None)
        
        result = get_ssl_context(self.cert_path)
        
        mock_create_context.assert_called_once()
        mock_context.load_cert_chain.assert_called_once_with(self.cert_path, self.cert_path)
        self.assertEqual(result, mock_context)

    @patch('ssl.create_default_context')
    def test_ssl_context_with_verification_disabled(self, mock_create_context):
        """Test SSL context creation with verification disabled."""
        mock_context = MagicMock(spec=ssl.SSLContext)
        mock_create_context.return_value = mock_context
        
        os.environ['UNIFI_VERIFY_SSL'] = 'false'
        
        result = get_ssl_context(self.cert_path)
        
        mock_create_context.assert_called_once()
        mock_context.load_cert_chain.assert_called_once_with(self.cert_path, self.cert_path)
        self.assertEqual(mock_context.check_hostname, False)
        self.assertEqual(mock_context.verify_mode, ssl.CERT_NONE)
        self.assertEqual(result, mock_context)

    @patch('ssl.create_default_context')
    def test_ssl_context_with_verification_disabled_case_insensitive(self, mock_create_context):
        """Test SSL context creation with various case variations of 'false'."""
        for value in ['FALSE', 'False', 'false', 'FaLsE']:
            mock_context = MagicMock(spec=ssl.SSLContext)
            mock_create_context.return_value = mock_context
            
            os.environ['UNIFI_VERIFY_SSL'] = value
            
            result = get_ssl_context(self.cert_path)
            
            self.assertEqual(mock_context.verify_mode, ssl.CERT_NONE)
            self.assertEqual(mock_context.check_hostname, False)

    @patch('ssl.create_default_context')
    def test_ssl_context_with_verification_enabled_explicit(self, mock_create_context):
        """Test SSL context creation with verification explicitly enabled."""
        mock_context = MagicMock(spec=ssl.SSLContext)
        mock_create_context.return_value = mock_context
        
        os.environ['UNIFI_VERIFY_SSL'] = 'true'
        
        result = get_ssl_context(self.cert_path)
        
        # Should NOT modify verify_mode or check_hostname when verification is enabled
        self.assertNotEqual(mock_context.verify_mode, ssl.CERT_NONE)


class TestCoreClass(unittest.TestCase):
    """Test cases for Core class."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_args = MagicMock()
        self.mock_args.host = "192.168.1.1"
        self.mock_args.token = "test-token-123"
        self.mock_args.mac = "AA:BB:CC:DD:EE:FF"
        self.mock_args.cert = "/path/to/cert.pem"
        
        self.mock_camera = MagicMock()
        self.mock_logger = MagicMock()

    @patch('unifi.core.get_ssl_context')
    def test_core_initialization(self, mock_get_ssl):
        """Test Core class initialization."""
        mock_ssl_context = MagicMock(spec=ssl.SSLContext)
        mock_get_ssl.return_value = mock_ssl_context
        
        core = Core(self.mock_args, self.mock_camera, self.mock_logger)
        
        self.assertEqual(core.host, "192.168.1.1")
        self.assertEqual(core.token, "test-token-123")
        self.assertEqual(core.mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(core.cam, self.mock_camera)
        self.assertEqual(core.logger, self.mock_logger)
        self.assertEqual(core.ssl_context, mock_ssl_context)
        self.assertFalse(core._shutdown_requested)

    @patch('unifi.core.get_ssl_context')
    def test_request_shutdown(self, mock_get_ssl):
        """Test shutdown request functionality."""
        mock_get_ssl.return_value = MagicMock(spec=ssl.SSLContext)
        
        core = Core(self.mock_args, self.mock_camera, self.mock_logger)
        
        self.assertFalse(core._shutdown_requested)
        
        core.request_shutdown()
        
        self.assertTrue(core._shutdown_requested)
        self.mock_logger.info.assert_called_once_with("Shutdown requested")


class TestExceptionClasses(unittest.TestCase):
    """Test custom exception classes."""

    def test_retryable_error(self):
        """Test RetryableError exception."""
        error = RetryableError("Test retryable error")
        self.assertIsInstance(error, Exception)
        self.assertEqual(str(error), "Test retryable error")

    def test_permanent_error(self):
        """Test PermanentError exception."""
        error = PermanentError("Test permanent error")
        self.assertIsInstance(error, Exception)
        self.assertEqual(str(error), "Test permanent error")

    def test_retryable_error_inheritance(self):
        """Test RetryableError is catchable as Exception."""
        with self.assertRaises(Exception):
            raise RetryableError("Should be caught")

    def test_permanent_error_inheritance(self):
        """Test PermanentError is catchable as Exception."""
        with self.assertRaises(Exception):
            raise PermanentError("Should be caught")


if __name__ == '__main__':
    unittest.main()
