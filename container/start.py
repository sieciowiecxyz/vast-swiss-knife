"""Start Strata with a pre-existing Qwen NVFP4 model mounted at /models."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path('/opt/strata')
sys.path.insert(0, str(ROOT))
import setup


def check_hardware():
    if platform.machine() not in ('x86_64', 'AMD64'):
        raise ValueError('This image supports x86-64 CPUs only')
    cpuinfo = Path('/proc/cpuinfo').read_text()
    flags = next((line.partition(':')[2].split() for line in cpuinfo.splitlines()
                  if line.startswith('flags')), [])
    if not {'avx2', 'fma', 'f16c', 'bmi2'}.issubset(flags):
        raise ValueError('CPU must support AVX2, FMA, F16C and BMI2')
    result = subprocess.run(['nvidia-smi', '--query-gpu=name,compute_cap,driver_version,memory.total',
                             '--format=csv,noheader,nounits'], check=True, text=True, capture_output=True)
    rows = result.stdout.strip().splitlines()
    if not rows:
        raise ValueError('No NVIDIA GPU available')
    name, capability, driver, memory = [x.strip() for x in rows[0].split(',')]
    if capability not in ('7.5', '8.0', '8.6', '8.9', '12.0'):
        raise ValueError(f'Unsupported GPU compute capability {capability}')
    if int(driver.split('.')[0]) < 580:
        raise ValueError(f'NVIDIA driver {driver} is older than 580')
    if float(memory) < 11500:
        raise ValueError(f'{name} has less than 11,500 MiB VRAM')
    print(f'GPU: {name}; compute {capability}; driver {driver}; VRAM {memory} MiB', flush=True)


def find_model(roots):
    candidates = []
    for root in roots:
        candidates.extend((root, root / 'qwen'))
        if root.is_dir():
            candidates.extend(p for p in root.iterdir() if p.is_dir())
    found = {p.resolve() for p in candidates if (p / 'pack' / 'index.txt').is_file()}
    if len(found) != 1:
        raise ValueError(f'Expected exactly one Qwen pack under {roots}; found {len(found)}')
    return found.pop()


def effective_memory():
    limits = [os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')]
    for path in (Path('/sys/fs/cgroup/memory.max'), Path('/sys/fs/cgroup/memory/memory.limit_in_bytes')):
        try:
            value = path.read_text().strip()
        except FileNotFoundError:
            continue
        if value.isdigit() and int(value) > 0:
            limits.append(int(value))
    return min(limits)


def secure_ssh_key(path=Path('/root/.ssh/authorized_keys')):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def main():
    secure_ssh_key()
    check_hardware()
    if os.environ.get('STRATA_SMOKE') == '1':
        subprocess.run([str(ROOT / 'engine/strata'), '--help'], check=True)
        print('Strata binary and GPU smoke check passed', flush=True)
        return
    configured = os.environ.get('MODEL_DIR')
    roots = [Path(configured)] if configured else [Path('/models'), Path('/data')]
    root = find_model(roots)
    files, error = setup.nvfp4_files('qwen-nvfp4-gptq')
    if error:
        raise ValueError(error)
    for _, _, _, rel, size, _ in files:
        path = root / rel
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError(f'Missing or incomplete model file: {path}')
    paths = {'pack': root / 'pack', 'dense': root / 'qwen-nvfp4-gptq-dense.gguf',
             'ple': root / 'ple-fp8.gguf', 'embd': root / 'token-embd-bf16.gguf',
             'mtp': root / 'mtp', 'profile': ROOT / 'data/expert-profile.bin'}
    memory = effective_memory()
    extra = ['--low-ram'] if memory < 92 * 1024**3 else []
    print(f'Effective memory limit: {memory / 1024**3:.1f} GiB; low-RAM mode: {bool(extra)}', flush=True)
    args = setup.nvfp4_args(paths, 32768, 'int8', 'none', extra=extra)
    runtime = Path('/run/strata-vast')
    if runtime.is_symlink():
        raise ValueError('Runtime directory must not be a symlink')
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    cfg = {'exe': str(ROOT / 'engine/strata'), 'args': args, 'cwd': str(ROOT),
           'tokenizer': str(root / 'pack/tokenizer'), 'model_name': 'qwen3.8-flash-next-gptq-nvfp4',
           'log': str(runtime / 'engine.log'), 'host': '127.0.0.1', 'port': 8080,
           'gpu': 0, 'gpus_asked': True, 'open_browser': False}
    vision = Path(os.environ.get('MMPROJ', '/models/mmproj-Qwen3.8-Flash-Next-BF16.gguf'))
    if vision.is_file():
        cfg['args'].append('--vision')
        cfg['vision'] = {'exe': str(ROOT / 'engine/strata-vision'), 'mmproj': str(vision),
                         'model': str(paths['dense']), 'gpu': False, 'max_tokens': 1024}
    else:
        print(f'Vision disabled: encoder not found at {vision}', flush=True)
    config = runtime / 'config.json'
    config.write_text(json.dumps(cfg))
    print(f'Using model {root}; API on 127.0.0.1:8080', flush=True)
    os.execv(str(ROOT / '.venv/bin/python'), [str(ROOT / '.venv/bin/python'),
             str(ROOT / 'serve/server.py'), '--engine', 'strata', '--config', str(config),
             '--host', '127.0.0.1', '--port', '8080'])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Strata startup failed: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
