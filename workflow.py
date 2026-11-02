"""Interactive, resumable model-to-volume workflow for Vast."""
import fcntl
import functools
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import socket
import subprocess
import time
import uuid

import requests
from vastai.api import storage

import vast

MODELS = vast.ROOT / 'models'
STATE = MODELS / '.vast'
IMAGE = 'ghcr.io/sieciowiecxyz/vast-swiss-knife:latest'


def exclusive(function):
    @functools.wraps(function)
    def wrapped(name, *args, **kwargs):
        name_ok(name)
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(STATE / (name + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(f'Another operation for {name} is already running') from None
            return function(name, *args, **kwargs)
        finally:
            os.close(fd)
    return wrapped


def name_ok(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', name) or name in ('.', '..'):
        raise ValueError('Model name may contain only letters, digits, dots, underscores and hyphens')
    return name


def paths(name):
    name_ok(name)
    return STATE / (name + '.json'), STATE / (name + '.md5')


def save(path, data):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read(name):
    path, _ = paths(name)
    if not path.is_file():
        raise ValueError(f'No saved model {name}; run model-upload first')
    return json.loads(path.read_text())


def confirm(prompt, word='yes'):
    try:
        value = input(prompt + f' Type {word} to continue: ')
    except EOFError:
        raise ValueError('Confirmation requires an interactive terminal') from None
    if value.strip() != word:
        raise ValueError('Cancelled')


@exclusive
def upload(name, remote, source_path=None):
    state, manifest = paths(name)
    source = Path(source_path).expanduser() if source_path else MODELS / name
    if state.exists() or manifest.exists():
        raise ValueError('Model state already exists; do not overwrite an active deployment')
    if source.is_symlink() or not (source.is_dir() or source.is_file()):
        raise ValueError(f'Model path not found or is a symlink: {source}')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', remote):
        raise ValueError('Expected an rclone remote name without a colon')
    single_file = source.is_file()
    if single_file and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', source.name):
        raise ValueError('GGUF filename must contain only letters, digits, dots, underscores and hyphens')
    files = [source] if single_file else [p for p in source.rglob('*') if p.is_file()]
    if not single_file and (source / 'pack' / 'index.txt').is_file():
        model_format = 'strata-flash'
    elif len(files) == 1 and files[0].suffix.lower() == '.gguf':
        model_format = 'gguf'
    else:
        raise ValueError('Expected a Strata pack or one GGUF model file')
    if not files or (not single_file and any(p.is_symlink() for p in source.rglob('*'))):
        raise ValueError('Model must contain files and cannot contain symlinks')
    size = sum(p.stat().st_size for p in files)
    if size <= 0:
        raise ValueError('Model is empty')
    target = f'{remote}:models/{name}'
    print(f'Upload {len(files)} files, {size / 1e9:.3f} GB: {source} -> {target}', flush=True)
    confirm('Google Drive upload can take a long time.')
    if single_file:
        subprocess.run(['rclone', 'copyto', str(source), target + '/' + source.name, '--immutable', '--progress'], check=True)
        subprocess.run(['rclone', 'check', str(source.parent), target, '--one-way', '--include', '/' + source.name], check=True)
    else:
        subprocess.run(['rclone', 'copy', str(source), target, '--immutable', '--progress'], check=True)
        subprocess.run(['rclone', 'check', str(source), target], check=True)
    result = subprocess.run(['rclone', 'md5sum', target], check=True, capture_output=True, text=True)
    lines = result.stdout.splitlines()
    if len(lines) != len(files) or any(not re.fullmatch(r'[0-9a-fA-F]{32}  .+', line) for line in lines):
        raise ValueError('Google Drive did not return an MD5 for every file; model was not registered')
    for line in lines:
        rel = PurePosixPath(line[34:])
        if rel.is_absolute() or '..' in rel.parts or '\\' in str(rel):
            raise ValueError('Unsafe path in Google Drive checksum manifest')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(result.stdout)
    save(state, {'model': name, 'remote': remote, 'drive_path': f'/models/{name}',
                 'bytes': size, 'files': len(files), 'format': model_format, 'volume': None, 'copy': None})
    print('Google Drive upload and comparison complete.')
    if model_format == 'gguf': print('Note: this deployment currently supports only the Flash Next Strata pack, not standalone GGUF.')


@exclusive
def register(name, remote, folder):
    state, manifest = paths(name)
    if state.exists() or manifest.exists():
        raise ValueError('Model already registered')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', remote):
        raise ValueError('Expected an rclone remote name without a colon')
    path = PurePosixPath(folder)
    if path.is_absolute() or '..' in path.parts or not path.parts or any(c in folder for c in ('\\', '\n', '\r')):
        raise ValueError('Use a relative Google Drive folder path')
    target = f'{remote}:{path}'
    info = json.loads(subprocess.run(['rclone', 'size', target, '--json'], check=True, capture_output=True, text=True).stdout)
    checksums = subprocess.run(['rclone', 'md5sum', target], check=True, capture_output=True, text=True).stdout
    lines = checksums.splitlines()
    if info['bytes'] <= 0 or len(lines) != info['count'] or any(not re.fullmatch(r'[0-9a-fA-F]{32}  .+', line) for line in lines):
        raise ValueError('Drive did not return an MD5 for every file')
    for line in lines:
        rel = PurePosixPath(line[34:])
        if rel.is_absolute() or '..' in rel.parts or '\\' in str(rel):
            raise ValueError('Unsafe path in Google Drive checksum manifest')
    file_names = [line[34:] for line in lines]
    if 'pack/index.txt' in file_names:
        model_format = 'strata-flash'
    elif len(file_names) == 1 and file_names[0].lower().endswith('.gguf'):
        model_format = 'gguf'
    else:
        raise ValueError('Expected a Strata pack or one GGUF model file')
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(checksums)
    save(state, {'model': name, 'remote': remote, 'drive_path': '/' + str(path),
                 'bytes': info['bytes'], 'files': info['count'], 'format': model_format, 'volume': None, 'copy': None})
    print(f'Registered {name}: {info["count"]} files, {info["bytes"] / 1e9:.3f} GB on Google Drive.')
    if model_format == 'gguf': print('Note: this deployment currently supports only the Flash Next Strata pack, not standalone GGUF.')


def compatible(row):
    driver = str(row.get('driver_version') or '').split('.')[0]
    return (row.get('cpu_arch') in ('amd64', 'x86_64') and driver.isdigit() and int(driver) >= 580
            and (row.get('cpu_ram') or 0) >= 60000 and (row.get('gpu_ram') or 0) >= 11500
            and row.get('compute_cap') in (750, 800, 860, 890, 1200))


def offers(name, gpu, hours, disk, region, limit):
    model = read(name)
    names = vast.gpu_names()
    matches = [n for n in names if n.casefold().replace(' ', '_').replace('-', '_') == gpu.casefold().replace(' ', '_').replace('-', '_')]
    if len(matches) != 1:
        raise ValueError('GPU name not found or ambiguous; use --list-gpus')
    rows = vast.ranked_offers(vast.client(), matches[0], hours, model['bytes'] / 1e9, disk, region)
    rows = [row for row in rows if compatible(row)]
    print(f"Model {name}: {model['bytes'] / 1e9:.3f} GB; suggested volume: {math.ceil(model['bytes'] / 1e9 * 1.1)} GB (10% headroom).")
    vast.print_offers(rows[:limit])
    if not rows: print('No compatible offers found (x86-64, driver >= 580, 64 GB RAM, >= 12 GB VRAM and a supported GPU architecture required).')


@exclusive
def prepare(name, machine, size, connection):
    state, _ = paths(name)
    model = read(name)
    if size is None:
        size = math.ceil(model['bytes'] / 1e9 * 1.1)
    if model.get('format', 'strata-flash') != 'strata-flash':
        raise ValueError('This deployment supports the Flash Next Strata pack, not standalone GGUF; no volume was rented')
    if model.get('volume') or model.get('volume_pending'):
        raise ValueError('A volume already exists or its creation outcome is unknown; inspect model-status')
    if size * 1e9 < model['bytes']:
        raise ValueError('Volume size is smaller than the model')
    api = vast.client()
    if connection is None:
        connections = [c for c in storage.show_connections(api.client) if c.get('cloud_type') == 'drive']
        if len(connections) != 1 or not isinstance(connections[0].get('id'), int):
            raise ValueError('Expected one Google Drive connection; use connections and --connection ID')
        connection = connections[0]['id']
        print(f'Using Vast Google Drive connection {connection}.')
    offer = vast.volume_offer(api, machine, size)
    response = api.client.post('/bundles/', json_data={'machine_id': {'eq': machine},
        'rentable': {'eq': True}, 'type': 'bid', 'limit': 100, 'allocated_storage': 20})
    response.raise_for_status()
    data = response.json()
    rows = data.get('offers', [])
    if data.get('truncated') or len(rows) >= 100:
        raise ValueError('GPU offer results incomplete; cannot estimate upload')
    rates = [r.get('inet_down_cost') for r in rows if r.get('machine_id') == machine]
    if not rates or any(not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in rates):
        raise ValueError('No reliable inbound rate for this machine; cannot estimate upload')
    rate = max(rates)
    label = 'model_' + uuid.uuid4().hex[:12]
    print(f'Machine {machine}, volume {size} GB, ${offer["storage_total_cost"]:.5f}/h; model {model["bytes"] / 1e9:.3f} GB')
    print(f'Estimated upload: ${model["bytes"] / 1e9 * rate:.4f} at up to ${rate:.6f}/GB; retries may cost more. GPU is not rented.')
    confirm('Create this volume and start Cloud Copy?')
    model['volume_pending'] = {'machine': machine, 'label': label, 'offer': offer['id'], 'size_gb': size, 'connection': connection}
    save(state, model)
    try:
        result = vast.checked(storage.create_volume(api.client, offer['id'], size=size, name=label))
        volume = result.get('volume_id')
        if not isinstance(volume, int):
            raise ValueError('Missing volume ID')
    except requests.HTTPError as error:
        rejected = False
        if error.response is not None and error.response.status_code == 422:
            try:
                rejected = error.response.json().get('error') == 'invalid_args'
            except (ValueError, AttributeError):
                pass
        if rejected:
            model.pop('volume_pending', None)
            save(state, model)
            raise RuntimeError('Vast rejected volume creation (HTTP 422); no volume was created') from None
        raise RuntimeError(f'Volume creation outcome unknown; inspect volume-list for label {label} before retrying') from None
    except Exception:
        raise RuntimeError(f'Volume creation outcome unknown; inspect volume-list for label {label} before retrying') from None
    model['volume'] = {'id': volume, 'machine': machine, 'size_gb': size, 'label': label}
    model.pop('volume_pending')
    model['copy'] = {'state': 'pending', 'connection': connection}
    save(state, model)
    submit_copy(name, model, state, api, connection)


def submit_copy(name, model, state, api, connection):
    volume = model['volume']['id']
    model['copy'] = {'state': 'pending', 'connection': connection}
    save(state, model)
    print(f'Waiting for volume {volume} to become ready; storage is billable.', flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        rows = [v for v in storage.show_volumes(api.client) if v.get('id') == volume]
        if len(rows) == 1 and rows[0].get('status') == 'created':
            break
        time.sleep(5)
    else:
        raise RuntimeError(f'Volume {volume} is not ready; inspect model-status before retrying')
    for attempt in range(6):
        try:
            response = vast.checked(storage.copy(api.client, f'drive.{connection}', f'V.{volume}', model['drive_path'], '/qwen'))
            break
        except requests.HTTPError as error:
            try:
                not_ready = error.response is not None and error.response.status_code == 400 and error.response.json().get('error') == 'invalid_volume'
            except (ValueError, AttributeError):
                not_ready = False
            if not_ready and attempt < 5:
                time.sleep(5)
                continue
            raise RuntimeError(f'Cloud Copy was rejected or its outcome is unknown; volume {volume} exists. Inspect Vast before retrying.') from None
        except Exception:
            raise RuntimeError(f'Cloud Copy outcome unknown; volume {volume} exists. Inspect Vast before retrying.') from None
    model['copy'] = {'state': 'submitted', 'connection': connection, 'id': response.get('copy_id'),
                     'log_url': response.get('result_url')}
    save(state, model)
    print(f'Volume {volume} created. Cloud Copy submitted; completion is not yet verified.')
    print(f'Check: python vast.py model-status {name}')


@exclusive
def reset_volume(name):
    state, _ = paths(name)
    model = read(name)
    item = model.get('volume') or model.get('volume_pending')
    if not item:
        raise ValueError('There is no saved volume')
    api = vast.client()
    rows = storage.show_volumes(api.client)
    if any((item.get('id') is not None and row.get('id') == item['id']) or
           (item.get('label') and row.get('label') == item['label']) for row in rows):
        raise ValueError('The saved volume still exists; delete it explicitly before resetting')
    print('No matching volume is visible; a delayed creation could still appear.')
    confirm('Forget the saved volume state?', 'RESET')
    model['volume'] = None
    model['copy'] = None
    model.pop('volume_pending', None)
    save(state, model)
    print('Volume state cleared. The model remains registered on Google Drive.')


@exclusive
def recover_volume(name, volume_id):
    state, _ = paths(name)
    model = read(name)
    pending = model.get('volume_pending')
    if not pending or model.get('volume'):
        raise ValueError('There is no unknown volume creation to reconcile')
    api = vast.client()
    rows = [v for v in storage.show_volumes(api.client) if v.get('id') == volume_id]
    if (len(rows) != 1 or rows[0].get('machine_id') != pending['machine'] or
            rows[0].get('label') != pending['label'] or rows[0].get('instances')):
        raise ValueError('Volume does not match the saved machine and label, or is attached')
    print(f"Adopt volume {volume_id} on machine {pending['machine']} with label {pending['label']}.")
    confirm('Adopt this existing volume?', 'ADOPT')
    model['volume'] = {'id': volume_id, 'machine': pending['machine'],
                       'size_gb': pending['size_gb'], 'label': pending['label']}
    model['copy'] = {'state': 'pending', 'connection': pending['connection']}
    model.pop('volume_pending')
    save(state, model)
    print(f'Volume adopted. Use model-copy {name} after checking whether a transfer was started.')


@exclusive
def recover_rent(name, instance_id=None):
    receipt = STATE / (name_ok(name) + '.rent.json')
    if not receipt.exists():
        raise ValueError('No rental receipt')
    record = json.loads(receipt.read_text())
    if record.get('outcome') == 'created':
        raise ValueError('Rental was created; resume it or destroy it normally')
    label = record.get('label')
    if not label:
        raise ValueError('Rental label is missing; inspect Vast first')
    rows = [r for r in vast.instances(vast.client()) if r.get('label') == label]
    if instance_id is not None:
        model = read(name)
        if (len(rows) != 1 or rows[0].get('id') != instance_id or
                not any(v.get('id') == (model.get('volume') or {}).get('id')
                        for v in (rows[0].get('volume_info') or []) if isinstance(v, dict))):
            raise ValueError('Instance does not match the saved label and volume')
        print(f'Instance {instance_id} matches the saved label and volume and may be billable.')
        confirm('Adopt this existing instance?', 'ADOPT')
        record.update(outcome='created', response={'new_contract': instance_id})
        save(receipt, record)
        print('Rental adopted; run model-launch to resume.')
        return
    if rows:
        raise ValueError('Matching instance exists; inspect Vast or adopt it with --instance')
    print(f'No instance with label {label} was found. A delayed creation may still appear.')
    confirm('Check Vast carefully before allowing another rental.', 'RESET')
    os.replace(receipt, STATE / (name + '.rent.' + uuid.uuid4().hex + '.archive'))
    print('Receipt archived; the next launch will ask before renting.')


@exclusive
def retry_copy(name):
    state, _ = paths(name)
    model = read(name)
    copy = model.get('copy') or {}
    if not model.get('volume') or copy.get('state') != 'pending' or not copy.get('connection'):
        raise ValueError('No failed or uncertain transfer to retry')
    print(f"The previous transfer outcome is unknown for volume {model['volume']['id']}. A duplicate copy can incur charges.")
    confirm('Check Vast first and confirm that retrying is safe.', 'RETRY')
    submit_copy(name, model, state, vast.client(), copy['connection'])


def status(name):
    m = read(name)
    v = m.get('volume') or {}
    c = m.get('copy') or {}
    vast.table(['MODEL', 'GB', 'DRIVE', 'VOLUME', 'MACHINE', 'COPY'], [[
        name, f"{m['bytes'] / 1e9:.3f}", f"{m['remote']}:{m['drive_path']}",
        v.get('id', '-'), v.get('machine', '-'), c.get('state', 'not submitted')]])
    if m.get('volume_pending'):
        print('Volume creation outcome unknown; check volume-list for label', m['volume_pending']['label'])
    if c.get('log_url'):
        response = requests.get(c['log_url'], timeout=20)
        response.raise_for_status()
        print('Transfer log (inventory alone does not establish completion):')
        print('\n'.join(response.text.splitlines()[-8:]))
    receipt = STATE / (name + '.rent.json')
    if receipt.exists():
        r = json.loads(receipt.read_text())
        print('Rental:', r.get('outcome'), 'label:', r.get('label'), 'instance:', r.get('response', {}).get('new_contract', '-'))


@exclusive
def launch(name, offer_id, bid, key, local_port, timeout):
    state, manifest = paths(name)
    model = read(name)
    volume = model.get('volume')
    if not volume or (model.get('copy') or {}).get('state') != 'submitted':
        raise ValueError('No volume with submitted Cloud Copy; run model-prepare first')
    if not manifest.is_file():
        raise ValueError('Missing checksum manifest')
    if not 1 <= local_port <= 65535:
        raise ValueError('Invalid local port')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', local_port))
    api = vast.client()
    receipt = STATE / (name + '.rent.json')
    identifier = None
    if receipt.exists():
        record = json.loads(receipt.read_text())
        identifier = record.get('response', {}).get('new_contract')
        if record.get('outcome') != 'created' or not identifier:
            raise ValueError('Previous rental outcome is unknown; inspect model-status and Vast before retrying')
        if any(row.get('id') == identifier for row in vast.instances(api)):
            print(f'Resuming instance {identifier}; it remains billable.')
        else:
            archive = STATE / (name + '.rent.' + uuid.uuid4().hex + '.archive')
            os.replace(receipt, archive)
            identifier = None
    if identifier is None:
        row = vast.find_machine_offer(api, volume['machine'], offer_id, 'bid', 20)
        if not compatible(row):
            raise ValueError('Offer lacks required CPU architecture, driver, RAM or GPU memory for Strata')
        print('Cloud Copy completion has not been verified. GPU and storage are billable during verification.')
        confirm('Proceed to paid GPU verification even though copy completion is unknown?', 'READY')
        print(f"Offer {offer_id}, machine {volume['machine']}, {row.get('num_gpus')} x {row.get('gpu_name')}; bid ${bid:.4f}/h plus disk and network; volume {volume['id']}.")
        confirm('Rent this interruptible instance?')
        result = vast.rent(api, offer_id, os.environ.get('VAST_IMAGE', IMAGE), bid=bid, receipt=receipt,
                           volume=volume['id'], strata=True)
        identifier = result['response']['new_contract']
        print(f'Instance {identifier} created and billable; receipt: {receipt}', flush=True)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                row = vast.instance(api, identifier)
                if row.get('intended_status') == 'stopped':
                    raise RuntimeError(f'Instance {identifier} stopped; inspect it before resuming')
                argv = vast.ssh_command(row, key)
                probe = subprocess.run(argv + ['true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                if probe.returncode == 0:
                    break
            except (LookupError, ValueError, subprocess.TimeoutExpired):
                pass
            time.sleep(5)
        else:
            raise TimeoutError('SSH did not become ready')
        print('Checking every model file against Google Drive MD5; this can take several minutes.', flush=True)
        seconds = max(1, int(deadline - time.monotonic()))
        check = subprocess.run(argv + [f'cd /models/qwen && timeout -k 5 {seconds} md5sum -c --quiet'],
                               input=manifest.read_bytes(), timeout=seconds + 10)
        if check.returncode:
            raise RuntimeError('Model checksum verification failed or timed out')
        print('All model checksums match. Waiting for the API on the instance.', flush=True)
        while time.monotonic() < deadline:
            probe = subprocess.run(argv + ['curl -fsS --max-time 3 http://127.0.0.1:8080/health'],
                                   capture_output=True, text=True, timeout=10)
            if probe.returncode == 0:
                try:
                    if json.loads(probe.stdout).get('loaded') is True:
                        break
                except (ValueError, AttributeError):
                    pass
            time.sleep(5)
        else:
            raise TimeoutError('API did not become ready')
        tunnel = argv[:-1] + ['-N', '-o', 'ExitOnForwardFailure=yes', '-L',
                              f'127.0.0.1:{local_port}:127.0.0.1:8080', argv[-1]]
        print(f'Opening SSH tunnel: http://127.0.0.1:{local_port}/v1. Ctrl+C closes the tunnel; the instance remains billable.', flush=True)
        try:
            subprocess.run(tunnel, check=True)
        except KeyboardInterrupt:
            print(f'\nTunnel closed. Instance {identifier} remains running; destroy it with: python vast.py destroy {identifier}')
    except KeyboardInterrupt:
        print(f'\nInterrupted. Instance {identifier} may still be billable; use python vast.py destroy {identifier} when done.')
    except Exception as e:
        raise RuntimeError(f'{e}. Instance {identifier} may still be billable; inspect it and use python vast.py destroy {identifier} when done.') from e
