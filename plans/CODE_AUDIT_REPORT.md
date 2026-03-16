# Comprehensive Code Audit Report: unifi-cam-proxy

**Audit Date:** 2026-03-16  
**Version Audited:** 0.3.0  
**Auditor:** Code Audit Mode  

---

## Executive Summary

This audit reveals **critical issues** that require immediate attention before any refactoring effort. The most severe finding is a **corrupted core file** ([`unifi/clock_sync.py`](unifi/clock_sync.py)) that contains malformed Python code with syntax errors, duplicated lines, and incomplete functions. Additionally, the project has two competing streaming implementations - a new pure Python approach in [`streaming.py`](unifi/streaming.py) and a legacy shell pipeline approach in [`rlc410.py`](unifi/cams/rlc410.py) - creating confusion and maintenance burden.

### Critical Findings Summary

| Severity | Count | Description |
|----------|-------|-------------|
| 🔴 Critical | 3 | Corrupted file, shell pipeline fragility, SSL verification disabled |
| 🟠 High | 5 | Resource leaks, credential exposure, unpinned dependencies |
| 🟡 Medium | 8 | Code duplication, missing error handling, race conditions |
| 🟢 Low | 6 | Style issues, missing documentation, optimization opportunities |

### Risk Assessment

```
┌─────────────────────────────────────────────────────────────────┐
│                    RISK LEVEL: HIGH                             │
├─────────────────────────────────────────────────────────────────┤
│  • Production deployments may fail due to corrupted file        │
│  • Shell pipelines cause I/O deadlocks under load               │
│  • SSL verification disabled exposes traffic to MITM attacks    │
│  • Credentials visible in process list and logs                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. Architecture Analysis

### 1.1 Project Structure

```
unifi-cam-proxy/
├── unifi/
│   ├── __init__.py          # Empty package marker
│   ├── main.py              # Entry point (191 lines)
│   ├── core.py              # WebSocket connection manager (82 lines)
│   ├── streaming.py         # NEW: Pure Python streaming (503 lines)
│   ├── clock_sync.py        # CORRUPTED: FLV timestamp handling
│   ├── version.py           # Version string
│   └── cams/
│       ├── __init__.py      # Exports RLC410Camera
│       └── rlc410.py        # RLC-410-5MP implementation (1068 lines)
├── docker/
│   └── entrypoint.sh        # Container entry script
├── requirements.txt         # Dependencies
├── Dockerfile               # Multi-stage build
└── plans/                   # Design documents
```

### 1.2 Component Relationships

```mermaid
flowchart TB
    subgraph Entry
        MAIN[main.py]
    end
    
    subgraph Core
        CORE[core.py]
        WS[WebSocket Connection]
    end
    
    subgraph Camera
        RLC[rlc410.py]
        MOTION[Motion Detection]
        STREAM[Video Streaming]
    end
    
    subgraph Streaming Implementations
        PYSTREAM[streaming.py<br/>Pure Python - NEW]
        SHELL[Shell Pipeline<br/>ffmpeg | clock_sync | nc - LEGACY]
        CLOCK[clock_sync.py<br/>CORRUPTED]
    end
    
    subgraph External
        REOLINK[Reolink Camera API]
        UNIFI[UniFi Protect NVR]
        FFMPEG[FFmpeg Process]
    end
    
    MAIN --> CORE
    CORE --> WS
    WS --> RLC
    RLC --> MOTION
    RLC --> STREAM
    STREAM --> SHELL
    STREAM -.-> PYSTREAM
    SHELL --> CLOCK
    SHELL --> FFMPEG
    MOTION --> REOLINK
    STREAM --> UNIFI
    PYSTREAM --> UNIFI
```

### 1.3 Data Flow Analysis

#### Video Streaming Data Flow - Current Implementation

```mermaid
sequenceDiagram
    participant U as UniFi Protect
    participant R as rlc410.py
    participant F as FFmpeg
    participant C as clock_sync.py
    participant N as netcat
    
    U->>R: WebSocket request for video stream
    R->>R: Build shell command
    R->>F: subprocess.Popen with shell=True
    F->>C: stdout pipe
    C->>N: stdout pipe
    N->>U: TCP connection
    
    Note over F,N: Fragile OS-level pipe chain
    Note over C: CORRUPTED FILE
