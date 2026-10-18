"""Package prompts must never consume a curl | bash script's source."""
import fcntl
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import termios
import unittest


class InstallerInputTests(unittest.TestCase):
    def test_piped_arch_installer_reads_confirmation_from_terminal(self):
        source = (Path(__file__).resolve().parents[1] / 'static/install.sh').read_text()
        # Execute the actual package-install block without installing packages.
        block = source[source.index('if [[ ${1:-} == --install-deps ]]; then'):source.index('for tool in')]
        with tempfile.TemporaryDirectory() as directory:
            bindir = Path(directory)
            (bindir / 'pacman').write_text('#!/bin/bash\nexit 0\n')
            (bindir / 'sudo').write_text('#!/bin/bash\nIFS= read -r answer\nprintf "CONFIRM=%s\\n" "$answer"\n')
            for tool in bindir.iterdir():
                tool.chmod(0o755)
            master, slave = pty.openpty()

            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            try:
                proc = subprocess.Popen(
                    ['/bin/bash', '-s', '--', '--install-deps'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=dict(os.environ, PATH=directory), pass_fds=(slave,),
                    preexec_fn=controlling_terminal,
                )
                os.write(master, b'yes\n')
                code = 'set -eu\nfail() { echo "$*" >&2; exit 1; }\n' + block + '\nprintf "SCRIPT_FINISHED\\n"\n'
                stdout, stderr = proc.communicate(code.encode(), timeout=10)
                self.assertEqual(proc.returncode, 0, stderr.decode())
                self.assertIn(b'CONFIRM=yes\n', stdout)
                self.assertIn(b'SCRIPT_FINISHED\n', stdout)
            finally:
                os.close(master)
                os.close(slave)

    def test_dependencies_without_terminal_fail_clearly(self):
        source = (Path(__file__).resolve().parents[1] / 'static/install.sh').read_text()
        block = source[source.index('if [[ ${1:-} == --install-deps ]]; then'):source.index('for tool in')]
        proc = subprocess.run(
            ['/bin/bash', '-s', '--', '--install-deps'],
            input='set -eu\nfail() { echo "$*" >&2; exit 1; }\n' + block,
            text=True, capture_output=True, start_new_session=True, timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('interactive terminal', proc.stderr)


if __name__ == '__main__':
    unittest.main()
