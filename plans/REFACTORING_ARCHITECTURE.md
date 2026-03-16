# Streaming Pipeline Refactoring Architecture

**Document Version:** 1.0  
**Date:** 2026-03-16  
**Status:** Design Phase  

---

## Executive Summary

This document defines the architecture for refactoring the video streaming pipeline in `unifi-cam-proxy`. The primary goal is to replace the fragile shell pipeline (`ffmpeg | clock_sync | nc`) with the robust pure-Python async implementation that already exists in [`unifi/streaming.py`](../unifi/streaming.py).

### Key Changes

| Component | Current State | Target State |
|-----------|---------------|--------------|
| Video Streaming | Shell pipeline in [`rlc410.py`](../unifi/cams/rlc410.py) | [`VideoStreamer`](../unifi/streaming.py:168) class |
| Clock Sync | Corrupted [`clock_sync.py`](../unifi/clock_sync.py) | [`ClockSyncProcessor`](../unifi/streaming.py:41) inline |
| SSL Verification | Disabled everywhere | Configurable with opt-out |
| Credentials | Exposed in logs/process list | Sanitized |
| Dependencies | Unpinned | Pinned with versions |

---

## 1. Proposed Architecture

### 1.1 High-Level Architecture Diagram

```mermaid
flowchart TB
    subgraph Entry Layer
        MAIN[main.py<br/>Entry Point]
    end
    
    subgraph Core Layer
        CORE[core.py<br/>WebSocket Manager]
        WS[WebSocket Connection<br/>to UniFi Protect]
    end
    
    subgraph Camera Layer
        CAM[RLC410Camera<br/>rlc410.py]
        MOTION[Motion Detection<br/>HTTP Polling]
        PROTO[Protocol Handlers<br/>UBNT AVClient]
    end
    
    subgraph Streaming Layer - REFACTORED
        SM[StreamManager<br/>streaming.py:418]
        VS[VideoStreamer<br/>streaming.py:168]
        CSP[ClockSyncProcessor<br/>streaming.py:41]
    end
    
    subgraph External Systems
        REOLINK[Reolink Camera<br/>RTSP/HTTP API]
        UNIFI[UniFi Protect NVR<br/>WebSocket + TCP]
        FFMPEG[FFmpeg Process<br/>asyncio subprocess]
    end
    
    MAIN --> CORE
    CORE --> WS
    WS --> CAM
    CAM --> MOTION
    CAM --> PROTO
    CAM --> SM
    SM --> VS
    VS --> CSP
    VS --> FFMPEG
    MOTION --> REOLINK
    FFMPEG --> REOLINK
    VS --> UNIFI
    WS --> UNIFI
    
    style SM fill:#90EE90
    style VS fill:#90EE90
    style CSP fill:#90EE90
```

### 1.2 Data Flow Diagram - Proposed Implementation

```mermaid
sequenceDiagram
    participant U as UniFi Protect NVR
    participant C as Core.py
    participant Cam as RLC410Camera
    participant SM as StreamManager
    participant VS as VideoStreamer
    participant F as FFmpeg
    participant R as Reolink Camera
    
    U->>C: WebSocket connection request
    C->>Cam: Initialize adoption
    Cam->>U: ubnt_avclient_hello
    
    U->>Cam: Video stream request
    Cam->>SM: start_stream with params
    SM->>VS: Create VideoStreamer instance
    VS->>F: asyncio.create_subprocess_shell
    F->>R: RTSP connection
    R-->>F: RTSP video stream
    
    loop Streaming Loop
        F-->>VS: stdout FLV data
        VS->>VS: ClockSyncProcessor.process_packet
        VS->>U: socket.sendall processed FLV
    end
    
    Note over VS: On error: reconnect with backoff
    Note over VS: On shutdown: graceful cleanup
```

### 1.3 Component Responsibilities

#### 1.3.1 StreamManager - [`streaming.py:418`](../unifi/streaming.py:418)

**Purpose:** High-level interface for managing multiple concurrent video streams.