```

#### Video Streaming Data Flow - New Implementation (streaming.py)

```mermaid
sequenceDiagram
    participant U as UniFi Protect
    participant S as streaming.py
    participant F as FFmpeg
    
    U->>S: Request video stream
    S->>F: asyncio subprocess
    F->>S: stdout async read
    S->>S: ClockSyncProcessor inline
    S->>U: socket.sendall
    
    Note over S: Pure Python, no shell
    Note over S: Proper error handling
```

---

## 2. Critical Bugs & Issues

### 2.1 🔴 CRITICAL: Corrupted clock_sync.py File

**Location:** [`unifi/clock_sync.py`](unifi/clock_sync.py)  
**Severity:** Critical  
**Impact:** Runtime failures, syntax errors, production instability  

The [`clock_sync.py`](unifi/clock_sync.py) file is severely corrupted with:

1. **Syntax Errors** - Lines 18-26 have incomplete function definitions:
```python
def read_bytes(source, num_bytes):
    """Read bytes from source with retry on EOF."""
    read_bytes = 0
            return buf  # Indentation error, missing code
        elif d_in:
            return buf
        else:
            return None
```

2. **Broken Exception Handling** - Lines 33-35:
```python
    except (Broken_pipe, Broken pipe error):  # Invalid syntax
    # Gracefully exit
            sys.exit(0)  # Wrong indentation
```

3. **Massive Code Duplication** - Lines 89-230 contain ~140 lines of duplicated assignments:
```python
data["sp"]["min"] = 1500000
data["sp"]["min"] = 1500000
data["sp"]["min"] = 1500000
# ... repeated 140+ times
```

4. **Invalid Syntax** - Line 73:
```python
timestamp = struct.unpack(">i", combined)[0}  # Should be ]
```

5. **Incomplete Code Blocks** - Lines 40-46 reference undefined class context:
```python
            raise StreamConnectionError(
                f"Connection to {self.destination[0]}:{self.destination[1]} lost, {e}"
            )
            except Exception:
                pass
            finally:
                self.close()
```

**Recommendation:** This file needs complete rewrite. The [`streaming.py`](unifi/streaming.py) file already contains a working [`ClockSyncProcessor`](unifi/streaming.py:41) class that should be used instead.

---

### 2.2 🔴 CRITICAL: Fragile Shell Pipeline for Streaming

**Location:** [`unifi/cams/rlc410.py:330-349`](unifi/cams/rlc410.py:330)  
**Severity:** Critical  
**Impact:** I/O deadlocks, broken pipes, stream failures  

The current implementation uses a fragile shell pipeline:

```python
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

**Issues:**

| Issue | Description |
|-------|-------------|
| `shell=True` | Security risk, allows command injection |
| OS-level pipes | Prone to deadlocks when buffers fill |
| No backpressure | Fast FFmpeg can overwhelm slow consumer |
| Silent failures | `stdout=DEVNULL` hides errors |
| Process orphaning | No proper process group management |

**I/O Deadlock Scenario:**

```
┌─────────────────────────────────────────────────────────────────┐
│                     PIPELINE DEADLOCK                            │
├─────────────────────────────────────────────────────────────────┤
│  1. FFmpeg produces data faster than clock_sync can process     │
│  2. OS pipe buffer fills (typically 64KB)                       │
│  3. FFmpeg blocks on write()                                    │
│  4. clock_sync blocks waiting for netcat to read                │
│  5. netcat blocks waiting for UniFi to acknowledge              │
│  6. DEADLOCK - all processes blocked                            │
└─────────────────────────────────────────────────────────────────┘
```

**Recommendation:** Replace with the [`VideoStreamer`](unifi/streaming.py:168) class from [`streaming.py`](unifi/streaming.py) which handles this properly with async I/O.

