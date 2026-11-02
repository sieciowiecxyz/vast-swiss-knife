"""Tests of mounted-model discovery and refusal to start incomplete models."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

setup = types.ModuleType('setup')
setup.nvfp4_files = lambda family: ([('pack', 'repo', 'revision', 'pack/index.txt', 3, 'hash')], None)
setup.nvfp4_args = lambda paths, ctx, kv, vision, extra=(): ['--pack', str(paths['pack']), *extra]
with patch.dict(sys.modules, {'setup': setup}):
    spec = importlib.util.spec_from_file_location('strata_vast_start', Path(__file__).with_name('start.py'))
    start = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start)


class StartupTests(unittest.TestCase):
    def test_gpu_and_cpu_capabilities_are_checked(self):
        good_cpu = 'flags : fma avx2 f16c bmi2\n'
        with patch.object(start.platform, 'machine', return_value='x86_64'), \
             patch.object(Path, 'read_text', return_value=good_cpu), \
             patch.object(start.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'RTX 5090, 12.0, 580.95.05, 32607\n')
            start.check_hardware()
            run.return_value.stdout = 'Older card, 6.1, 580.95.05, 24576\n'
            with self.assertRaisesRegex(ValueError, 'Unsupported GPU'):
                start.check_hardware()

    def test_container_memory_limit_wins_over_host(self):
        with patch.object(start.os, 'sysconf', side_effect=lambda name: 4096 if name == 'SC_PAGE_SIZE' else 67108864), \
             patch.object(Path, 'read_text', side_effect=['68719476736', FileNotFoundError()]):
            self.assertEqual(start.effective_memory(), 64 * 1024**3)

    def test_ssh_key_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'authorized_keys'
            key.write_text('public key')
            key.chmod(0o666)
            with patch.object(start.os, 'fchown'):
                start.secure_ssh_key(key)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            link = Path(directory) / 'link'
            link.symlink_to(key)
            with self.assertRaises(OSError):
                start.secure_ssh_key(link)

    def test_discovers_volume_and_rejects_missing_or_partial_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                start.find_model([root])
            model = root / 'qwen'
            (model / 'pack').mkdir(parents=True)
            (model / 'pack/index.txt').write_bytes(b'x')
            with patch.dict('os.environ', {'MODEL_DIR': str(root)}, clear=True), \
                 patch.object(start, 'check_hardware'), patch.object(start, 'secure_ssh_key'):
                with self.assertRaisesRegex(ValueError, 'incomplete model'):
                    start.main()
            (model / 'pack/index.txt').write_bytes(b'abc')
            self.assertEqual(start.find_model([root]), model)
            other = root / 'other/pack'
            other.mkdir(parents=True)
            (other / 'index.txt').write_bytes(b'abc')
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                start.find_model([root])


if __name__ == '__main__':
    unittest.main()
