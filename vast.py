"""Minimal Vast.ai SDK helpers and CLI; no simulation-specific dependencies."""
import argparse
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

import requests
from vastai import VastAI
from vastai.api import storage

ROOT = Path(__file__).resolve().parent
EU = set('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())
REGIONS = {
    'eu': EU,
    'europe': EU | set('AL AD BA BY CH GB GI GG IM IS JE LI MC MD ME MK NO RS RU SM TR UA VA XK'.split()),
    'north-america': set('AG AI AW BB BL BM BQ BS BZ CA CR CU CW DM DO GD GL GP GT HN HT JM KN KY LC MF MQ MS MX NI PA PM PR SV SX TC TT US VC VG VI'.split()),
    'south-america': set('AR BO BR CL CO EC FK GF GY PE PY SR UY VE'.split()),
    'asia': set('AE AF AM AZ BD BH BN BT CN GE HK ID IL IN IO IQ IR JO JP KG KH KP KR KW KZ LA LB LK MM MN MO MV MY NP OM PH PK PS QA SA SG SY TH TJ TL TM TR TW UZ VN YE'.split()),
    'africa': set('AO BF BI BJ BW CD CF CG CI CM CV DJ DZ EG EH ER ET GA GH GM GN GQ GW KE KM LR LS LY MA MG ML MR MU MW MZ NA NE NG RE RW SC SD SH SL SN SO SS ST SZ TD TG TN TZ UG YT ZA ZM ZW'.split()),
    'oceania': set('AS AU CK FJ FM GU KI MH MP NC NF NR NU NZ PF PG PN PW SB TK TO TV UM VU WF WS'.split()),
    'all': None,
}


def client():
    key = os.environ.get('VAST_API_KEY', '').strip()
    if not key and (ROOT / '.env').exists():
        for line in (ROOT / '.env').read_text().splitlines():
            name, sep, value = line.partition('=')
            if sep and name.strip() == 'VAST_API_KEY':
                values = shlex.split(value, comments=True)
                key = values[0] if values else ''
    if not key:
        raise ValueError('Set VAST_API_KEY in the environment or .env')
    # In SDK1.9.1 retry=1 means ONE total attempt. Never retry paid creation.
    vast = VastAI(api_key=key, retry=1, raw=True, quiet=True)
    vast.client.timeout = 25
    return vast


def instances(vast):
    """Raw paginated rows also work while a machine is still booting."""
    rows, seen, params = [], set(), {'limit': 100}
    while True:
        page = vast.show_instances_v1(params)
        rows.extend(page.get('instances') or [])
        cursor = page.get('next_token')
        if not cursor:
            return rows
        if cursor in seen:
            raise RuntimeError('Repeated inventory cursor')
        seen.add(cursor)
        params = {'limit': 100, 'after_token': cursor}


def instance(vast, identifier):
    row = next((r for r in instances(vast) if r['id'] == identifier), None)
    if row is None:
        raise LookupError(f'Instance {identifier} not found in your account')
    return row


def checked(value):
    if isinstance(value, dict) and value.get('success') is False:
        raise RuntimeError('Vast rejected the operation')
    return value


def safe(value):
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items()
                if k.lower() not in ('env', 'extra_env', 'onstart', 'onstart_cmd', 'result_url', 'image_login')
                and not any(s in k.lower() for s in ('key', 'token', 'secret', 'password'))}
    if isinstance(value, list):
        return [safe(v) for v in value]
    return value


def find_machine_offer(vast, machine, offer, bid_type, disk):
    # Vast can return a rotating subset of valid asks for the same machine.
    for _ in range(3):
        response = vast.client.post('/bundles/', json_data={'machine_id': {'eq': machine},
            'rentable': {'eq': True}, 'type': bid_type, 'limit': 100, 'allocated_storage': disk})
        response.raise_for_status()
        data = response.json()
        rows = data.get('offers', [])
        if data.get('truncated') or len(rows) >= 100:
            raise ValueError('Offer results incomplete')
        matching = [row for row in rows if row.get('id') == offer and row.get('machine_id') == machine]
        if len(matching) == 1:
            return matching[0]
    raise ValueError('GPU offer is unavailable or on a different machine than the volume')