---

### 2.3 🔴 CRITICAL: SSL Verification Disabled

**Location:** Multiple files  
**Severity:** Critical  
**Impact:** Man-in-the-middle attacks, credential interception  

**[`unifi/core.py:20-24`](unifi/core.py:20):**
```python
self.ssl_context = ssl.create_default_context()
self.ssl_context.check_hostname = False
self.ssl_context.verify_mode = ssl.CERT_NONE
self.ssl_context.load_cert_chain(args.cert, args.cert)
```

**[`unifi/cams/rlc410.py:58-61`](unifi/cams/rlc410.py:58):**
```python
self._ssl_context = ssl.create_default_context()
self._ssl_context.check_hostname = False
self._ssl_context.verify_mode = ssl.CERT_NONE
```

**Recommendation:** Make SSL verification configurable with environment variable, default to enabled:

```python
verify_ssl = os.getenv('UNIFI_VERIFY_SSL', 'true').lower() == 'true'
if verify_ssl:
    self.ssl_context.verify_mode = ssl.CERT_REQUIRED
    self.ssl_context.check_hostname = True
```

---

## 3. High Severity Issues

### 3.1 🟠 HIGH: Credential Exposure

**Location:** Multiple files  
**Severity:** High  
**Impact:** Credential leakage via logs and process list  

**Issues:**

1. **Credentials in RTSP URLs** - [`rlc410.py:154-157`](unifi/cams/rlc410.py:154):
```python
return (
    f"rtsp://{self.args.username}:{self.args.password}@{self.args.ip}:554"
    f"/Preview_{int(self.args.channel) + 1:02}_{stream}"
)
```
This URL with credentials is passed to shell command and logged.

2. **Credentials in HTTP URLs** - [`rlc410.py:137-144`](unifi/cams/rlc410.py:137):
```python
url = (
    f"http://{self.args.ip}"
    f"/cgi-bin/api.cgi?cmd=Snap&channel={self.args.channel}"
    f"&rs=6PHVjvf0UntSLbyT&user={self.args.username}"
    f"&password={self.args.password}"
)
self.logger.info(f"Grabbing snapshot: {url}")  # CREDENTIALS LOGGED!
```

3. **Credentials Visible in Process List** - Shell command with credentials:
```bash
# Visible via `ps aux`
ffmpeg ... -i "rtsp://admin:secretpass@192.168.1.10:554/Preview_01_main"
```

**Recommendation:**
- Use URL-safe encoding or token-based auth where possible
- Sanitize URLs before logging
- Use environment variables or credential files instead of command-line args

---

### 3.2 🟠 HIGH: Resource Leaks

**Location:** [`unifi/cams/rlc410.py`](unifi/cams/rlc410.py)  
**Severity:** High  
**Impact:** Memory leaks, file descriptor exhaustion  

**Issue 1: Temporary File Leak** - [`rlc410.py:261-267`](unifi/cams/rlc410.py:261):
```python
motion_snapshot_path: str = tempfile.NamedTemporaryFile(delete=False).name
try:
    shutil.copyfile(await self.get_snapshot(), motion_snapshot_path)
    # ...
except FileNotFoundError:
    pass  # File never cleaned up on exception
```

**Issue 2: FFmpeg Process Leak** - [`rlc410.py:347-349`](unifi/cams/rlc410.py:347):
```python
self._ffmpeg_handles[stream_index] = subprocess.Popen(
    cmd, stdout=subprocess.DEVNULL, shell=True
)
# No stderr capture, no process monitoring
```

**Issue 3: Socket Not Closed on Error** - [`rlc410.py:951-962`](unifi/cams/rlc410.py:951):
```python
async with aiohttp.ClientSession() as session:
    files = {"payload": open(path, "rb")}  # File handle not guaranteed close
    # ...
```

**Recommendation:** Use context managers consistently:
```python
async with aiohttp.ClientSession() as session:
    async with await anyio.open_file(path, "rb") as f:
        files = {"payload": await f.read()}
```

---

