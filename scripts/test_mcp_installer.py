"""Test configuration preservation and failure safety without touching real Codex settings."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('installer', Path(__file__).parent / 'mac_mcp/install_mcp.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

SETTINGS = dict(command='/Users/Test User/python', args=['-m', 'freevideo_engine.agent.mcp'],
                cwd='/Users/Test User/freevideo', enabled=True)
ORIGINAL = '''# User preferences — keep this comment
model = "my-model" # exact inline comment
instructions = """line one
[mcp_servers.fake]
line three"""

[projects."/Users/Test User/My Project"]
trust_level = "trusted"

[mcp_servers.other]
command = "other-server"
args = ["--keep"]

[mcp_servers.other.env]
PRIVATE_SETTING = "unchanged"
'''


class Configuration(unittest.TestCase):
    def test_preserves_other_settings_comments_and_multiline_strings(self):
        result = installer.updated_config(ORIGINAL, SETTINGS)
        self.assertTrue(result.startswith(ORIGINAL))
        self.assertEqual(installer.without_freevideo(tomllib.loads(result)), tomllib.loads(ORIGINAL))

    def test_repeat_is_idempotent(self):
        result = installer.updated_config(ORIGINAL, SETTINGS)
        self.assertEqual(installer.updated_config(result, SETTINGS), result)

    def test_updates_quoted_existing_server_and_keeps_tool_permissions(self):
        text = ORIGINAL + '''
[mcp_servers."freevideo"]
url = "https://old.invalid/mcp"
bearer_token_env_var = "OLD_TOKEN"
enabled = false
disabled_tools = ["freevideo_cancel"]

[mcp_servers.freevideo.tools.freevideo_generate]
approval_mode = "prompt"
'''
        result = tomllib.loads(installer.updated_config(text, SETTINGS))
        fv = result['mcp_servers']['freevideo']
        self.assertNotIn('url', fv)
        self.assertNotIn('bearer_token_env_var', fv)
        self.assertEqual(fv['disabled_tools'], ['freevideo_cancel'])
        self.assertEqual(fv['tools']['freevideo_generate']['approval_mode'], 'prompt')
        self.assertEqual(fv['command'], SETTINGS['command'])

    def test_inline_server_tables(self):
        text = 'mcp_servers = { other = { command = "keep" }, freevideo = { url = "https://old.invalid" } }\n'
        result = tomllib.loads(installer.updated_config(text, SETTINGS))
        self.assertEqual(result['mcp_servers']['other'], {'command': 'keep'})
        self.assertEqual(result['mcp_servers']['freevideo']['command'], SETTINGS['command'])

    def test_invalid_toml_is_never_written(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.toml'
            config.write_bytes(b'model = [broken')
            with self.assertRaises(ValueError):
                installer.save_config(config, SETTINGS)
            self.assertEqual(config.read_bytes(), b'model = [broken')
            self.assertEqual(list(Path(temp).iterdir()), [config])

    def test_backup_is_exact_and_repeated_save_does_not_add_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.toml'
            original = ORIGINAL.replace('\n', '\r\n').encode()
            config.write_bytes(original)
            backup = installer.save_config(config, SETTINGS)
            self.assertEqual(backup.read_bytes(), original)
            self.assertIsNone(installer.save_config(config, SETTINGS))
            self.assertEqual(len(list(Path(temp).glob('*backup*'))), 1)

    def test_new_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'new/config.toml'
            self.assertIsNone(installer.save_config(config, SETTINGS))
            data = tomllib.loads(config.read_text())
            self.assertTrue(data['mcp_servers']['freevideo']['enabled'])

    def test_atomic_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.toml'
            config.write_text(ORIGINAL, encoding='utf-8')
            before = config.read_bytes()
            with patch.object(installer.os, 'replace', side_effect=OSError('test failure')):
                with self.assertRaises(OSError):
                    installer.save_config(config, SETTINGS)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(list(Path(temp).glob('.freevideo-*')))

    def test_concurrent_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.toml'
            config.write_text(ORIGINAL, encoding='utf-8')
            original_update = installer.updated_config
            def update(text, settings):
                result = original_update(text, settings)
                config.write_text('# concurrent edit\n', encoding='utf-8')
                return result
            with patch.object(installer, 'updated_config', side_effect=update):
                with self.assertRaises(RuntimeError):
                    installer.save_config(config, SETTINGS)
            self.assertEqual(config.read_text(), '# concurrent edit\n')

    def test_failed_connection_does_not_touch_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, release = root / 'source', root / 'release'
            (source / 'freevideo_engine/agent').mkdir(parents=True)
            (source / 'freevideo_engine/agent/mcp.py').touch()
            (source / 'LICENSE').write_text('license')
            release.mkdir()
            config = root / 'config.toml'
            config.write_text(ORIGINAL, encoding='utf-8')
            before = config.read_bytes()
            with patch.object(installer, 'prepare_credentials', return_value=(root / 'key', root / 'ca')), \
                 patch.object(installer, 'verify_client', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    installer.install(source, release, 'https://192.168.1.2:8443', config, root, root / 'python')
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(list(root.glob('*backup*')))


if __name__ == '__main__':
    unittest.main()