def rent(vast, offer, image, disk=20, bid=None, label=None, receipt=None, volume=None, mount='/models', strata=False):
    if disk <= 0 or not math.isfinite(disk) or (bid is not None and (bid <= 0 or not math.isfinite(bid))):
        raise ValueError('Disk and bid must be positive finite values')
    identifier = uuid.uuid4().hex[:12]
    label = label or 'vast-' + identifier
    path = Path(receipt) if receipt else ROOT / f'rent-{identifier}.json'
    body = dict(id=offer, image=image, disk=disk, label=label, ssh=True,
                direct=True, cancel_unavail=True)
    if bid is not None:
        body['bid_price'] = bid
    if strata:
        body['onstart_cmd'] = ('chmod go-w /root; chmod 700 /root/.ssh; '
                               'chown root:root /root/.ssh/authorized_keys; '
                               'chmod 600 /root/.ssh/authorized_keys; '
                               'exec /opt/instance-tools/bin/entrypoint.sh')
    if volume is not None:
        if not mount.startswith('/') or mount == '/':
            raise ValueError('Volume mount must be an absolute path below /')
        rows = storage.show_volumes(vast.client)
        selected = next((row for row in rows if row.get('id') == volume), None)
        if selected is None:
            raise LookupError(f'Volume {volume} not found')
        if selected.get('instances'):
            raise ValueError(f'Volume {volume} is already attached')
        if not isinstance(selected.get('machine_id'), int):
            raise ValueError('Volume has no machine ID; cannot safely match GPU offer')
        find_machine_offer(vast, selected['machine_id'], offer,
                           'bid' if bid is not None else 'on-demand', disk)
        body.update(link_volume=volume, mount_path=mount)
    # Persist intent before the paid call. On timeout, find this label with
    # `list`; repeating create_instance could produce a second paid instance.
    record = dict(outcome='pending', label=label, request=body)
    with path.open('x') as stream:
        json.dump(record, stream, indent=2)
    try:
        result = checked(vast.create_instance(**body))
        if not isinstance(result, dict) or not result.get('new_contract'):
            raise RuntimeError('Creation response has no instance ID')
    except requests.HTTPError as error:
        rejected = False
        if error.response is not None and error.response.status_code == 410:
            try:
                rejected = error.response.json().get('error') == 'no_such_ask'
            except (ValueError, AttributeError):
                pass
        record['outcome'] = 'rejected' if rejected else 'unknown'
        path.write_text(json.dumps(record, indent=2) + '\n')
        if rejected:
            raise RuntimeError(f'The offer is no longer available; no instance was created. Receipt: {path}') from None
        raise RuntimeError(f'Rental outcome unknown; inspect {path} and list by label before retrying') from None
    except Exception:
        record['outcome'] = 'unknown'
        path.write_text(json.dumps(record, indent=2) + '\n')
        raise RuntimeError(f'Rental outcome unknown; inspect {path} and list by label before retrying') from None
    record.update(outcome='created', response=safe(result))
    path.write_text(json.dumps(record, indent=2) + '\n')
    return record


def ssh_command(row, key=None):
    host, port = row.get('ssh_host'), row.get('ssh_port')
    if not host or not port:
        mappings = (row.get('ports') or {}).get('22/tcp') or []
        host = row.get('public_ipaddr')
        port = mappings[0].get('HostPort') if mappings else None
    if not host or not port:
        raise ValueError('SSH address is not available yet')
    if (not isinstance(host, str) or host.startswith('-')
            or any(not (c.isascii() and (c.isalnum() or c in '.:-[]')) for c in host)
            or not 1 <= int(port) <= 65535):
        raise ValueError('Invalid SSH address')
    argv = ['ssh', '-p', str(int(port)), '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new']
    if key:
        argv += ['-i', str(Path(key).expanduser())]
    return argv + ['root@' + host]