### 3.3 🟠 HIGH: Unpinned Dependencies

**Location:** [`requirements.txt`](requirements.txt)  
**Severity:** High  
**Impact:** Supply chain attacks, reproducibility issues  

```
aiohttp              # No version pin
backoff              # No version pin
coloredlogs          # No version pin
opencv-python        # No version pin
packaging            # No version pin
pydantic<2.0         # Only upper bound
reolinkapi           # No version pin
```

**Issues:**
- 6 dependencies have no version constraints
- GitHub archive dependencies bypass PyPI security
- `pydantic<2.0` allows any 1.x version

**Recommendation:** Pin all dependencies with hashes:
```
aiohttp==3.9.1 \
    --hash=sha256:...
backoff==2.2.1 \
    --hash=sha256:...
```

---

### 3.4 🟠 HIGH: Missing Error Handling in Core Loop

**Location:** [`unifi/cams/rlc410.py:189-228`](unifi/cams/rlc410.py:189)  
**Severity:** High  
**Impact:** Silent failures, infinite loops  

```python
while True:
    self.logger.info(f"Connecting to motion events API: {url}")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(None)
        ) as session:
            while True:  # Inner infinite loop
                async with session.post(encoded_url, data=body) as resp:
                    # No timeout on individual requests
                    # No retry limit
                    # No circuit breaker
```

**Issues:**
- Infinite retry without backoff
- No maximum retry count
- No circuit breaker pattern
- No health check integration

---

### 3.5 🟠 HIGH: Race Condition in Motion Detection

**Location:** [`unifi/cams/rlc410.py:234-267`](unifi/cams/rlc410.py:234)  
**Severity:** High  
**Impact:** Duplicate events, state corruption  

```python
async def trigger_motion_start(self) -> None:
    if not self._motion_event_ts:  # Check-then-act pattern
        # ... state modification
        self._motion_event_ts = time.time()
```

The check-then-act pattern is not atomic. If `trigger_motion_start` is called concurrently:
1. Task A checks `self._motion_event_ts` - finds it None
2. Task B checks `self._motion_event_ts` - finds it None
3. Both tasks proceed to create events

**Recommendation:** Use asyncio.Lock:
```python
def __init__(self):
    self._motion_lock = asyncio.Lock()

async def trigger_motion_start(self) -> None:
    async with self._motion_lock:
        if not self._motion_event_ts:
            # ...
```

---

## 4. Medium Severity Issues

### 4.1 🟡 MEDIUM: Duplicate Streaming Implementations

**Files:** [`unifi/streaming.py`](unifi/streaming.py), [`unifi/cams/rlc410.py`](unifi/cams/rlc410.py)  
**Severity:** Medium  
**Impact:** Maintenance burden, confusion  

The project has two streaming implementations:

| Aspect | streaming.py | rlc410.py shell pipeline |
|--------|-------------|-------------------------|
| Implementation | Pure Python async | Shell command |
| Error handling | Comprehensive | Minimal |
| Reconnection | Built-in backoff | None |
| Process management | asyncio subprocess | subprocess.Popen |
| Status | New, unused | Current, fragile |

The [`StreamManager`](unifi/streaming.py:418) class in [`streaming.py`](unifi/streaming.py) is never used.

**Recommendation:** Complete migration to [`streaming.py`](unifi/streaming.py) and remove shell pipeline.

---

### 4.2 🟡 MEDIUM: Deprecated asyncio Patterns

**Location:** [`unifi/main.py:185-186`](unifi/main.py:185)  
**Severity:** Medium  
**Impact:** Deprecation warnings in Python 3.10+  

```python
def main():
    loop = asyncio.get_event_loop()  # Deprecated
    loop.run_until_complete(run())
```

**Recommendation:**
```python
def main():
    asyncio.run(run())
```

---

### 4.3 🟡 MEDIUM: Missing Type Hints

**Location:** Multiple files  
**Severity:** Medium  
**Impact:** Reduced code clarity, IDE support  

Several functions lack type hints:

