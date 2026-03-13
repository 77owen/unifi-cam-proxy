# UniFi Camera Proxy for Reolink RLC-410-5MP

A simplified Docker container that allows you to connect a Reolink RLC-410-5MP camera to UniFi Protect without needing a UniFi Cloud account or official support.

## Features

- **Video Streaming**: RTSP streams from camera (main and sub streams)
- **Motion Detection**: HTTP polling via Reolink API
- **Audio Support**: Audio streaming from camera
- **Simple Setup**: Single camera type, minimal configuration

## Requirements

- Docker and Docker Compose
- UniFi Protect NVR (UDM-Pro, UNVR, or similar)
- Reolink RLC-410-5MP camera on the same network
- `ffmpeg` and `nc` (netcat) installed

## Quick Start

### 1. Generate Client Certificate

First, generate a client certificate that will be used to authenticate with UniFi Protect:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -nodes -keyout client.pem -out client.pem -days 36500 -subj "/C=US"
```

### 2. Get Adoption Token

You'll need an adoption token from your UniFi Protect controller. You can generate one using the API:

```bash
# Using curl with your NVR credentials
curl -k -X POST https://<NVR_IP>/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<username>","password":"<password>"}'

# Then get the token
curl -k https://<NVR_IP>/proxy/protect/api/cameras/manage-payload \
  -H "Authorization: Bearer <token_from_login>"
```

The response will contain a `mgmt.token` field - this is your adoption token.

### 3. Configure Environment

Create a `.env` file or modify `docker-compose.yaml` with your settings:

```yaml
environment:
  # UniFi Protect NVR settings
  - "UBIQUITI_ADDRESS=192.168.1.1"    # Your NVR IP
  - "UBIQUITI_PORT=7442"
  - "UBIQUITI_MAC=AABBCCDDEEFF"       # Unique MAC for this camera
  
  # Camera settings
  - "CAMERA_HOST=192.168.1.100"       # Your camera IP
  - "CAMERA_USERNAME=admin"           # Camera username
  - "CAMERA_PASSWORD=password"        # Camera password
```

### 4. Run the Container

```bash
docker-compose up -d
```

## Command Line Usage

You can also run directly without Docker:

```bash
python -m unifi.main \
  --host <NVR_IP>:7442 \
  --token <ADOPTION_TOKEN> \
  --cert client.pem \
  --mac AABBCCDDEEFF \
  --ip <CAMERA_IP> \
  --name "Front Yard Camera" \
  --username admin \
  --password password
```

### Command Line Options

| Option | Description | Required | Default |
|--------|-------------|----------|---------|
| `--host`, `-H` | UniFi Protect NVR address and port | Yes | - |
| `--token`, `-t` | Adoption token | Yes* | - |
| `--cert`, `-c` | Client certificate path | Yes | `client.pem` |
| `--mac`, `-m` | MAC address for camera | No | `AABBCCDDEEFF` |
| `--ip`, `-i` | IP address (display only) | No | `192.168.1.10` |
| `--name`, `-n` | Camera name in UniFi Protect | No | `unifi-cam-proxy` |
| `--model` | Camera model to emulate | No | `UVC G3` |
| `--username`, `-u` | Camera username | Yes | - |
| `--password`, `-p` | Camera password | Yes | - |
| `--stream`, `-m` | Main stream profile | No | `main` |
| `--substream`, `-s` | Sub stream profile | No | `sub` |
| `--rtsp-transport` | RTSP transport protocol | No | `tcp` |
| `--verbose`, `-v` | Enable debug logging | No | False |

*Token can be auto-generated if `--nvr-username` and `--nvr-password` are provided.

## Stream Configuration

The RLC-410-5MP supports two RTSP streams:

- **Main Stream** (`Preview_01_main`): High quality (1920x1080)
- **Sub Stream** (`Preview_01_sub`): Lower quality (for mobile/low bandwidth)

By default, the proxy uses:
- `main` stream for the primary video feed
- `sub` stream for the secondary video feed

You can change these with `--stream` and `--substream` options.

## Troubleshooting

### Camera not appearing in UniFi Protect

1. Verify your token is valid (tokens expire after a period of time)
2. Check that the MAC address is unique (no other camera with same MAC)
3. Ensure the certificate is correctly mounted in the container

### No video stream

1. Verify camera credentials are correct
2. Check camera is accessible from the container/network
3. Verify RTSP URL works: `ffplay rtsp://user:pass@camera_ip:554/Preview_01_main`

### No motion detection

1. Check camera logs for motion API errors
2. Verify motion detection is enabled on the camera
3. Enable verbose logging with `--verbose`

## Building from Source

```bash
git clone https://github.com/your-repo/unifi-cam-proxy.git
cd unifi-cam-proxy
docker build -t unifi-cam-proxy .
```

## License

See [LICENSE](LICENSE) file for details.

## Acknowledgments

This project is a simplified fork of the original [unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) project, specifically tailored for the Reolink RLC-410-5MP camera.