def provision(vast, identifier, key=None, script=None, timeout=420):
    """Wait for SSH, then run a basic or user-supplied Bash script once."""
    content = Path(script).expanduser().read_bytes() if script else (
        b'set -eu\nmkdir -p /workspace\n'
        b'if command -v nvidia-smi >/dev/null; then nvidia-smi; fi\n')
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        remaining = end - time.monotonic()
        vast.client.timeout = min(25, max(1, remaining))
        try:
            argv = ssh_command(instance(vast, identifier), key)
        except LookupError:
            pass
        except ValueError as error:
            if 'not available yet' not in str(error):
                raise
        else:
            try:
                probe = subprocess.run(argv + ['true'], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=min(8, max(.1, end - time.monotonic())))
            except subprocess.TimeoutExpired:
                probe = None
            if probe is not None and probe.returncode == 0 and time.monotonic() < end:
                seconds = max(1, int(end - time.monotonic()))
                try:
                    subprocess.run(argv + [f'timeout -k 5 {seconds} bash -se'],
                        input=content, check=True, timeout=seconds + 10)
                    return {'id': identifier, 'provisioned': True}
                except subprocess.TimeoutExpired:
                    raise RuntimeError('Provisioning timed out; instance remains allocated') from None
        time.sleep(min(5, max(0, end - time.monotonic())))
    raise TimeoutError('SSH did not become ready; instance remains allocated')


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('Expected a positive finite number')
    return value


def gpu_names():
    response = requests.get('https://console.vast.ai/api/v0/gpu_names/unique/', timeout=25)
    response.raise_for_status()
    return sorted(response.json()['gpu_names'])


def ranked_offers(vast, gpu, hours, upload_gb, disk, region='eu'):
    """Rank interruptible offers by estimated rental and inbound transfer cost."""
    response = vast.client.post('/bundles/', json_data={
        'rentable': {'eq': True}, 'num_gpus': {'eq': 1},
        'gpu_name': {'eq': gpu}, 'type': 'bid',
        'allocated_storage': disk, 'limit': 500, 'order': [['dph_total', 'asc']],
    })
    response.raise_for_status()
    data = response.json()
    rows = data['offers']
    if data.get('truncated') or len(rows) >= 500:
        raise RuntimeError('Offer response may be incomplete; refusing to rank')
    ranked = []
    for row in rows:
        country = (row.get('geolocation') or '').split(',')[-1].strip().upper()
        if (REGIONS[region] is not None and country not in REGIONS[region]) or row.get('gpu_name') != gpu or row.get('num_gpus') != 1:
            continue
        hourly, rate = row.get('dph_total'), row.get('inet_down_cost')
        if not isinstance(hourly, (int, float)) or not math.isfinite(hourly) or hourly < 0:
            continue
        if upload_gb and (not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0):
            continue
        if row.get('duration') is not None and row['duration'] < hours * 3600:
            continue
        transfer = upload_gb * rate if upload_gb else 0
        speed = row.get('inet_down')
        ranked.append({**{k: row.get(k) for k in ('id', 'host_id', 'machine_id', 'geolocation', 'verification', 'cpu_arch', 'cpu_ram', 'gpu_ram', 'driver_version', 'compute_cap',
            'reliability2', 'min_bid', 'dph_total', 'storage_total_cost', 'inet_down', 'inet_up', 'inet_down_cost')},
            'rental_cost_usd': hourly * hours, 'upload_cost_usd': transfer,
            'estimated_total_usd': hourly * hours + transfer,
            'ideal_upload_minutes': upload_gb * 8000 / speed / 60 if speed and speed > 0 else None})
    return sorted(ranked, key=lambda row: (row['estimated_total_usd'], row['id']))


def table(headers, rows, colors=None):
    rows = [[str(value) for value in row] for row in rows]
    widths = [max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
              for i, header in enumerate(headers)]
    def line(row):
        return '  '.join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip()
    print(line(headers))
    print(line(['-' * width for width in widths]))
    for i, row in enumerate(rows):
        if colors and sys.stdout.isatty() and 'NO_COLOR' not in os.environ:
            cells = []
            for j, value in enumerate(row):
                color = colors[i][j]
                cells.append((f'\033[{color}m{value}\033[0m' if color else value)
                             + ' ' * (widths[j] - len(value)))
            rendered = '  '.join(cells).rstrip()
        else:
            rendered = line(row)
        print(rendered)
    if not rows:
        print('No results.')


def print_result(value):
    rows = []
    def collect(prefix, item):
        if isinstance(item, dict) and item:
            for key, child in item.items():
                collect(f'{prefix}.{key}' if prefix else str(key), child)
        elif isinstance(item, list) and item:
            for index, child in enumerate(item):
                collect(f'{prefix}[{index}]', child)
        else:
            rows.append([prefix or 'result', '-' if item is None else str(item)])
    collect('', safe(value))
    table(['FIELD', 'VALUE'], rows)