```python
# rlc410.py
def _get_stream_info(self) -> tuple[int, int]:  # Good
    ...

def get_base_ffmpeg_args(self, stream_index: str = "") -> str:  # Good
    ...

async def _fetch_to_file(self, url: str, dst: Path) -> bool:  # Good
    ...

# But inconsistent elsewhere
def read_bytes(source, num_bytes):  # No hints
    ...
```

---

### 4.4 🟡 MEDIUM: Hardcoded Configuration Values

**Location:** Multiple files  
**Severity:** Medium  
**Impact:** Inflexibility, deployment issues  

```python
# rlc410.py:79 - Hardcoded bitrate values
data["cs"]["cur"] = 1500000
data["cs"]["max"] = 1500000
data["cs"]["min"] = 1500000

# rlc410.py:253 - Hardcoded timeout
data["cs"]["cur"] = 1500000

# core.py:36 - Hardcoded max backoff
max_value=10,
```

---

### 4.5 🟡 MEDIUM: atexit Handler Reliability

**Location:** [`unifi/cams/rlc410.py:63`](unifi/cams/rlc410.py:63)  
**Severity:** Medium  
**Impact:** Resource leaks on abnormal termination  

```python
atexit.register(self.close_streams)
```

**Issues:**
- `atexit` handlers don't run on SIGKILL
- Handlers run in undefined order
- Not suitable for async cleanup

**Recommendation:** Use proper signal handling:
```python
import signal

def setup_signal_handlers(self):
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
```

---

### 4.6 🟡 MEDIUM: No Health Check Endpoint

**Location:** Architecture  
**Severity:** Medium  
**Impact:** Orchestration difficulty  

The application has no health check mechanism for:
- Kubernetes liveness/readiness probes
- Docker HEALTHCHECK
- Monitoring integration

---

### 4.7 🟡 MEDIUM: Potential Memory Growth in Buffer

**Location:** [`unifi/streaming.py:365`](unifi/streaming.py:365)  
**Severity:** Medium  
**Impact:** Memory exhaustion  

```python
buffer = b""
# ...
buffer += chunk  # Unbounded growth if processing is slow
```

If [`_send_data`](unifi/streaming.py:403) blocks, the buffer grows unboundedly.

**Recommendation:** Implement buffer size limits:
```python
MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB

if len(buffer) > MAX_BUFFER_SIZE:
    self.logger.error("Buffer overflow, dropping data")
    buffer = buffer[-MAX_BUFFER_SIZE:]  # Keep most recent
```

---

### 4.8 🟡 MEDIUM: Inconsistent Logging Levels

**Location:** Multiple files  
**Severity:** Medium  
**Impact:** Debugging difficulty  

```python
# Some important events logged at INFO
self.logger.info(f"Spawning ffmpeg for {stream_index}")

# Some errors logged as warnings
self.logger.warn(f"Previous ffmpeg process for {stream_index} died.")

# Some critical errors just logged
self.logger.error(f"Motion API request failed, retrying.")
```

---

## 5. Low Severity Issues

### 5.1 🟢 LOW: Missing Docstrings

Several methods lack docstrings:
- [`rlc410.py:gen_msg_id()`](unifi/cams/rlc410.py:383)
- [`rlc410.py:gen_response()`](unifi/cams/rlc410.py:388)
- [`rlc410.py:get_uptime()`](unifi/cams/rlc410.py:404)

---

### 5.2 🟢 LOW: Unused Imports

**Location:** [`unifi/cams/rlc410.py`](unifi/cams/rlc410.py)  
```python
import tempfile  # Used
import time      # Used
import urllib.parse  # Used only once, could inline
```

---

### 5.3 🟢 LOW: Magic Numbers

```python
# rlc410.py:150 - Magic number 5 seconds
if self.write_timestamps and (not self.last_ts or now - self.last_ts >= 5):

# streaming.py:253 - Magic number 10 seconds
sock.settimeout(10.0)
```

---

### 5.4 🟢 LOW: Inconsistent String Formatting