**Responsibilities:**
- Create and track `VideoStreamer` instances
- Start/stop streams by stream index
- Manage asyncio tasks for each stream
- Provide status queries for active streams

**Interface:**
```python
class StreamManager:
    async def start_stream(
        self,
        stream_index: str,        # e.g., "video1", "video2"
        stream_name: str,         # Metadata name for UniFi
        destination: Tuple[str, int],  # UniFi NVR host:port
        ffmpeg_cmd: str,          # Complete FFmpeg command
        write_timestamps: bool = False,  # FLV timestamp injection
    ) -> None: ...
    
    async def stop_stream(self, stream_index: str) -> None: ...
    async def stop_all(self) -> None: ...
    def is_streaming(self, stream_index: str) -> bool: ...
    def get_active_streams(self) -> list[str]: ...
```

#### 1.3.2 VideoStreamer - [`streaming.py:168`](../unifi/streaming.py:168)

**Purpose:** Manages a single video stream from FFmpeg to UniFi Protect.

**Responsibilities:**
- FFmpeg subprocess lifecycle management
- TCP socket connection to UniFi Protect
- Clock sync processing via `ClockSyncProcessor`
- Error handling and automatic reconnection
- Graceful shutdown with timeouts

**Interface:**
```python
class VideoStreamer:
    def __init__(
        self,
        stream_index: str,
        stream_name: str,
        destination: Tuple[str, int],
        ffmpeg_cmd: str,
        write_timestamps: bool = False,
        logger: Optional[logging.Logger] = None,
    ): ...
    
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

#### 1.3.3 ClockSyncProcessor - [`streaming.py:41`](../unifi/streaming.py:41)

**Purpose:** Process FLV stream data and inject clock synchronization packets.

**Responsibilities:**
- Parse FLV headers and packets
- Inject `onClockSync` script tags every 5 seconds
- Inject `onMpma` packets for bitrate adaptation
- Write timestamp trailers for each packet

**Interface:**
```python
class ClockSyncProcessor:
    def __init__(
        self,
        write_timestamps: bool = False,
        logger: Optional[logging.Logger] = None,
    ): ...
    
    def process_header(self, data: bytes) -> Tuple[bytes, bytes]: ...
    def process_packet(self, data: bytes) -> Tuple[bytes, bytes, bool]: ...
```

---

## 2. Integration Architecture

### 2.1 RLC410Camera Refactoring

The [`RLC410Camera`](../unifi/cams/rlc410.py:36) class needs to be modified to use `StreamManager` instead of the shell pipeline.

#### 2.1.1 Current Implementation (to be removed)

Location: [`rlc410.py:321-349`](../unifi/cams/rlc410.py:321)

```python
# CURRENT - Shell Pipeline (REMOVE)
async def start_video_stream(
    self, stream_index: str, stream_name: str, destination: tuple[str, int]
):
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
    self._ffmpeg_handles[stream_index] = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, shell=True
    )
```

#### 2.1.2 Proposed Implementation

```python
# PROPOSED - Pure Python Streaming
from unifi.streaming import StreamManager

class RLC410Camera:
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        # ... existing initialization ...
        
        # Replace _ffmpeg_handles dict with StreamManager
        self._stream_manager = StreamManager(logger=logger)
    
    def _build_ffmpeg_cmd(
        self, 
        stream_index: str, 
        stream_name: str,
        source: str
    ) -> str:
        """Build FFmpeg command without shell interpolation."""
        return (
            f"ffmpeg -nostdin -loglevel error -y"
            f" {self.get_base_ffmpeg_args(stream_index)}"
            f" -rtsp_transport {self.args.rtsp_transport}"
            f' -i "{source}"'
            f" {self.get_extra_ffmpeg_args(stream_index)}"
            f" -metadata streamName={stream_name}"
            f" -f flv -"
        )
    
    async def start_video_stream(
        self, 
        stream_index: str, 
        stream_name: str, 
        destination: tuple[str, int]
    ) -> None:
        """Start video stream using StreamManager."""
        source = await self.get_stream_source(stream_index)
        ffmpeg_cmd = self._build_ffmpeg_cmd(stream_index, stream_name, source)
        
        await self._stream_manager.start_stream(
            stream_index=stream_index,
            stream_name=stream_name,
            destination=destination,
            ffmpeg_cmd=ffmpeg_cmd,
            write_timestamps=self._needs_flv_timestamps,
        )
    
    async def stop_video_stream(self, stream_index: str) -> None:
        """Stop video stream via StreamManager."""
        await self._stream_manager.stop_stream(stream_index)
    
    async def close(self) -> None:
        """Clean up all resources."""
        await self._stream_manager.stop_all()
