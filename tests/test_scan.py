"""scripts/zam-scan.py notifications: python3 -m unittest discover tests
notify_transitions runs on Zellij's agent states the bar passes with --native, against a temp notify dir."""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("zam_scan", os.path.join(ROOT, "scripts", "zam-scan.py"))
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

CLAUDE = {"kind": "claude", "zellij_session": "work", "pane_id": 3, "name": "svc", "branch": "main"}


def native(status, since, muted=False, pane=3, app="claude-code", saw_entry=True):
    return {"session": "work", "pane": pane, "status": status, "since_ms": since, "saw_entry": saw_entry,
            "muted": muted, "app": app,
            "title": "fix login", "text": "needs approval: Bash" if status == "needs_you" else ""}


class NotifyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for name, value in (("NOTIFY_DIR", tmp), ("send_notification", mock.Mock())):
            patcher = mock.patch.object(scan, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = scan.send_notification

    def scan(self, *entries, agents=(CLAUDE,)):
        """one bar's scan; the notifications it sent"""
        self.sent.reset_mock()
        scan.notify_transitions(list(agents), list(entries))
        return [c.args for c in self.sent.call_args_list]

    def test_first_sight_only_records(self):
        self.assertEqual(self.scan(native("needs_you", 100)), [])
        self.assertEqual(self.scan(native("needs_you", 100)), [])

    def test_fires_once_on_needs_you_done_failed(self):
        self.scan(native("working", 100))
        self.assertEqual(self.scan(native("needs_you", 200)),
                         [("Claude needs you", "svc@main · fix login · needs approval: Bash")])
        self.assertEqual(self.scan(native("needs_you", 200)), [])  # the same state again, as another bar sees it
        self.assertEqual(self.scan(native("working", 300)), [])
        self.assertEqual(self.scan(native("done", 400)), [("Claude finished", "svc@main · fix login")])
        self.assertEqual(self.scan(native("idle", 400)), [])  # done, then seen in Zellij
        self.scan(native("working", 500))
        self.assertEqual(self.scan(native("failed", 600)), [("Claude failed", "svc@main · fix login")])

    def test_claimed_once_across_bars(self):
        self.scan(native("working", 100))
        sent = self.scan(native("done", 200))
        # a second bar whose record file was lost still finds the claim
        os.remove(os.path.join(scan.NOTIFY_DIR, "work:3"))
        self.scan(native("working", 100))
        self.assertEqual((len(sent), self.scan(native("done", 200))), (1, []))

    def test_never_when_muted(self):
        self.scan(native("working", 100, muted=True))
        self.assertEqual(self.scan(native("needs_you", 200, muted=True)), [])
        self.assertEqual(self.scan(native("done", 300, muted=True)), [])
        # unmuted later: the old change does not fire, the next one does
        self.assertEqual(self.scan(native("done", 300)), [])
        self.scan(native("working", 400))
        self.assertEqual(len(self.scan(native("done", 500))), 1)

    def test_never_moves_backwards(self):
        self.scan(native("working", 100))
        self.scan(native("done", 300))
        self.assertEqual(self.scan(native("working", 200)), [])  # a stale snapshot from another bar
        self.assertEqual(self.scan(native("done", 300)), [])

    def test_same_status_again_fires(self):
        # two approval prompts within one scan interval, and a short turn between scans (the bar sends a new since
        # only on a new entry into the status, not when an agent re-sends the same state)
        self.scan(native("working", 500))
        self.assertEqual(len(self.scan(native("needs_you", 1000))), 1)
        self.assertEqual(len(self.scan(native("needs_you", 3000))), 1)
        self.assertEqual(len(self.scan(native("done", 5000))), 1)
        self.assertEqual(self.scan(native("done", 9000)), [("Claude finished", "svc@main · fix login")])
        self.assertEqual(self.scan(native("done", 9000)), [])  # the same entry again, as another bar sees it
        self.assertEqual(self.scan(native("done", 9000, muted=True)), [])

    def test_bar_started_since(self):
        # a new bar knows only Zellij's latest restamp of the unanswered prompt: not a second prompt
        self.scan(native("working", 100))
        self.assertEqual(len(self.scan(native("needs_you", 5000))), 1)
        self.assertEqual(self.scan(native("needs_you", 6000, saw_entry=False)), [])
        self.assertEqual(self.scan(native("needs_you", 5000)), [])  # the bar that saw it, again
        # another status is news to it, and so is an entry it saw
        self.assertEqual(len(self.scan(native("done", 9000, saw_entry=False))), 1)
        self.scan(native("working", 9500))
        self.assertEqual(len(self.scan(native("done", 9800))), 1)

    def test_since_zero_is_not_older(self):
        # seen twice: Zellij reports idle with no time; it is recorded, and the next change still fires
        self.scan(native("working", 100))
        self.scan(native("done", 300))
        self.scan(native("idle", 0))
        with open(os.path.join(scan.NOTIFY_DIR, "work:3")) as f:
            self.assertEqual(f.read(), "idle 0")
        self.assertEqual(len(self.scan(native("done", 300))), 0)  # a stale done from another bar: already claimed
        self.assertEqual(len(self.scan(native("done", 900))), 1)

    def test_agent_the_scanner_does_not_know(self):
        self.scan(native("working", 100, pane=9, app="opencode"))
        self.assertEqual(self.scan(native("needs_you", 200, pane=9, app="opencode")),
                         [("Opencode needs you", "fix login · needs approval: Bash")])


if __name__ == "__main__":
    unittest.main()
