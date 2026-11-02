# Strata on Vast

Purpose: Run the pinned Strata NVFP4 engine with model data on a Vast local volume.
Read when: Building the image or starting an instance with this model.

Status: experimental. An RTX 5090 loaded the model and returned `4` through an SSH-forwarded chat API; all 32 model files on the mounted volume matched Google Drive MD5 checksums. RTX 4070 Ti and CMP 170HX passed smoke checks without loading the model or exercising CUDA inference kernels.

- The image uses Vast's CUDA 13.2 mini base and starts Strata under supervisord. Vast SSH launch mode replaces the image entrypoint; use `vast.py rent ... --strata` so the on-start command launches `/opt/instance-tools/bin/entrypoint.sh` and fixes SSH authorized-key permissions.
- The build targets NVIDIA `sm_75`, `sm_80`, `sm_86`, `sm_89`, and `sm_120`; the host CPU needs AVX2. A sufficiently recent NVIDIA driver is required. Compatibility and performance must be checked per host.
- The 120 GiB Qwen model is not included. Mount its volume at `/models` or `/data`; that directory or one direct child containing `pack/index.txt` is detected. `MODEL_DIR` overrides the search roots. Startup checks every model file's expected size; this is not a cryptographic integrity check.
- Vision is optional and requires a separately supplied Qwen vision projector; place it at `/models/mmproj-Qwen3.8-Flash-Next-BF16.gguf` or set `MMPROJ` to its path. Image input has not been validated here.
- The API listens only on `127.0.0.1:8080` in the instance. Use an SSH tunnel; do not publish that port. The Vast SSH setup and the user key must be working.
- Set `STRATA_SMOKE=1` for an inexpensive GPU and binary startup check without mounting or loading the model. A smoke check is not an inference or CUDA-kernel validation.
- Read `/var/log/strata.log` and `/var/log/strata-error.log` inside the instance. Supervisor will not automatically restart a failed startup.
- Model volume and GPU are billed independently. A Vast local volume is tied to its machine and does not reserve its GPU. Interruptible GPU instances may be evicted.

```bash
# From the parent project directory; the GPU offer must be on the volume's machine.
python vast.py rent OFFER_ID --image ghcr.io/sieciowiecxyz/vast-swiss-knife:latest \
  --volume VOLUME_ID --bid BID_USD_PER_HOUR --strata

# Or build from this directory.
podman build -t strata-vast:dev -f Dockerfile .
ssh -p SSH_PORT -L 8080:127.0.0.1:8080 root@SSH_HOST
curl http://127.0.0.1:8080/health
```

The automatic startup is configured in `strata.conf`; model detection and the generated local server config are in `start.py`.