```

### 2.2 Interface Contract

```mermaid
classDiagram
    class RLC410Camera {
        -_stream_manager: StreamManager
        -_needs_flv_timestamps: bool
        +start_video_stream(stream_index, stream_name, destination)
        +stop_video_stream(stream_index)
        +close()
        +get_stream_source(stream_index) str
        +get_base_ffmpeg_args(stream_index) str
        +get_extra_ffmpeg_args(stream_index) str
    }
    
    class StreamManager {
        -_streamers: dict
        -_tasks: dict
        +start_stream(stream_index, stream_name, destination, ffmpeg_cmd, write_timestamps)
        +stop_stream(stream_index)
        +stop_all()
        +is_streaming(stream_index) bool
        +get_active_streams() list
    }
    
    class VideoStreamer {
        +stream_index: str
        +stream_name: str
        +destination: Tuple
        +ffmpeg_cmd: str
        +write_timestamps: bool
        +start()
        +stop()
    }
    
    class ClockSyncProcessor {
        +write_timestamps: bool
        +process_header(data) Tuple
        +process_packet(data) Tuple
    }
    
    RLC410Camera --> StreamManager : uses
    StreamManager --> VideoStreamer : creates/manages
    VideoStreamer --> ClockSyncProcessor : uses
```

---

## 3. Error Handling Strategy

### 3.1 Error Categories and Handling

```mermaid
flowchart TD
    subgraph Error Types
        E1[StreamConnectionError<br/>Socket failures]
        E2[FFmpegError<br/>Process failures]
        E3[StreamError<br/>General stream errors]
        E4[asyncio.CancelledError<br/>Graceful shutdown]
    end
    
    subgraph Recovery Actions
        R1[Exponential Backoff<br/>Reconnect]
        R2[Restart FFmpeg<br/>Process]
        R3[Log and Continue<br/>with retry]
        R4[Clean Shutdown<br/>Exit loop]
    end
    
    E1 --> R1
    E2 --> R2
    E3 --> R3
    E4 --> R4
```

### 3.2 Reconnection Logic with Exponential Backoff

The [`VideoStreamer._connect_socket()`](../unifi/streaming.py:234) method already implements exponential backoff:

```python
async def _connect_socket(self, retries: int = 5, backoff: float = 1.0) -> socket.socket:
    """Connect with exponential backoff."""
    last_error = None
    for attempt in range(retries):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(10.0)
            await asyncio.get_event_loop().sock_connect(sock, self.destination)
            return sock
        except (socket.error, OSError, asyncio.TimeoutError) as e:
            last_error = e
            if attempt < retries - 1:
                wait_time = backoff * (2 ** attempt)  # 1s, 2s, 4s, 8s, 16s
                self.logger.warning(
                    f"Connection attempt {attempt + 1}/{retries} failed: {e}. "
                    f"Retrying in {wait_time:.1f}s..."
                )
                await asyncio.sleep(wait_time)
    
    raise StreamConnectionError(f"Failed after {retries} attempts: {last_error}")