Mix of f-strings and format():
```python
uri = "wss://{}:7442/camera/1.0/ws?token={}".format(self.host, self.token)
self.logger.info(f"Creating ws connection to {uri}")
```

---

### 5.5 🟢 LOW: Dockerfile Missing Healthcheck

**Location:** [`Dockerfile`](Dockerfile)  
```dockerfile
# No HEALTHCHECK instruction
```

---

### 5.6 🟢 LOW: Missing .dockerignore Optimizations

**Location:** [`.dockerignore`](.dockerignore)  
Current file is minimal. Should exclude:
- `__pycache__/`
- `.venv/`
- `*.pyc`
- `.git/`
- `plans/`
- `tests/`

---

## 6. Security Vulnerability Assessment

### 6.1 Vulnerability Summary

| CVE Risk | Count | Description |
|----------|-------|-------------|
| Critical | 1 | SSL verification disabled |
| High | 2 | Credential exposure, shell injection potential |
| Medium | 2 | Unpinned dependencies, missing input validation |

### 6.2 Detailed Findings

#### 6.2.1 SSL/TLS Issues

| Location | Issue | Risk |
|----------|-------|------|
| [`core.py:22-23`](unifi/core.py:22) | `CERT_NONE` | MITM |
| [`rlc410.py:59-60`](unifi/cams/rlc410.py:59) | `CERT_NONE` | MITM |
| [`main.py:134`](unifi/main.py:134) | `verify_ssl=False` | MITM |

#### 6.2.2 Command Injection Risk

**Location:** [`rlc410.py:330-349`](unifi/cams/rlc410.py:330)  
**Risk:** Medium (internal network deployment mitigates)

```python
cmd = (
    f"ffmpeg ... -i \"{source}\" ..."  # source contains user input
)
subprocess.Popen(cmd, shell=True)
```

While `source` is constructed from args (not direct user input), `shell=True` with string interpolation is risky.

#### 6.2.3 Input Validation Gaps

No validation of:
- MAC address format
- IP address format
- Port number ranges
- Token format

---

## 7. Dependency Analysis

### 7.1 Current Dependencies

| Package | Constraint | Issue |
|---------|-----------|-------|
| aiohttp | None | Unpinned |
| backoff | None | Unpinned |
| coloredlogs | None | Unpinned |
| flvlib3 | GitHub archive | No version, no hash |
| opencv-python | None | Unpinned, heavy |
| packaging | None | Unpinned |
| pydantic | <2.0 | Only upper bound |
| pyunifiprotect | GitHub archive | No version, no hash |
| reolinkapi | None | Unpinned |
| websockets | >=9.0.1,<13.0 | Acceptable |

### 7.2 Known Vulnerable Versions Check

Based on current constraints:
- `aiohttp <3.8.0` - Multiple CVEs (we have no lower bound)
- `pydantic <1.10.8` - CVE-2022-23831 (we allow all 1.x)

### 7.3 Recommendations

```txt
# Recommended pinned versions with hashes
aiohttp==3.9.1 \
    --hash=sha256:...
backoff==2.2.1 \
    --hash=sha256:...
coloredlogs==15.0.1 \
    --hash=sha256:...
flvlib3==0.1.0 \
    --hash=sha256:...  # Or find PyPI alternative
opencv-python-headless==4.8.1.78 \
    --hash=sha256:...  # Use headless for server
packaging==23.2 \
    --hash=sha256:...
pydantic==1.10.13 \
    --hash=sha256:...
pyunifiprotect==4.20.0 \
    --hash=sha256:...  # Use PyPI version
reolinkapi==0.0.10 \
    --hash=sha256:...
websockets==12.0 \
    --hash=sha256:...
```

---

## 8. Streaming Pipeline Deep Dive