def print_offers(rows):
    def number(value, decimals=2):
        return f'{value:.{decimals}f}' if value is not None else '-'
    metrics = {5: ('reliability2', True), 6: ('dph_total', False),
               7: ('rental_cost_usd', False), 8: ('upload_cost_usd', False),
               9: ('estimated_total_usd', False), 10: ('inet_down', True),
               11: ('inet_up', True), 12: ('ideal_upload_minutes', False), 13: ('min_bid', False)}
    colors = [[None] * 14 for _ in rows]
    for column, (key, higher_is_better) in metrics.items():
        values = sorted({row[key] for row in rows if row[key] is not None}, reverse=higher_is_better)
        for index, row in enumerate(rows):
            value = row[key]
            if value is not None:
                position = values.index(value) / (len(values) - 1) if len(values) > 1 else 0
                colors[index][column] = '32' if position < 1 / 3 else '33' if position < 2 / 3 else '31'
    table(['OFFER', 'HOST ID', 'MACHINE ID', 'LOCATION', 'VERIFIED', 'RELIABILITY', '$/H', 'RENT $',
           'UPLOAD $', 'TOTAL $', 'DOWN Mb/s', 'UP Mb/s', 'MIN UPLOAD min', 'MIN BID $/H'], [
        [row['id'], row['host_id'] or '-', row['machine_id'] or '-', row['geolocation'] or '-', row['verification'] or '-',
         number(row['reliability2'], 3), number(row['dph_total'], 4),
         number(row['rental_cost_usd'], 4), number(row['upload_cost_usd'], 4),
         number(row['estimated_total_usd'], 4), number(row['inet_down'], 0), number(row['inet_up'], 0),
         number(row['ideal_upload_minutes'], 1), number(row.get('min_bid'), 4)] for row in rows], colors)


def volume_offer(vast, machine, size):
    offers = vast.search_volumes(query=f'machine_id={machine} disk_space>={size}',
                                 storage=size, limit=100, no_default=True)
    if len(offers) >= 100:
        raise RuntimeError('Volume offer list may be incomplete; cannot select a price')
    offers = [row for row in offers if row.get('machine_id') == machine
              and isinstance(row.get('disk_space'), (int, float)) and row['disk_space'] >= size
              and isinstance(row.get('storage_total_cost'), (int, float))
              and math.isfinite(row['storage_total_cost']) and row['storage_total_cost'] >= 0
              and isinstance(row.get('id'), int)]
    if not offers:
        raise LookupError(f'No priced volume offer with {size} GB on machine {machine}')
    return min(offers, key=lambda row: (row['storage_total_cost'], row['id']))


def upload_file(vast, instance_id, source, destination, key=None):
    path = Path(source).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('Source must be a regular file')
    if not destination.startswith('/') or destination == '/' or any(c in destination for c in '\n\r\x00'):
        raise ValueError('Destination must be an absolute file path below /')
    row = instance(vast, instance_id)
    rate = row.get('inet_down_cost')
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
        raise ValueError('Instance has no available inbound transfer price; cannot estimate safely')
    size = path.stat().st_size
    command = ssh_command(row, key)
    print(f'File: {path}\nSize: {size:,} bytes ({size / 1024**3:.3f} GiB)')
    print(f'Inbound rate: ${rate:.8f}/GiB; estimated transfer: ${size / 1024**3 * rate:.4f}')
    print('GPU and storage remain billable during upload; retries and outbound traffic can add cost.')
    print(f'Target: instance {instance_id}:{destination}')
    if not sys.stdin.isatty():
        raise ValueError('Confirmation requires an interactive terminal')
    if input('Upload file? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Cancelled.')
        return
    subprocess.run(['rsync', '-a', '--info=progress2', '--protect-args', '-e',
                    shlex.join(command[:-1]), '--', str(path), command[-1] + ':' + destination], check=True)