```

### 3.3 Graceful Degradation Flow

```mermaid
flowchart LR
    subgraph Normal Operation
        A[Stream Running] --> B{Error Occurs}
    end
    
    subgraph Error Recovery
        B -->|Connection Lost| C[Log Warning]
        C --> D[Close Socket]
        D --> E[Wait 2s]
        E --> F{Max Retries?}
        F -->|No| G[Reconnect]
        G --> A
        F -->|Yes| H[Raise Exception]
        
        B -->|FFmpeg Died| I[Log Error]
        I --> J[Kill Process]
        J --> E
        
        B -->|Cancelled| K[Log Info]
        K --> L[Break Loop]
    end
    
    subgraph Cleanup
        L --> M[Close Socket]
        H --> M
        M --> N[Kill FFmpeg]
        N --> O[Exit Clean]
    end
```

### 3.4 Proper Cleanup on Shutdown

```python
async def stop(self) -> None:
    """Stop the video stream gracefully with timeouts."""
    self._running = False
    self.logger.info(f"Stopping stream {self.stream_index}")
    
    # 1. Close socket first (stops data flow)
    if self._socket:
        try:
            self._socket.close()
        except Exception:
            pass
        self._socket = None
    
    # 2. Terminate FFmpeg gracefully with timeout
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
```

### 3.5 Handling Feed Interruptions

| Scenario | Detection | Response |
|----------|-----------|----------|
| Socket connection lost | `BrokenPipeError`, `ConnectionResetError` | Log warning, close socket, wait 2s, reconnect |
| FFmpeg process died | `proc.poll() is not None` | Log error, kill process, wait 2s, restart |
| RTSP source timeout | FFmpeg exits with error | Caught as FFmpeg death, restart |
| UniFi NVR reboot | Socket write fails | Connection error triggers reconnect |
| Network partition | Socket timeout | Connection timeout triggers reconnect |

---

## 4. Security Improvements

### 4.1 SSL Verification Configuration

#### 4.1.1 Environment Variable Approach

```python
import os

def create_ssl_context(cert_path: str, verify: bool = True) -> ssl.SSLContext:
    """
    Create SSL context with configurable verification.
    
    Args:
        cert_path: Path to client certificate
        verify: Whether to verify server certificates
    
    Environment:
        UNIFI_VERIFY_SSL: Set to 'false' to disable verification
    """
    context = ssl.create_default_context()
    
    # Allow opt-out via environment variable
    verify_ssl = os.getenv('UNIFI_VERIFY_SSL', 'true').lower() == 'true'
    
    if verify_ssl and verify:
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
    else:
        context.verify_mode = ssl.CERT_NONE
        context.check_hostname = False
        logger.warning(
            "SSL verification disabled. This is insecure and should only be "
            "used for testing or with self-signed certificates."
        )
    
    context.load_cert_chain(cert_path, cert_path)
    return context
```

#### 4.1.2 Command-Line Argument

Add to argument parser:

```python
parser.add_argument(
    "--verify-ssl",
    action="store_true",
    default=True,
    help="Verify SSL certificates (default: True)"
)
parser.add_argument(
    "--no-verify-ssl",
    action="store_true",
    default=False,
    help="Disable SSL verification for self-signed certificates"
)
```

#### 4.1.3 Files to Modify

| File | Location | Change |
|------|----------|--------|
| [`core.py`](../unifi/core.py:20) | `__init__` | Use configurable SSL context |
| [`rlc410.py`](../unifi/cams/rlc410.py:58) | `__init__` | Use configurable SSL context |
| [`main.py`](../unifi/main.py:134) | `generate_token()` | Pass `verify_ssl` to `ProtectApiClient` |

### 4.2 Credential Sanitization

#### 4.2.1 URL Sanitization Utility

```python
import re
from urllib.parse import urlparse, urlunparse

def sanitize_url(url: str) -> str:
    """
    Remove credentials from URL for safe logging.
    
    Examples:
        >>> sanitize_url("rtsp://admin:secretpass@192.168.1.10:554/stream")
        'rtsp://***:***@192.168.1.10:554/stream'
        
        >>> sanitize_url("http://192.168.1.10/api?user=admin&pass=secret")
        'http://192.168.1.10/api?user=***&pass=***'
    """
    # Parse URL and redact credentials in userinfo
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        netloc = f"***:***@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        url = urlunparse(parsed)
    
    # Redact common credential query parameters
    url = re.sub(r'([?&])(user|username|pass|password|token)=([^&]+)', r'\1\2=***', url)
    
    return url