### 8.1 Current Architecture - Shell Pipeline

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        CURRENT STREAMING PIPELINE                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────┐    OS Pipe    ┌─────────────┐    OS Pipe    ┌─────────────┐  │
│  │  FFmpeg  │ ───────────► │ clock_sync  │ ───────────► │   netcat    │  │
│  │          │   stdout     │   (CORRUPT)  │   stdout     │             │  │
│  └──────────┘              └─────────────┘              └─────────────┘  │
│       │                                                        │          │
│       │                                                        │          │
│       ▼                                                        ▼          │
│  RTSP Source                                              TCP to UniFi    │
│                                                                             │
│  PROBLEMS:                                                                  │
│  • clock_sync.py is CORRUPTED - syntax errors, won't run                   │
│  • OS pipes have fixed 64KB buffers - deadlock risk                        │
│  • No backpressure - fast producer overwhelms slow consumer                │
│  • No error propagation - failures are silent                              │
│  • shell=True - security and reliability issues                            │
│  • Process management - orphaned processes on crash                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 8.2 New Architecture - Pure Python (streaming.py)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        NEW STREAMING PIPELINE                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                         VideoStreamer                                │  │
│  │  ┌──────────┐         ┌─────────────────┐         ┌──────────────┐  │  │
│  │  │  FFmpeg  │ async   │ ClockSyncProc   │ async   │   Socket     │  │  │
│  │  │ subprocess│ ─────► │ (inline class)  │ ─────► │   sendall    │  │  │
│  │  │          │  read   │                 │  write  │              │  │  │
│  │  └──────────┘         └─────────────────┘         └──────────────┘  │  │
│  │       │                     │                           │           │  │
│  │       ▼                     ▼                           ▼           │  │
│  │  RTSP Source         FLV Processing              TCP to UniFi       │  │
│  │                      Timestamp Injection                            │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  IMPROVEMENTS:                                                              │
│  • All async - no blocking I/O                                             │
│  • Inline clock sync - no separate process                                 │
│  • Proper error handling with typed exceptions                             │
│  • Exponential backoff for reconnection                                    │
│  • Clean shutdown with timeouts                                            │
│  • StreamManager for multiple streams                                      │
│                                                                             │
│  STATUS: Implemented but NOT USED - rlc410.py still uses shell pipeline    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 8.3 Code Paths Causing I/O Errors

**Path 1: Deadlock on Full Pipe Buffer**

```python
# rlc410.py:347
self._ffmpeg_handles[stream_index] = subprocess.Popen(
    cmd, stdout=subprocess.DEVNULL, shell=True  # stdout discarded!
)
```

Wait - stdout is discarded, so where does the data go? The shell command redirects:
```
ffmpeg ... -f flv - | clock_sync ... | nc ...
```

Each pipe in the chain has a 64KB buffer. If `nc` blocks on TCP write:
1. `clock_sync` blocks on pipe write to `nc`
2. `ffmpeg` blocks on pipe write to `clock_sync`
3. All processes blocked = deadlock

**Path 2: Broken Pipe on Consumer Death**

If `nc` dies (connection lost):
1. `clock_sync` gets SIGPIPE when writing to dead pipe
2. `clock_sync` exits
3. `ffmpeg` gets SIGPIPE when writing to dead pipe
4. `ffmpeg` exits
5. No error reported to Python - silent failure

**Path 3: No Reconnection Logic**

The shell pipeline has no reconnection:
```python
# After starting, Python has no visibility into pipeline health
self._ffmpeg_handles[stream_index] = subprocess.Popen(...)
# Just checks if process is dead to restart
is_dead = has_spawned and self._ffmpeg_handles[stream_index].poll() is not None
```

---

## 9. Refactoring Recommendations

### 9.1 Immediate Actions (Critical)

| Priority | Action | File |
|----------|--------|------|
| 1 | Delete or fix corrupted clock_sync.py | [`unifi/clock_sync.py`](unifi/clock_sync.py) |
| 2 | Migrate to streaming.py VideoStreamer | [`unifi/cams/rlc410.py:321`](unifi/cams/rlc410.py:321) |
| 3 | Add SSL verification option | [`unifi/core.py`](unifi/core.py), [`unifi/cams/rlc410.py`](unifi/cams/rlc410.py) |

### 9.2 Short-term Actions (High)

