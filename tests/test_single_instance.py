import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QLockFile

from maildesk.single_instance import MailInstance

_CHILD = """
import os, sys, time
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QTimer
from maildesk.single_instance import MailInstance
app = QCoreApplication([])
gate = MailInstance(Path(sys.argv[1]))
if not gate.acquire_or_activate():
    print("forwarded", flush=True)
    sys.exit(0)
print("owner", flush=True)
if sys.argv[2] == "crash":
    os._exit(0)
if sys.argv[2] == "listen":
    def activated():
        print("activated", flush=True)
        app.quit()
    gate.activation_requested.connect(activated)
    # Simulate work on the main thread while the second launch asks for the window.
    time.sleep(0.35)
    QTimer.singleShot(4000, app.quit)
    app.exec()
gate.close()
"""


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def launch(self, action):
        process = subprocess.Popen([sys.executable, "-c", _CHILD, self.dir.name, action],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        return process

    def test_simultaneous_launches_elect_one_owner_and_deliver_activation(self):
        processes = [self.launch("listen"), self.launch("listen")]
        output = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stderr)
            output.extend(stdout.splitlines())
        self.assertCountEqual(output, ["owner", "activated", "forwarded"])

    def test_crashed_process_can_be_replaced(self):
        for action in ("crash", "exit"):
            process = self.launch(action)
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "owner")
        self.assertFalse((Path(self.dir.name) / "desktop.lock").exists())

    def test_unresponsive_live_owner_keeps_exclusive_ownership(self):
        directory = Path(self.dir.name)
        lock = QLockFile(str(directory / "desktop.lock"))
        self.assertTrue(lock.tryLock(0))
        self.addCleanup(lock.unlock)
        gate = MailInstance(directory)
        with patch("maildesk.single_instance.monotonic", side_effect=[0, 4]):
            with self.assertRaisesRegex(RuntimeError, "暂时未响应"):
                gate.acquire_or_activate()
        self.assertTrue(lock.isLocked())
        self.assertTrue((directory / "desktop.lock").exists())

    def test_second_normal_entry_point_returns_before_initializing_mail(self):
        owner = self.launch("listen")
        # The owner accepts a connection even before it enters the event loop.
        self.assertEqual(owner.stdout.readline().strip(), "owner")
        script = """
import sys
from unittest.mock import patch
from maildesk import __main__ as launcher
directory = sys.argv.pop()
with patch.object(launcher.QStandardPaths, "writableLocation", return_value=directory), \\
     patch.object(launcher, "MailService", side_effect=AssertionError("duplicate mail startup")):
    sys.exit(launcher.main())
"""
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        second = subprocess.run([sys.executable, "-c", script, self.dir.name], capture_output=True,
                                text=True, timeout=8, env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(owner.communicate(timeout=8)[0].strip(), "activated")