```

#### 4.2.2 Secure Logging Mixin

```python
class SecureLoggingMixin:
    """Mixin providing secure logging methods that sanitize credentials."""
    
    def _log_url(self, url: str, level: int = logging.INFO, msg: str = "") -> None:
        """Log URL with credentials sanitized."""
        safe_url = sanitize_url(url)
        self.logger.log(level, f"{msg}: {safe_url}" if msg else safe_url)
    
    def _log_cmd(self, cmd: str, level: int = logging.INFO, msg: str = "") -> None:
        """Log command with URLs sanitized."""
        safe_cmd = sanitize_url(cmd)
        self.logger.log(level, f"{msg}: {safe_cmd}" if msg else safe_cmd)
```

#### 4.2.3 Files to Modify

| File | Location | Current | Proposed |
|------|----------|---------|----------|
| [`rlc410.py`](../unifi/cams/rlc410.py:143) | `get_snapshot()` | `self.logger.info(f"Grabbing snapshot: {url}")` | `self._log_url(url, msg="Grabbing snapshot")` |
| [`rlc410.py`](../unifi/cams/rlc410.py:345) | `start_video_stream()` | `self.logger.info(f"Spawning ffmpeg...: {cmd}")` | `self._log_cmd(cmd, msg="Spawning ffmpeg")` |

### 4.3 Secure Subprocess Handling

#### 4.3.1 Current Issue

The current implementation uses `shell=True` which:
- Allows command injection if any input is user-controlled
- Shows credentials in process list (`ps aux`)
- Makes process management harder

#### 4.3.2 Proposed Solution

Use `asyncio.create_subprocess_exec()` with argument list:

```python
async def _start_ffmpeg(self) -> asyncio.subprocess.Process:
    """
    Start FFmpeg subprocess without shell.
    
    Uses create_subprocess_exec instead of create_subprocess_shell
    to avoid shell injection risks.
    """
    # Parse command into list of arguments
    # Note: FFmpeg command is built internally, not from user input
    args = shlex.split(self.ffmpeg_cmd)
    
    self.logger.info(f"Starting FFmpeg for {self.stream_index}")
    self._log_cmd(self.ffmpeg_cmd, logging.DEBUG, "FFmpeg command")
    
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,  # Capture stderr for debugging
    )
    
    return proc
```

---

## 5. Dependency Updates Plan

### 5.1 Current Dependencies (Unpinned)

```txt
# Current requirements.txt - PROBLEMATIC
aiohttp                  # No version
backoff                  # No version
coloredlogs              # No version
flvlib3@github archive   # No version, no hash
opencv-python            # No version
packaging                # No version
pydantic<2.0             # Only upper bound
pyunifiprotect@github    # No version, no hash
reolinkapi               # No version
websockets>=9.0.1,<13.0  # Acceptable range
```

### 5.2 Proposed Pinned Dependencies

```txt
# Proposed requirements.txt - PINNED
aiohttp==3.9.3
backoff==2.2.1
coloredlogs==15.0.1
flvlib3==0.2.0
opencv-python-headless==4.9.0.80
packaging==24.0
pydantic==1.10.14
pyunifiprotect==4.20.0
reolinkapi==0.0.10
websockets==12.0
```

### 5.3 Version Selection Rationale

| Package | Version | Rationale |
|---------|---------|-----------|
| `aiohttp` | 3.9.3 | Latest stable, fixes multiple CVEs in <3.8.0 |
| `backoff` | 2.2.1 | Latest stable |
| `coloredlogs` | 15.0.1 | Latest stable |
| `flvlib3` | 0.2.0 | Latest release, avoid GitHub archive |
| `opencv-python-headless` | 4.9.0.80 | Headless for server, no GUI dependencies |
| `packaging` | 24.0 | Latest stable |
| `pydantic` | 1.10.14 | Latest 1.x, avoids breaking 2.x changes |
| `pyunifiprotect` | 4.20.0 | Use PyPI version instead of GitHub |
| `reolinkapi` | 0.0.10 | Latest available |
| `websockets` | 12.0 | Latest stable within compatible range |

### 5.4 API Changes in Updated Packages

#### 5.4.1 pydantic 1.10.x (No Breaking Changes from Current)

The current code uses pydantic features compatible with 1.10.x. No changes required.

#### 5.4.2 websockets 12.0 (Minor Changes)

```python
# Old (websockets <10.0)
await websockets.connect(uri, ssl=ssl_context)