| Priority | Action | File |
|----------|--------|------|
| 4 | Pin all dependencies | [`requirements.txt`](requirements.txt) |
| 5 | Add asyncio.Lock for motion state | [`unifi/cams/rlc410.py:234`](unifi/cams/rlc410.py:234) |
| 6 | Sanitize credentials in logs | [`unifi/cams/rlc410.py`](unifi/cams/rlc410.py) |
| 7 | Add proper resource cleanup | Multiple |

### 9.3 Medium-term Actions

| Priority | Action | File |
|----------|--------|------|
| 8 | Add health check endpoint | New file |
| 9 | Implement buffer size limits | [`unifi/streaming.py`](unifi/streaming.py) |
| 10 | Add circuit breaker for motion API | [`unifi/cams/rlc410.py`](unifi/cams/rlc410.py) |
| 11 | Update asyncio patterns | [`unifi/main.py`](unifi/main.py) |

---

## 10. Conclusion

This audit reveals significant technical debt and critical issues that must be addressed before any major refactoring. The most urgent issue is the corrupted [`clock_sync.py`](unifi/clock_sync.py) file which will cause runtime failures.

The good news is that [`streaming.py`](unifi/streaming.py) provides a solid foundation for the streaming pipeline refactoring - it just needs to be integrated into the camera implementation.

### Recommended Refactoring Sequence

1. **Fix Critical Bug**: Replace corrupted [`clock_sync.py`](unifi/clock_sync.py) with [`ClockSyncProcessor`](unifi/streaming.py:41) from [`streaming.py`](unifi/streaming.py)
2. **Integrate New Streaming**: Modify [`rlc410.py`](unifi/cams/rlc410.py) to use [`VideoStreamer`](unifi/streaming.py:168) instead of shell pipeline
3. **Security Hardening**: Add SSL verification options and sanitize credentials
4. **Dependency Hygiene**: Pin all versions and remove GitHub archive dependencies
5. **Add Observability**: Health checks, metrics, structured logging

---

## Appendix A: File Integrity Check

| File | Lines | Status | Issues |
|------|-------|--------|--------|
| [`unifi/__init__.py`](unifi/__init__.py) | 1 | ✅ OK | Empty |
| [`unifi/main.py`](unifi/main.py) | 191 | ⚠️ Warning | Deprecated asyncio |
| [`unifi/core.py`](unifi/core.py) | 82 | ⚠️ Warning | SSL disabled |
| [`unifi/streaming.py`](unifi/streaming.py) | 503 | ✅ OK | Unused |
| [`unifi/clock_sync.py`](unifi/clock_sync.py) | 230 | ❌ Corrupt | Syntax errors |
| [`unifi/version.py`](unifi/version.py) | 2 | ✅ OK | - |
| [`unifi/cams/__init__.py`](unifi/cams/__init__.py) | 11 | ✅ OK | - |
| [`unifi/cams/rlc410.py`](unifi/cams/rlc410.py) | 1068 | ⚠️ Warning | Multiple issues |

---

## Appendix B: Quick Reference - Code Locations

### Critical Code Paths

| Function | Location | Purpose |
|----------|----------|---------|
| [`start_video_stream()`](unifi/cams/rlc410.py:321) | rlc410.py:321 | Shell pipeline launch |
| [`VideoStreamer.start()`](unifi/streaming.py:204) | streaming.py:204 | New streaming impl |
| [`ClockSyncProcessor`](unifi/streaming.py:41) | streaming.py:41 | Inline clock sync |
| [`trigger_motion_start()`](unifi/cams/rlc410.py:234) | rlc410.py:234 | Motion event (race condition) |
| [`run()`](unifi/core.py:26) | core.py:26 | WebSocket connection |

### Error Handling Locations

| Location | Issue |
|----------|-------|
| rlc410.py:189-228 | No retry limit |
| rlc410.py:951-962 | File handle leak |
| streaming.py:403-415 | Good error handling |
| core.py:50-65 | Good retry logic |

---

*End of Audit Report*
