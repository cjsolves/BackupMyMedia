# Adding a Second Upscale Node

Any machine with Docker and an NVIDIA GPU (or a fast CPU) can join the upscaling queue. Each movie is allocated exclusively to one node — they never share a single file.

## How it works

The second node runs in **remote mode**:
1. Polls the pipeline API every 30 seconds for a queued job
2. Claims it atomically (the pipeline marks it as owned by this node)
3. Downloads the source MKV directly from the pipeline over HTTP — **no NAS mount needed**
4. Upscales locally with Real-ESRGAN
5. Uploads the finished file back; the pipeline places it on the NAS and re-queues for Tdarr

Progress is visible on the dashboard alongside the primary node, labelled by `NODE_ID`.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker Desktop or Docker Engine | Windows, Linux, or macOS |
| NVIDIA GPU + Container Toolkit | Optional — set `USE_GPU=false` for CPU-only (much slower) |
| Network access to main machine port `8090` | Must be reachable from this machine |
| Git | To clone the repo |

### NVIDIA Container Toolkit (GPU support)

**Linux:**
```bash
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/libnvidia-container/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

**Windows (Docker Desktop):**
Install the latest NVIDIA Game Ready or Studio driver — Docker Desktop picks it up automatically via WSL2.

---

## Setup steps

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/BackupMyMedia.git
cd BackupMyMedia/upscaler-node
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and set `PIPELINE_API` to the **local network IP** of your main machine:

```
PIPELINE_API=http://192.168.1.XXX:8090
```

Find the IP on the main machine:
- **Windows:** `ipconfig` → look for IPv4 Address under your network adapter
- **Linux:** `ip addr show` or `hostname -I`

> **Firewall note:** Port 8090 must be reachable from this machine.  
> On Windows: *Windows Defender Firewall → Allow an app → Docker Desktop Backend*  
> Or add an inbound rule for TCP 8090.

### 3. Start the node

```bash
docker compose up -d
```

The first start downloads the Real-ESRGAN model (~65 MB) and builds the image. This takes a few minutes.

### 4. Verify it's working

```bash
docker logs media-upscaler-node2 -f
```

You should see:
```
=== High-Quality AI Video Upscaler ===
  Node=node2 | Mode=remote | Model=RealESRGAN_x4plus ...
...
[node2] Starting job: Some Movie (2003)
[Some Movie (2003)] Downloading: 12%
```

On the pipeline dashboard (`http://MAIN_MACHINE_IP:8090`) the movie card will show:
```
⚡ 8% · node2
```

---

## Changing the node name

Edit `NODE_ID` in your `.env` file before starting. The name appears on the dashboard so you can tell nodes apart.

```
NODE_ID=garage-pc
```

---

## Running CPU-only (no GPU)

Set in `.env`:
```
USE_GPU=false
```

Remove the `deploy:` block from `docker-compose.yml` (the GPU reservation will fail without the Container Toolkit).

Expect roughly 10–30× longer processing time versus GPU.

---

## Stopping the node

```bash
docker compose down
```

The current job saves a checkpoint before stopping. The next time you start the node it resumes from where it left off.

If the node is offline for more than 24 hours while a job is claimed, the pipeline automatically re-queues that job so another node (or the primary) can pick it up.

---

## Multiple additional nodes

Each additional node needs a unique `NODE_ID` in its `.env`. You can run this `docker-compose.yml` on as many machines as you have, all pointing at the same `PIPELINE_API`.