# New (websockets >=10.0) - Current code is already compatible
await websockets.connect(
    uri,
    additional_headers=headers,  # Changed from extra_headers
    ssl=ssl_context,
)
```

The current code at [`core.py:43`](../unifi/core.py:43) uses `extra_headers` which is deprecated but still works. Should update to `additional_headers`.

#### 5.4.3 aiohttp 3.9.x (No Breaking Changes)

Current usage is compatible with 3.9.x.

---

## 6. Migration Path

### 6.1 Migration Phases

```mermaid
flowchart LR
    subgraph Phase 1 - Foundation
        P1A[Pin dependencies]
        P1B[Add SSL config]
        P1C[Add URL sanitization]
    end
    
    subgraph Phase 2 - Streaming
        P2A[Add StreamManager to RLC410Camera]
        P2B[Add feature flag for new streaming]
        P2C[Test with flag enabled]
    end
    
    subgraph Phase 3 - Cutover
        P3A[Remove shell pipeline]
        P3B[Delete clock_sync.py]
        P3C[Remove feature flag]
    end
    
    subgraph Phase 4 - Cleanup
        P4A[Update documentation]
        P4B[Remove deprecated code]
        P4C[Release new version]
    end
    
    P1A --> P1B --> P1C --> P2A --> P2B --> P2C --> P3A --> P3B --> P3C --> P4A --> P4B --> P4C
```

### 6.2 Detailed Migration Steps

#### Phase 1: Foundation (No Breaking Changes)

| Step | File | Change | Risk |
|------|------|--------|------|
| 1.1 | `requirements.txt` | Pin all dependencies | Low |
| 1.2 | `unifi/core.py` | Add `UNIFI_VERIFY_SSL` env var | Low |
| 1.3 | `unifi/cams/rlc410.py` | Add `UNIFI_VERIFY_SSL` support | Low |
| 1.4 | `unifi/main.py` | Add `--no-verify-ssl` argument | Low |
| 1.5 | `unifi/cams/rlc410.py` | Add `sanitize_url()` function | Low |
| 1.6 | `unifi/cams/rlc410.py` | Replace log statements with sanitized versions | Low |

#### Phase 2: Streaming Integration (Feature Flag)

| Step | File | Change | Risk |
|------|------|--------|------|
| 2.1 | `unifi/cams/rlc410.py` | Import `StreamManager` | Low |
| 2.2 | `unifi/cams/rlc410.py` | Add `self._stream_manager` in `__init__` | Low |
| 2.3 | `unifi/cams/rlc410.py` | Add `UNIFI_USE_PYTHON_STREAMING` env var | Low |
| 2.4 | `unifi/cams/rlc410.py` | Add conditional: if env var, use `StreamManager` | Medium |
| 2.5 | `unifi/cams/rlc410.py` | Add `_build_ffmpeg_cmd()` method | Low |
| 2.6 | Tests | Test with `UNIFI_USE_PYTHON_STREAMING=true` | Medium |

#### Phase 3: Cutover (Breaking Change)

| Step | File | Change | Risk |
|------|------|--------|------|
| 3.1 | `unifi/cams/rlc410.py` | Remove shell pipeline code | High |
| 3.2 | `unifi/cams/rlc410.py` | Remove `_ffmpeg_handles` dict | Medium |
| 3.3 | `unifi/clock_sync.py` | Delete file (or mark deprecated) | Medium |
| 3.4 | `unifi/cams/rlc410.py` | Remove feature flag check | Low |
| 3.5 | `unifi/main.py` | Remove `nc` from preflight checks | Low |

#### Phase 4: Cleanup

| Step | File | Change | Risk |
|------|------|--------|------|
| 4.1 | `unifi/core.py` | Update `websockets` to use `additional_headers` | Low |
| 4.2 | `unifi/main.py` | Update deprecated asyncio pattern | Low |
| 4.3 | Documentation | Update README with new env vars | Low |
| 4.4 | `Dockerfile` | Remove `nc` dependency if not needed elsewhere | Low |

### 6.3 Backward Compatibility

#### 6.3.1 Environment Variables (Additive)

New environment variables are additive and don't break existing deployments:

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNIFI_VERIFY_SSL` | `true` | Control SSL verification |
| `UNIFI_USE_PYTHON_STREAMING` | `false` (Phase 2) then `true` (Phase 3) | Feature flag for new streaming |