def main():
    parser = argparse.ArgumentParser(
        description='Vast Swiss Knife: models, GPU offers, volumes, and SSH.\n\nStart here: models → add / import-model → find → stage → stage-status → launch',
        epilog='Quick start: models → add / import-model → find → stage → stage-status → launch\nUse COMMAND --help for examples and options.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--gpu', help='GPU name from --list-gpus')
    group.add_argument('--list-gpus', action='store_true', help='List GPU names from Vast')
    parser.add_argument('--iloscgb', '--ilośćgb', type=positive, default=0, dest='upload_gb', help='Inbound transfer size in GB')
    parser.add_argument('--hours', type=positive, default=1, help='Rental duration in hours (default: 1)')
    parser.add_argument('--disk', type=positive, default=20, help='Instance disk size in GB (default: 20)')
    parser.add_argument('--limit', type=int, default=10, help='Maximum offers to display (default: 10)')
    parser.add_argument('--region', choices=REGIONS, default='eu',
        help='Region: eu (EU27, default), europe (Europe including UK), north-america, south-america, asia, africa, oceania, all (worldwide, including unknown locations)')
    commands = parser.add_subparsers(dest='command', metavar='COMMAND')
    p = commands.add_parser('search'); p.add_argument('query')
    p.add_argument('--type', choices=('on-demand', 'bid'), default='on-demand')
    p.add_argument('--limit', type=int, default=10); p.add_argument('--disk', type=positive, default=20)
    p = commands.add_parser('list'); p.add_argument('--label')
    p = commands.add_parser('volume-search', help='Find volume offers on a machine')
    p.add_argument('machine', type=int); p.add_argument('--size', type=positive, default=20)
    p = commands.add_parser('volume-create', help='Rent a volume on a machine')
    p.add_argument('machine', type=int, help='Machine ID from the GPU offer table'); p.add_argument('--size', type=int, required=True)
    p.add_argument('--name', required=True)
    commands.add_parser('disks', aliases=['volume-list'], help='List volumes')
    p = commands.add_parser('rm-disk', aliases=['volume-delete'], help='Delete a volume and its data')
    p.add_argument('id', type=int)
    p = commands.add_parser('volume-copy', help='Copy from connected cloud storage to a volume')
    p.add_argument('source', help='Cloud source, e.g. s3.123:/bucket/path')
    p.add_argument('id', type=int); p.add_argument('destination', help='Absolute destination path on volume')
    p = commands.add_parser('upload', help='Upload a local file to an existing SSH instance')
    p.add_argument('id', type=int, help='Instance ID')
    p.add_argument('file', type=Path); p.add_argument('destination', help='Absolute file path on the instance')
    p.add_argument('--key', help='SSH private key')
    for action in ('status', 'start', 'stop', 'destroy'):
        p = commands.add_parser(action); p.add_argument('id', type=int)
    p = commands.add_parser('rent'); p.add_argument('offer', type=int)
    p.add_argument('--image', required=True); p.add_argument('--disk', type=positive, default=20)
    p.add_argument('--bid', type=positive); p.add_argument('--label'); p.add_argument('--receipt', type=Path)
    p.add_argument('--strata', action='store_true', help='Start the Strata Vast image in SSH launch mode')
    p.add_argument('--volume', type=int, help='Existing volume ID on the GPU host')
    p.add_argument('--mount', default='/models', help='Volume mount path (default: /models)')
    p = commands.add_parser('key'); p.add_argument('public_key', type=Path)
    p.add_argument('--instance', type=int)
    p = commands.add_parser('ssh'); p.add_argument('id', type=int); p.add_argument('--key')
    p = commands.add_parser('provision'); p.add_argument('id', type=int)
    p.add_argument('--key'); p.add_argument('--script', type=Path)
    p.add_argument('--timeout', type=positive, default=420)
    p = commands.add_parser('add', aliases=['model-upload'], help='Upload models/NAME to Google Drive and verify it')
    p.add_argument('name'); p.add_argument('--remote', default='gdrive_vast', help='rclone remote name')
    p.add_argument('--source', type=Path, help='Local model directory or GGUF file (default: models/NAME)')
    commands.add_parser('models', aliases=['model-list'], help='List local and registered models')
    commands.add_parser('connections', help='List connected cloud accounts')
    p = commands.add_parser('import-model', aliases=['model-register'], help='Register an existing model on Google Drive')
    p.add_argument('name'); p.add_argument('--remote', default='gdrive_vast'); p.add_argument('--folder', required=True)
    p = commands.add_parser('find', aliases=['model-offers'], help='Rank interruptible offers for a registered model')
    p.add_argument('name'); p.add_argument('--gpu', required=True); p.add_argument('--hours', type=positive, default=2)
    p.add_argument('--disk', type=positive, default=20); p.add_argument('--region', choices=REGIONS, default='eu')
    p.add_argument('--limit', type=int, default=10)
    p = commands.add_parser('stage', aliases=['model-prepare'], help='Confirm host and prepare a standalone model volume')
    p.add_argument('name'); p.add_argument('machine', type=int); p.add_argument('--size', type=int, help='Volume GB (default: model size + 10%%)')
    p.add_argument('--connection', type=int, help='Vast Google Drive connection ID (auto-select if there is one)')
    p = commands.add_parser('reset-volume', help='Clear state after a volume was deleted')
    p.add_argument('name')
    p = commands.add_parser('model-recover-volume', help='Adopt a matching volume after a creation timeout')
    p.add_argument('name'); p.add_argument('id', type=int)
    p = commands.add_parser('model-recover-rent', help='Reconcile an unknown rental after checking Vast')
    p.add_argument('name'); p.add_argument('--instance', type=int, help='Adopt a matching instance ID')
    p = commands.add_parser('model-copy', help='Retry an uncertain transfer after checking Vast')
    p.add_argument('name')
    p = commands.add_parser('stage-status', aliases=['model-status'], help='Show model, volume and transfer status')
    p.add_argument('name')
    p = commands.add_parser('launch', aliases=['model-launch'], help='Rent, verify model, start API and open an SSH tunnel')
    p.add_argument('name'); p.add_argument('offer', type=int); p.add_argument('--bid', type=positive, required=True)
    p.add_argument('--key'); p.add_argument('--port', type=int, default=8080); p.add_argument('--timeout', type=positive, default=1800)
    args = parser.parse_args()
    args.command = {'disks':'volume-list', 'rm-disk':'volume-delete', 'add':'model-upload',
                    'models':'model-list', 'import-model':'model-register', 'find':'model-offers',
                    'stage':'model-prepare', 'stage-status':'model-status', 'launch':'model-launch'}.get(args.command, args.command)
    try:
        if args.command == 'connections':
            rows = storage.show_connections(client().client)
            table(['ID', 'NAME', 'TYPE'], [[r.get('id'), r.get('name') or '-', r.get('cloud_type') or '-'] for r in rows])
            return
        if args.command and (args.command.startswith('model-') or args.command == 'reset-volume'):
            import workflow
            if args.command == 'model-upload': workflow.upload(args.name, args.remote, args.source)
            elif args.command == 'model-list':
                names = {p.stem for p in workflow.STATE.glob('*.json') if not p.name.endswith('.rent.json')}
                if workflow.MODELS.is_dir():
                    names.update(p.name for p in workflow.MODELS.iterdir() if p.is_dir() and not p.name.startswith('.'))
                rows = []
                for name in sorted(names):
                    state = workflow.STATE / (name + '.json')
                    rows.append([name, 'yes' if (workflow.MODELS / name).is_dir() else '-',
                                 'registered' if state.is_file() else 'not uploaded'])
                table(['MODEL', 'LOCAL', 'DRIVE'], rows)
            elif args.command == 'model-register': workflow.register(args.name, args.remote, args.folder)
            elif args.command == 'model-offers':
                if args.limit < 1: raise ValueError('Limit must be positive')
                workflow.offers(args.name, args.gpu, args.hours, args.disk, args.region, args.limit)
            elif args.command == 'model-prepare':
                if (args.size is not None and args.size <= 0) or (args.connection is not None and args.connection <= 0) or args.machine <= 0: raise ValueError('IDs and size must be positive')
                workflow.prepare(args.name, args.machine, args.size, args.connection)
            elif args.command == 'reset-volume': workflow.reset_volume(args.name)
            elif args.command == 'model-recover-volume': workflow.recover_volume(args.name, args.id)
            elif args.command == 'model-recover-rent': workflow.recover_rent(args.name, args.instance)
            elif args.command == 'model-copy': workflow.retry_copy(args.name)
            elif args.command == 'model-status': workflow.status(args.name)
            elif args.command == 'model-launch': workflow.launch(args.name, args.offer, args.bid, args.key, args.port, args.timeout)
            return
        if args.command and (args.gpu or args.list_gpus):
            parser.error('Use --gpu or --list-gpus without a command')
        if args.list_gpus:
            table(['GPU'], [[name] for name in gpu_names()]); return
        if not args.command and not args.gpu:
            parser.error('Specify --gpu, --list-gpus or a command')
        vast = client()
        if args.gpu:
            if args.limit < 1: raise ValueError('Limit must be positive')
            names = gpu_names()
            matches = [name for name in names if name.casefold().replace(' ', '_').replace('-', '_') == args.gpu.casefold().replace(' ', '_').replace('-', '_')]
            if len(matches) != 1: raise ValueError('GPU name not found or ambiguous; see --list-gpus')
            result = ranked_offers(vast, matches[0], args.hours, args.upload_gb, args.disk, args.region)[:args.limit]
            print_offers(result); return
        elif args.command == 'search':
            result = vast.search_offers(query=args.query, type=args.type,
                                       order='dph_total', limit=args.limit, storage=args.disk)
        elif args.command == 'list':
            result = instances(vast)
            if args.label: result = [r for r in result if r.get('label') == args.label]
        elif args.command == 'volume-search':
            result = vast.search_volumes(query=f'machine_id={args.machine} disk_space>={args.size}', storage=args.size, limit=100, no_default=True)
            table(['OFFER', 'MACHINE', 'LOCATION', 'AVAILABLE GB', 'DISK $/H'],
                  [[r.get('id'), r.get('machine_id'), r.get('geolocation'),
                    r.get('disk_space'), f"{r.get('storage_total_cost', 0):.5f}"] for r in result])
            return
        elif args.command == 'volume-create':
            if args.size <= 0: raise ValueError('Volume size must be positive')
            offer = volume_offer(vast, args.machine, args.size)
            try:
                result = checked(storage.create_volume(vast.client, offer['id'], size=args.size, name=args.name))
            except Exception:
                raise RuntimeError(f'Volume creation outcome unknown; check volume-list for label {args.name} before retrying') from None
            result = {'machine_id': args.machine, 'volume_offer_id': offer['id'],
                      'storage_usd_per_hour': offer['storage_total_cost'], 'response': result}
        elif args.command == 'volume-list':
            rows = storage.show_volumes(vast.client)
            table(['VOLUME ID', 'NAME', 'MACHINE ID', 'LOCATION', 'STATUS', 'SIZE GB', 'DISK $/H', 'INSTANCES'], [
                [r.get('id', '-'), r.get('label') or '-', r.get('machine_id') or '-',
                 r.get('geolocation') or '-', r.get('status') or '-', r.get('disk_space', '-'),
                 f"{r['storage_total_cost']:.5f}" if r.get('storage_total_cost') is not None else '-',
                 ', '.join(str(x.get('id', '-')) if isinstance(x, dict) else str(x) for x in (r.get('instances') or [])) or '-']
                for r in rows])
            return
        elif args.command == 'volume-delete': result = checked(storage.delete_volume(vast.client, args.id))
        elif args.command == 'upload':
            upload_file(vast, args.id, args.file, args.destination, args.key); return
        elif args.command == 'volume-copy':
            from vastai.utils import parse_vast_url
            src, path = parse_vast_url(args.source)
            if not isinstance(src, str) or src.split('.')[0] not in ('s3', 'drive', 'b2', 'hf', 'dropbox'):
                raise ValueError('Local files cannot be copied directly to a volume; use a connected cloud source')
            if not args.destination.startswith('/') or args.destination == '/':
                raise ValueError('Destination must be an absolute path below /')
            result = checked(storage.copy(vast.client, src, f'V.{args.id}', path, args.destination))
        elif args.command == 'status': result = instance(vast, args.id)
        elif args.command in ('start', 'stop', 'destroy'):
            result = checked(getattr(vast, args.command + '_instance')(id=args.id))
        elif args.command == 'rent':
            result = rent(vast, args.offer, args.image, args.disk, args.bid, args.label, args.receipt,
                          args.volume, args.mount, args.strata)
        elif args.command == 'key':
            key = args.public_key.expanduser().read_text().strip()
            result = checked(vast.attach_ssh(args.instance, key) if args.instance is not None
                             else vast.create_ssh_key(ssh_key=key))
        elif args.command == 'ssh':
            print(shlex.join(ssh_command(instance(vast, args.id), args.key))); return
        else:
            result = provision(vast, args.id, args.key, args.script, args.timeout)
        print_result(result)
    except requests.RequestException as error:
        code = error.response.status_code if error.response is not None else type(error).__name__
        parser.exit(1, f'Vast request failed: {code}\n')
    except (OSError, ValueError, LookupError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