#### 6.3.2 Command-Line Arguments (Additive)

New arguments are additive:

```bash
# New optional arguments
--no-verify-ssl          # Disable SSL verification
```

#### 6.3.3 Behavior Changes

| Behavior | Before | After | Compatibility |
|----------|--------|-------|---------------|
| SSL Verification | Disabled | Enabled | **Breaking** - self-signed certs need `--no-verify-ssl` |
| Streaming | Shell pipeline | Python async | Transparent to user |
| Log output | Contains credentials | Sanitized | Transparent to user |

### 6.4 Rollback Plan

If issues arise after deployment:

1. **Phase 2 (Feature Flag)**: Set `UNIFI_USE_PYTHON_STREAMING=false`
2. **Phase 3 (Cutover)**: Revert to previous version or set `UNIFI_USE_PYTHON_STREAMING=false` if flag retained
3. **SSL Issues**: Set `UNIFI_VERIFY_SSL=false` or use `--no-verify-ssl`

---

## 7. Code Change Impact Analysis

### 7.1 Files Modified

| File | Lines Changed | Complexity | Risk |
|------|---------------|------------|------|
| `unifi/cams/rlc410.py` | ~50 | Medium | High |
| `unifi/core.py` | ~10 | Low | Low |
| `unifi/main.py` | ~15 | Low | Low |
| `requirements.txt` | ~10 | Low | Low |

### 7.2 Files Deleted

| File | Reason |
|------|--------|
| `unifi/clock_sync.py` | Replaced by `ClockSyncProcessor` in `streaming.py` |

### 7.3 Files Unchanged

| File | Reason |
|------|--------|
| `unifi/streaming.py` | Already implements required functionality |
| `unifi/version.py` | Only version bump needed |
| `unifi/__init__.py` | Empty file, no changes |

### 7.4 Test Coverage Requirements

| Component | Test Type | Coverage Needed |
|-----------|-----------|-----------------|
| `StreamManager` | Unit | start/stop/is_streaming |
| `VideoStreamer` | Integration | FFmpeg mock, socket mock |
| `ClockSyncProcessor` | Unit | Header/packet processing |
| `sanitize_url()` | Unit | Various URL formats |
| SSL configuration | Integration | With/without verification |
| Error handling | Integration | Connection failures, timeouts |

---

## 8. Migration Checklist

### Pre-Migration

- [ ] Create backup of current working version
- [ ] Document current behavior with test recordings
- [ ] Verify `streaming.py` works in isolation
- [ ] Stage dependencies in test environment

### Phase 1: Foundation

- [ ] Update `requirements.txt` with pinned versions
- [ ] Run `pip install -r requirements.txt` in clean environment
- [ ] Add `UNIFI_VERIFY_SSL` environment variable support
- [ ] Add `sanitize_url()` function
- [ ] Update logging to use sanitized URLs
- [ ] Test SSL verification with valid and self-signed certs
- [ ] Verify logs don't contain credentials

### Phase 2: Streaming Integration

- [ ] Import `StreamManager` in `rlc410.py`
- [ ] Add `_stream_manager` instance variable
- [ ] Add `_build_ffmpeg_cmd()` method
- [ ] Add `UNIFI_USE_PYTHON_STREAMING` feature flag
- [ ] Add conditional streaming logic
- [ ] Test with feature flag disabled (shell pipeline)
- [ ] Test with feature flag enabled (Python streaming)
- [ ] Compare stream quality between implementations

### Phase 3: Cutover

- [ ] Remove shell pipeline code from `start_video_stream()`
- [ ] Remove `_ffmpeg_handles` dict
- [ ] Update `stop_video_stream()` to use `StreamManager`
- [ ] Update `close()` to call `_stream_manager.stop_all()`
- [ ] Mark or delete `clock_sync.py`
- [ ] Remove `nc` from preflight checks
- [ ] Remove feature flag (default to Python streaming)
- [ ] Full integration test

### Phase 4: Cleanup

- [ ] Update `websockets` call to use `additional_headers`
- [ ] Update `main.py` to use `asyncio.run()`
- [ ] Update README with new environment variables
- [ ] Update Dockerfile if `nc` no longer needed
- [ ] Add `.dockerignore` optimizations
- [ ] Version bump
- [ ] Create release notes

### Post-Migration Verification

- [ ] Stream video for 24+ hours without errors
- [ ] Test reconnection after network interruption
- [ ] Test graceful shutdown
- [ ] Verify no credential leakage in logs
- [ ] Verify SSL verification works correctly
- [ ] Performance comparison (CPU, memory)

---

## 9. Risk Assessment

### 9.1 Risk Matrix

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| SSL verification breaks existing deployments | High | Medium | Default to disabled, document clearly |
| Streaming quality degradation | Low | High | Feature flag for rollback |
| FFmpeg compatibility issues | Low | Medium | Same FFmpeg command, different invocation |
| Memory leak in Python streaming | Medium | Medium | Buffer size limits, monitoring |
| Connection instability | Medium | High | Exponential backoff, comprehensive logging |

### 9.2 Mitigation Strategies

1. **Feature Flag**: Keep ability to revert to shell pipeline during Phase 2
2. **Gradual Rollout**: Test in staging before production
3. **Monitoring**: Add health checks and metrics for stream status
4. **Documentation**: Clear upgrade guide with breaking changes
5. **Rollback Plan**: Documented procedure to revert changes

---

## 10. Success Criteria

### 10.1 Functional Requirements

- [ ] Video streams successfully to UniFi Protect
- [ ] Clock sync packets injected correctly
- [ ] Motion detection continues to work
- [ ] Reconnection works after network interruption
- [ ] Graceful shutdown without orphaned processes

### 10.2 Non-Functional Requirements

- [ ] No credentials in logs or process list
- [ ] SSL verification enabled by default
- [ ] All dependencies pinned to specific versions
- [ ] No shell=True in subprocess calls
- [ ] Memory usage stable over 24+ hours

### 10.3 Quality Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Stream uptime | ~95% (deadlocks) | >99.9% |
| Reconnection time | N/A (manual restart) | <10s automatic |
| Memory growth | Unknown | <100MB/hour |
| Log security | Credentials exposed | Sanitized |

---

## Appendix A: Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `UNIFI_VERIFY_SSL` | `true` | Set to `false` to disable SSL verification |
| `UNIFI_USE_PYTHON_STREAMING` | `true` (Phase 3) | Set to `false` to use legacy shell pipeline |

## Appendix B: Command-Line Arguments Reference

| Argument | Default | Description |
|----------|---------|-------------|
| `--verify-ssl` | True | Verify SSL certificates |
| `--no-verify-ssl` | False | Disable SSL verification |

## Appendix C: Related Documents

- [Code Audit Report](./CODE_AUDIT_REPORT.md)
- [Simplified Design](./SIMPLIFIED_DESIGN.md)
- [Build Process Analysis](./BUILD_PROCESS_ANALYSIS.md)

---

*End of Architecture Document*
