"""setup.sh / scripts/zam-setup.py and the install scripts it drives: python3 -m unittest discover tests
Everything runs against temp dirs (HOME, ZELLIJ_CONFIG_DIR, CLAUDE_CONFIG_DIR, CODEX_HOME), with a fake zellij on PATH
that prints the dumps in tests/fixtures (taken from zellij 0.45.1)."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("zam_setup", os.path.join(ROOT, "scripts", "zam-setup.py"))
zs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zs)
REAL_RUN = subprocess.run

FAKE_ZELLIJ = f"""#!/bin/sh
case "$*" in
  --version) echo "zellij 0.45.1" ;;
  "setup --dump-config") cat "{FIX}/dump-config.kdl" ;;
  "setup --dump-layout default") cat "{FIX}/default.kdl" ;;
  "setup --dump-swap-layout default") cat "{FIX}/default.swap.kdl" ;;
  *) exit 1 ;;
esac
"""


AGENT_BINS = ("claude", "codex", "agy", "cursor-agent")


class SetupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # system tools by symlink, minus any real agent: "not found" must not depend on the machine
        if not shutil.which("jq"):
            raise unittest.SkipTest("jq is required by the install scripts and is not installed")
        cls.sysbin = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.sysbin)
        for d in ("/usr/bin", "/bin"):
            for f in os.listdir(d) if os.path.isdir(d) else []:
                if f not in AGENT_BINS and not os.path.lexists(os.path.join(cls.sysbin, f)):
                    os.symlink(os.path.join(d, f), os.path.join(cls.sysbin, f))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.bin = bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        with open(os.path.join(bin_, "zellij"), "w") as f:
            f.write(FAKE_ZELLIJ)
        os.chmod(os.path.join(bin_, "zellij"), 0o755)
        os.symlink(sys.executable, os.path.join(bin_, "python3"))
        os.symlink(shutil.which("jq"), os.path.join(bin_, "jq"))
        self.zc = os.path.join(self.home, "zellij")
        self.claude = os.path.join(self.home, "claude")
        self.codex = os.path.join(self.home, "codex")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, PATH=f"{bin_}:{self.sysbin}", ZELLIJ_CONFIG_DIR=self.zc,
                        CLAUDE_CONFIG_DIR=self.claude, CODEX_HOME=self.codex,
                        XDG_CACHE_HOME=os.path.join(self.home, ".cache"))
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sh(self, *args):
        return subprocess.run(args, env=self.env, cwd=ROOT, capture_output=True, text=True, check=True).stdout

    def status(self):
        """{label: what follows it} of the indented --status lines; an agent's tool line under "<agent> tool"."""
        s, last = {}, None
        for line in self.sh("./setup.sh", "--status").splitlines()[2:]:
            if line.startswith("  "):
                last = line[2:18].strip() or last
                s[line[2:18].strip() or last + " tool"] = line[18:]
        return s

    def fake_bin(self, name, body, where=None):
        path = os.path.join(where or self.bin, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def write_json(self, path, obj):  # formatted as jq writes it, so a restored file compares byte for byte
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = json.dumps(obj, indent=2) + "\n"
        with open(path, "w") as f:
            f.write(text)
        return text

    def config(self, text):
        os.makedirs(self.zc, exist_ok=True)
        with open(os.path.join(self.zc, "config.kdl"), "w") as f:
            f.write(text)

    # ---- modifier detection and clashes ----

    def test_detect_stock(self):
        binds = zs.effective_binds("")
        self.assertEqual(zs.detect_mod(binds), "Ctrl")
        self.assertEqual(zs.clashes(binds, "Ctrl Alt"), [])

    def test_detect_clear_defaults(self):
        binds = zs.effective_binds("""keybinds clear-defaults=true {
    normal {
        bind "Ctrl Alt g" { SwitchToMode "locked"; }
    }
    shared_except "locked" "pane" {
        bind "Ctrl Alt p" { SwitchToMode "pane"; }
    }
}
""")
        self.assertEqual(zs.detect_mod(binds), "Ctrl Alt")
        self.assertEqual(zs.clashes(binds, "Alt"), [])  # the stock Alt binds are cleared

    def test_clashes_stock_alt(self):
        c = zs.clashes(zs.effective_binds(""), "Alt")
        self.assertEqual(c, ['Alt Left (MoveFocusOrTab "Left")', 'Alt Right (MoveFocusOrTab "Right")'])

    def test_own_lines_are_not_clashes(self):
        self.sh("scripts/install.sh", "--mod", "Ctrl Alt")
        self.assertEqual(zs.clashes(zs.read_state().binds, "Ctrl Alt"), [])

    # ---- Zellij status and install.sh flags ----

    def test_status_fresh(self):
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        s = self.status()
        self.assertEqual(s["layouts"], "[○ not installed] ›")
        self.assertEqual(s["default_layout"], "[○ off] ›")
        self.assertEqual(s["jump keys"], "[○ not installed] ›  default modifier: Ctrl")
        self.assertEqual(s["notify"], "[✓ on]  (default)  install the layouts first")
        self.assertEqual(s["Claude"], "[○ not installed] ›")
        self.assertEqual(s["Claude tool"], "○ claude not on PATH  · config ~/claude")
        self.assertEqual(s["Codex"], "[○ not installed] ›")
        self.assertEqual(s["Cursor"], "○ not found: no cursor-agent on PATH, no ~/.cursor")
        self.assertEqual(s["zellij"], "✓ 0.45.1")

    def test_mod_and_opts_survive_rerun(self):
        self.sh("scripts/install.sh", "--mod", "Alt", "--opt", "notify=false", "--opt", "debug_dump=/tmp/f")
        s = self.status()
        self.assertEqual(s["layouts"], "[✓ installed] ›")
        self.assertEqual(s["default_layout"], "[✓ on] ›")
        self.assertTrue(s["jump keys"].startswith("[✓ Alt] ›  ⚠ clashes with Alt Left"), s["jump keys"])
        self.assertEqual(s["notify"], "[○ off] ›")
        self.sh("scripts/install.sh")
        s = self.status()
        self.assertEqual(s["notify"], "[○ off] ›")
        self.assertEqual(s["jump keys"], "[✓ Ctrl Alt] ›")
        for f in ("agents.kdl", "agents.swap.kdl"):
            with open(os.path.join(self.zc, "layouts", f)) as fh:
                text = fh.read()
            self.assertIn('notify false\n', text)
            self.assertIn('debug_dump "/tmp/f"\n', text)
        self.sh("scripts/install.sh", "--opt", "notify=")
        self.assertEqual(self.status()["notify"], "[✓ on] ›  (default)")
        self.assertEqual(zs.read_state().opts, {"debug_dump": "/tmp/f"})

    def test_no_default_layout(self):
        self.sh("scripts/install.sh")
        self.sh("scripts/install.sh", "--no-default-layout")
        s = self.status()
        self.assertEqual(s["default_layout"], "[○ off] ›")
        self.assertEqual(s["jump keys"], "[✓ Ctrl Alt] ›")
        with open(os.path.join(self.zc, "config.kdl")) as f:
            self.assertNotIn("default_layout", f.read())

    def test_other_default_layout_kept(self):
        self.config('default_layout "compact"\n')
        self.sh("scripts/install.sh")
        self.assertEqual(self.status()["default_layout"], '[⚠ other: "compact"] ›')
        self.assertEqual(zs.zellij_cmds(zs.read_state(), mod="Alt"), [["--no-default-layout", "--mod", "Alt"]])

    def test_stale_layouts(self):
        self.sh("scripts/install.sh", "--layouts-only")
        p = os.path.join(self.zc, "layouts", "agents.kdl")
        with open(p) as f:
            text = f.read().replace(ROOT, "/elsewhere/zellij-agents-manager")
        with open(p, "w") as f:
            f.write(text)
        self.assertEqual(self.status()["layouts"], "[⚠ stale: from another checkout] ›")

    def test_zellij_cmds(self):
        st = zs.read_state()
        self.assertEqual(zs.zellij_cmds(st), [["--mod", "Ctrl"]])
        self.sh("scripts/install.sh", "--mod", "Alt", "--opt", "notify=false")
        st = zs.read_state()
        self.assertEqual(zs.zellij_cmds(st, opts={"interval": "5"}), [["--mod", "Alt", "--opt", "interval=5"]])
        self.assertEqual(zs.zellij_cmds(st, dl=False), [["--no-default-layout", "--mod", "Alt"]])
        self.assertEqual(zs.zellij_cmds(st, mod=None), [["--uninstall"], ["--layouts-only"]])
        with self.backends():
            zs.zellij_change(st, mod=None)
        s = self.status()
        self.assertEqual((s["layouts"], s["jump keys"], s["default_layout"], s["notify"]),
                         ("[✓ installed] ›", "[○ not installed] ›  default modifier: Ctrl", "[○ off] ›", "[○ off] ›"))

    def quiet_run(self, args, **kw):
        return REAL_RUN(args, **dict(dict(env=self.env, capture_output=True, text=True), **kw))

    @contextlib.contextmanager
    def backends(self):  # setup.sh's backend runs, quiet and without waiting for enter; yields the run mock
        with mock.patch("builtins.input"), mock.patch("builtins.print"), \
                mock.patch("subprocess.run", side_effect=self.quiet_run) as run:
            yield run

    def layout_text(self):
        with open(os.path.join(self.zc, "layouts", "agents.kdl")) as f:
            return f.read()

    def hand_edit_plugin_block(self, lines):
        p = os.path.join(self.zc, "layouts", "agents.kdl")
        text = self.layout_text().replace("zam-scan.py\"\n", "zam-scan.py\"\n" + "".join(" " * 12 + l + "\n" for l in lines))
        with open(p, "w") as f:
            f.write(text)

    def test_hand_written_plugin_lines(self):
        # comments and bare nodes aren't options, survive every change verbatim, and only the changed key is passed
        self.sh("scripts/install.sh")
        own = ["// notify false", "debug", 'debug_dump "C:\\tmp\\x"']
        self.hand_edit_plugin_block(own)
        self.assertEqual(zs.read_state().opts, {"debug_dump": '"C:\\tmp\\x"'})  # not JSON: as written, no crash
        self.assertEqual(self.status()["notify"], "[✓ on] ›  (default)")
        st = zs.read_state()
        with self.backends() as run:
            zs.set_opt(st, "notify", "false")
        self.assertEqual([c[0][0][1:] for c in run.call_args_list], [["--mod", "Ctrl Alt", "--opt", "notify=false"]])
        for l in own:
            self.assertIn(" " * 12 + l + "\n", self.layout_text())
        st = zs.read_state()
        with self.backends():
            zs.zellij_change(st, mod=None)  # --uninstall, then --layouts-only
        s = self.status()
        self.assertEqual((s["jump keys"][:17], s["notify"]), ("[○ not installed]", "[○ off] ›"))
        for l in own:
            self.assertIn(" " * 12 + l + "\n", self.layout_text())

    def test_tab_separated_plugin_lines(self):
        # KDL whitespace between name and value may be a tab: read back, and replaced (not duplicated) by --opt
        self.sh("scripts/install.sh")
        self.hand_edit_plugin_block(["notify\tfalse", "interval\t5"])
        self.assertEqual(zs.read_state().opts, {"notify": "false", "interval": "5"})
        s = self.status()
        self.assertEqual((s["notify"], s["interval"]), ("[○ off] ›", "[5 s] ›"))
        st = zs.read_state()
        with self.backends() as run:
            zs.set_opt(st, "notify", "true")
        self.assertEqual([c[0][0][1:] for c in run.call_args_list], [["--mod", "Ctrl Alt", "--opt", "notify="]])
        self.assertNotIn("notify", self.layout_text())
        self.assertIn(" " * 12 + "interval\t5\n", self.layout_text())
        self.sh("scripts/install.sh", "--opt", "interval=3")
        self.assertEqual(self.layout_text().count("interval"), 1)
        self.assertEqual(zs.read_state().opts, {"interval": "3"})

    def test_blank_lines_in_plugin_block(self):
        self.sh("scripts/install.sh")
        self.hand_edit_plugin_block(["notify false", "", "   ", "interval 5"])
        self.assertEqual(zs.read_state().opts, {"notify": "false", "interval": "5"})
        s = self.status()
        self.assertEqual((s["notify"], s["interval"]), ("[○ off] ›", "[5 s] ›"))
        self.sh("scripts/install.sh", "--opt", "notify=true")
        self.assertEqual(zs.read_state().opts, {"notify": "true", "interval": "5"})

    def test_non_ascii_opt(self):
        self.sh("scripts/install.sh", "--opt", "debug_dump=/tmp/ž")
        self.assertIn('debug_dump "/tmp/ž"\n', self.layout_text())
        self.assertEqual(zs.read_state().opts, {"debug_dump": "/tmp/ž"})

    def test_options_need_layouts(self):
        s = self.status()
        self.assertEqual(s["notify"], "[✓ on]  (default)  install the layouts first")
        self.assertEqual([a for segs, a in zs.rows(zs.read_state()) if segs and segs[0][0].strip() in zs.OPT_DEFAULTS],
                         [None, None, None])

    def test_switch_default_layout_and_back(self):
        self.config('theme "x"\ndefault_layout "compact"\n')
        self.sh("scripts/install.sh")
        with mock.patch.object(zs, "pick", side_effect=[0, 1]), self.backends():
            zs.act_default_layout(zs.read_state())
        self.assertEqual(self.status()["default_layout"], "[✓ on] ›")
        for removal in (["--no-default-layout"], ["--uninstall"]):
            self.sh("scripts/install.sh", *removal)
            self.assertEqual(self.status()["default_layout"], '[⚠ other: "compact"] ›')
            with open(os.path.join(self.zc, "config.kdl")) as f:
                text = f.read()
            self.assertIn('\ndefault_layout "compact"\n', text)
            self.assertNotIn("replaced", text)
            with mock.patch.object(zs, "pick", side_effect=[0, 1]), self.backends():
                zs.act_default_layout(zs.read_state())

    ALT_PANE = 'keybinds {\n    normal {\n        bind "Alt p" { SwitchToMode "pane"; }\n    }\n}\n'

    def test_install_layouts_confirms_clashes(self):
        # a fresh install puts the jump keys on the detected Alt, which clashes with the stock Alt Left/Right
        self.config(self.ALT_PANE)
        before = self.mtimes()
        st = zs.read_state()
        with mock.patch.object(zs, "pick", side_effect=[0, 0]) as pick, self.backends() as run:
            self.assertFalse(zs.act_layouts(st))  # Install, then Cancel at the clashes
        self.assertIn("Alt Left", pick.call_args[0][0])
        self.assertEqual((run.call_count, self.mtimes()), (0, before))
        with mock.patch.object(zs, "pick", side_effect=[0, 1]), self.backends():
            zs.act_layouts(zs.read_state())  # Install, then Use Alt anyway
        self.assertTrue(self.status()["jump keys"].startswith("[✓ Alt] ›  ⚠ clashes with Alt Left"))

    def test_default_layout_confirms_clashes(self):
        # turning default_layout on installs the jump keys too: on, from off and from another layout
        self.config(self.ALT_PANE)
        before = self.mtimes()
        st = zs.read_state()
        with mock.patch.object(zs, "pick", side_effect=[0, 0]) as pick, self.backends() as run:
            self.assertFalse(zs.act_default_layout(st))  # on, then Cancel at the clashes
        self.assertIn("Alt Left", pick.call_args[0][0])
        self.assertEqual((run.call_count, self.mtimes()), (0, before))
        self.config('default_layout "compact"\n' + self.ALT_PANE)
        before = self.mtimes()
        st = zs.read_state()
        with mock.patch.object(zs, "pick", side_effect=[0, 1, 0]) as pick, self.backends() as run:
            self.assertFalse(zs.act_default_layout(st))  # switch, yes, then Cancel at the clashes
        self.assertIn("Alt Left", pick.call_args[0][0])
        self.assertEqual((run.call_count, self.mtimes()), (0, before))
        with mock.patch.object(zs, "pick", side_effect=[0, 1, 1]), self.backends():
            zs.act_default_layout(zs.read_state())  # switch, yes, Use Alt anyway
        s = self.status()
        self.assertEqual(s["default_layout"], "[✓ on] ›")
        self.assertTrue(s["jump keys"].startswith("[✓ Alt] ›"), s["jump keys"])

    def test_remove_layouts_warns_about_own_default_layout(self):
        self.config('default_layout "agents"\n')
        self.sh("scripts/install.sh", "--layouts-only")
        with mock.patch.object(zs, "pick", return_value=None) as pick:
            zs.act_layouts(zs.read_state())
        self.assertIn('your own default_layout "agents" stays', "".join(t for t, _ in pick.call_args[0][1][1][0]))

    def test_interval_rejects_unicode_digits(self):
        self.sh("scripts/install.sh")
        with mock.patch("builtins.input", side_effect=["²", ""]):
            self.assertFalse(zs.act_interval(zs.read_state()))

    def test_state_read_once_per_selection(self):
        zs.stock_binds.cache_clear()
        with mock.patch.object(zs, "cmd_out", wraps=zs.cmd_out) as out:
            zs.effective_binds("")
            zs.effective_binds("")
        self.assertEqual([c for c in out.call_args_list if "--dump-config" in c[0]], [mock.call("zellij", "setup", "--dump-config")])
        layouts = next(i for i, (_, a) in enumerate(zs.rows(zs.read_state())) if a)
        with mock.patch.object(zs, "read_state", wraps=zs.read_state) as rs, \
                mock.patch.object(zs, "pick", side_effect=[layouts, None]), \
                mock.patch.object(zs, "act_layouts", return_value=False) as act:
            zs.main([])
        self.assertEqual((rs.call_count, act.call_count), (2, 1))  # two screens, one action

    # ---- agents: install -> installed, --uninstall -> the original file ----

    def agent_roundtrip(self, script, name, files):
        originals = {p: self.write_json(p, obj) for p, obj in files.items()}
        self.assertEqual(self.status()[name], "[○ not installed] ›")
        self.sh(f"scripts/{script}")
        self.sh(f"scripts/{script}")  # idempotent
        s = self.status()[name]
        self.assertNotIn("not installed", s)
        self.assertNotIn("stale", s)
        self.sh(f"scripts/{script}", "--uninstall")
        for p, text in originals.items():
            with open(p) as f:
                self.assertEqual(f.read(), text, p)
        self.assertEqual(self.status()[name], "[○ not installed] ›")
        return s

    def test_claude(self):
        s = self.agent_roundtrip("install-hooks.sh", "Claude", {f"{self.claude}/settings.json": {
            "model": "opus",
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/guard.sh"}]}],
                      "Stop": []},
            "statusLine": {"type": "command", "command": "echo it's me", "padding": 0}}})
        self.assertEqual(s, "[✓ hooks  ✓ status line] ›")

    def test_claude_stale_hooks_replaced(self):
        p = f"{self.claude}/settings.json"
        old = "/old/checkout/scripts/zam-hook.sh"
        self.write_json(p, {"permissions": {"allow": ["Bash(scripts/zam-hook.sh:*)"]}, "hooks": {
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "/x/mine.sh"},
                                               {"type": "command", "command": old, "timeout": 5}]}],
            "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": old, "timeout": 5}]}]}})
        self.sh("scripts/install-hooks.sh")
        with open(p) as f:
            s = json.load(f)
        h = zs.os.path.join(ROOT, "scripts", "zam-hook.sh")
        self.assertNotIn(old, json.dumps(s))
        self.assertEqual(s["hooks"]["Stop"][0]["hooks"], [{"type": "command", "command": "/x/mine.sh"}])
        self.assertEqual([g["hooks"][0]["command"] for g in s["hooks"]["SessionEnd"]], [h])
        self.assertEqual(self.status()["Claude"], "[✓ hooks  ✓ status line] ›")
        # an old hook beside the current one is stale, not installed
        s["hooks"]["Stop"].append({"matcher": "", "hooks": [{"type": "command", "command": old}]})
        self.write_json(p, s)
        self.assertEqual(self.status()["Claude"], "[⚠ hooks stale: from another checkout  ✓ status line] ›")

    def test_claude_fresh_install_unchanged(self):
        # the stale-hook pass leaves a first install as it was: one group per event, appended
        p = f"{self.claude}/settings.json"
        self.write_json(p, {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "/x/a.sh"}]}]}})
        self.sh("scripts/install-hooks.sh")
        with open(p) as f:
            s = json.load(f)
        h = os.path.join(ROOT, "scripts", "zam-hook.sh")
        ours = [{"matcher": "", "hooks": [{"type": "command", "command": h, "timeout": 5}]}]
        self.assertEqual(s["hooks"]["Stop"], [{"matcher": "", "hooks": [{"type": "command", "command": "/x/a.sh"}]}] + ours)
        self.assertEqual(list(s["hooks"]), ["Stop", "PreToolUse", "PostToolUse", "PermissionRequest", "Notification",
                                            "UserPromptSubmit", "SessionEnd"])
        self.assertTrue(all(s["hooks"][e] == ours for e in s["hooks"] if e != "Stop"))

    def test_claude_without_hooks(self):
        self.agent_roundtrip("install-hooks.sh", "Claude", {f"{self.claude}/settings.json": {"env": {"A": "1"}}})

    def test_cursor(self):
        self.agent_roundtrip("install-cursor.sh", "Cursor", {
            f"{self.home}/.cursor/hooks.json": {"version": 1, "hooks": {"stop": [{"command": "/x/mine.sh"}]}},
            f"{self.home}/.cursor/cli-config.json": {"editor": {"vimMode": True}}})

    def test_codex(self):
        s = self.agent_roundtrip("install-codex.sh", "Codex", {f"{self.codex}/hooks.json": {
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/n.sh"}]}]}}})
        self.assertIn("/hooks", s)

    def test_agy(self):
        self.agent_roundtrip("install-agy.sh", "agy",
                             {f"{self.home}/.gemini/antigravity-cli/settings.json": {"theme": "dark"}})

    def test_uninstall_all(self):
        settings = {
            f"{self.claude}/settings.json": {"statusLine": {"command": "echo 'original'"}},
            f"{self.codex}/hooks.json": {"hooks": {"Stop": []}},
            f"{self.home}/.cursor/hooks.json": {"version": 1, "hooks": {}},
            f"{self.home}/.cursor/cli-config.json": {"editor": {"vimMode": True}},
            f"{self.home}/.gemini/antigravity-cli/settings.json": {"theme": "dark"},
        }
        for path, original in settings.items():
            self.write_json(path, original)
        os.makedirs(self.zc)
        config = Path(self.home, "dotfiles.kdl")
        original_config = 'default_layout "compact"\nkeybinds {\n}\n'
        config.write_text(original_config)
        config_link = Path(self.zc, "config.kdl")
        config_link.symlink_to(config)
        self.sh("./scripts/install.sh")
        config.write_text(config.read_text().replace('default_layout "compact"',
                          '// zellij-agents replaced: default_layout "compact"\n'
                          'default_layout "agents" // zellij-agents'))
        for script in ("hooks", "codex", "cursor", "agy"):
            self.sh(f"./scripts/install-{script}.sh")

        cache = Path(self.home, "custom cache")
        self.env["XDG_CACHE_HOME"] = str(cache)
        manager_cache = cache / "zellij-agents"
        manager_cache.mkdir(parents=True)
        (manager_cache / "state.json").touch()
        permissions = cache / "zellij" / "permissions.kdl"
        permissions.parent.mkdir()
        unrelated = '"/other/plugin.wasm" {\n    RunCommands\n}\n'
        permissions.write_text('"/old checkout/dist/zellij-agents.wasm" {\n    RunCommands\n}\n' + unrelated)

        self.sh("./uninstall.sh")
        self.assertTrue(config_link.is_symlink())
        self.assertEqual(config.read_text(), original_config)
        self.assertEqual(list(Path(self.zc, "layouts").iterdir()), [])
        for path, original in settings.items():
            self.assertEqual(json.loads(Path(path).read_text()), original)
        self.assertEqual(list(Path(self.home).rglob("*.zam.bak")), [])
        self.assertFalse(manager_cache.exists())
        self.assertEqual(permissions.read_text(), unrelated)
        before = self.mtimes()
        self.sh("./uninstall.sh")
        self.assertEqual(self.mtimes(), before)

    def test_uninstall_without_zellij_config(self):
        settings = Path(self.claude, "settings.json")
        settings.parent.mkdir()
        settings.write_text('{"model":"opus"}\n')
        before = self.mtimes()
        self.sh("./uninstall.sh")
        self.assertFalse(Path(self.zc).exists())
        self.assertEqual(self.mtimes(), before)

    def test_uninstall_failure_keeps_backups_for_retry(self):
        claude = Path(self.claude, "settings.json")
        claude.parent.mkdir()
        claude.write_text("invalid JSON")
        agy = Path(self.home, ".gemini", "antigravity-cli", "settings.json")
        original = {"theme": "dark"}
        self.write_json(agy, original)
        self.sh("./scripts/install-agy.sh")

        result = subprocess.run(["./uninstall.sh"], env=self.env, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Uninstall failed in scripts/install-hooks.sh", result.stderr)
        self.assertEqual(json.loads(agy.read_text()), original)
        backup = agy.with_suffix(".json.zam.bak")
        self.assertTrue(backup.exists())
        self.assertEqual(list(claude.parent.iterdir()), [claude])

        self.write_json(claude, {})
        self.sh("./uninstall.sh")
        self.assertFalse(backup.exists())

    # ---- read-only status, and the curses menu driven through a pty ----

    def mtimes(self):
        return {os.path.join(d, f): os.stat(os.path.join(d, f)).st_mtime_ns
                for d, _, fs in os.walk(self.home) for f in fs}

    def test_status_changes_nothing(self):
        os.makedirs(self.claude)
        self.sh("scripts/install.sh")
        self.sh("scripts/install-hooks.sh")
        before = self.mtimes()
        self.status()
        self.assertEqual(self.mtimes(), before)

    def menu(self, keys_after):
        """runs ./setup.sh in a pty; keys_after = [(text to wait for, keys to send)]"""
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(ROOT)
                os.execve("./setup.sh", ["./setup.sh"], dict(self.env, TERM="xterm", LINES="40", COLUMNS="160"))
            finally:
                os._exit(127)
        out = b""
        try:
            for want, keys in keys_after:
                deadline = time.time() + 10
                while want.encode() not in out:
                    if time.time() > deadline:
                        self.fail(f"timed out waiting for {want!r}; output: {out[-2000:]!r}")
                    if select.select([fd], [], [], 0.2)[0]:
                        try:
                            out += os.read(fd, 65536)
                        except OSError:
                            break
                out = b""
                os.write(fd, keys.encode())
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    if select.select([fd], [], [], 0.2)[0] and not os.read(fd, 65536):
                        break
                except OSError:
                    break
        finally:
            done, code = os.waitpid(pid, os.WNOHANG)
            if not done:  # still running (exiting, or a wait timed out): don't hang the suite
                time.sleep(0.5)
                done, code = os.waitpid(pid, os.WNOHANG)
                if not done:
                    os.kill(pid, 9)
                    _, code = os.waitpid(pid, 0)
            os.close(fd)
        return os.waitstatus_to_exitcode(code)

    def tty_status(self, **env):
        """./setup.sh --status with stdout on a terminal"""
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(ROOT)
                os.execve("./setup.sh", ["./setup.sh", "--status"],
                          dict({k: v for k, v in self.env.items() if k != "NO_COLOR"}, TERM="xterm", **env))
            finally:
                os._exit(127)
        out = b""
        deadline = time.time() + 10
        try:
            while time.time() < deadline:
                try:
                    if select.select([fd], [], [], 0.2)[0]:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        out += chunk
                except OSError:
                    break
        finally:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if not done:  # still running: don't hang the suite
                time.sleep(0.5)
                done, _ = os.waitpid(pid, os.WNOHANG)
                if not done:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
            os.close(fd)
        return out.decode()

    def test_status_colors(self):
        self.assertNotIn("\x1b", self.sh("./setup.sh", "--status"))  # piped: plain text
        self.assertIn("\x1b[32m✓ 0.45.1\x1b[0m", self.tty_status())
        plain = self.tty_status(NO_COLOR="1")
        self.assertNotIn("\x1b", plain)
        self.assertIn("✓ 0.45.1", plain)

    def test_agent_tools_found(self):
        claude = self.fake_bin("claude", 'echo "9.8.7 (Claude Code)"; echo more')
        codex = self.fake_bin("codex", "exit 1")  # no version: found all the same
        ran = os.path.join(self.tmp, "ran")  # cursor-agent isn't run, its install dir names the version
        cursor = self.fake_bin("cursor-agent", f"touch {ran}", os.path.join(self.tmp, "versions", "1.2.3"))
        os.symlink(cursor, os.path.join(self.bin, "cursor-agent"))
        s = self.status()
        self.assertEqual(s["Claude"], "[○ not installed] ›")
        self.assertEqual(s["Claude tool"], f"✓ 9.8.7 (Claude Code)  {claude}  · no config dir ~/claude")
        self.assertEqual(s["Codex tool"], f"✓ found  {codex}  · no config dir ~/codex")
        self.assertEqual(s["Cursor tool"], f"✓ 1.2.3  {self.bin}/cursor-agent  · no config dir ~/.cursor")
        self.assertFalse(os.path.exists(ran))
        self.assertTrue(s["agy"].startswith("○ not found"))

    def test_version_probe_times_out(self):
        slow = self.fake_bin("agy", "sleep 5 & sleep 5")  # its child holds the output pipe too
        start = time.time()
        with mock.patch.object(zs, "PROBE_TIMEOUT", 0.3):
            self.assertEqual(zs.version(slow), "")
        self.assertLess(time.time() - start, 2)

    @staticmethod
    def kill_pidfile(path):
        try:
            with open(path) as f:
                os.kill(int(f.read()), 9)
        except (OSError, ValueError):
            pass

    def test_clip_by_display_width(self):
        self.assertEqual(zs.clip("abcdef", 3), "abc")
        self.assertEqual(zs.clip("abc", 5), "abc")
        self.assertEqual(zs.clip("日本語", 5), "日本")  # 2 columns each: a third would need 6
        self.assertEqual(zs.clip("a日", 2), "a")
        self.assertEqual(zs.clip("日", 0), "")

    def test_version_probe_escaped_child(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        slow = self.fake_bin("agy", f"echo 1.0; setsid sleep 8 &\necho $! > {pidfile}\nsleep 5")  # the child leaves the process group
        self.addCleanup(self.kill_pidfile, pidfile)
        start = time.time()
        with mock.patch.object(zs, "PROBE_TIMEOUT", 0.3):
            self.assertEqual(zs.version(slow), "")
        self.assertLess(time.time() - start, 2)

    def test_info_rows_not_selectable(self):
        self.sh("scripts/install.sh")
        for segs, act in zs.rows(zs.read_state()):
            self.assertEqual("›" in "".join(t for t, _ in segs), act is not None, segs)
        before = self.mtimes()
        # up from the first selectable row (layouts) stays on it; down past every row ends on Quit
        self.assertEqual(self.menu([("Quit", "kkkk\n"), ("Reinstall / update", "q"), ("Quit", "j" * 30 + "\n")]), 0)
        self.assertEqual(self.mtimes(), before)

    def test_menu_quit_changes_nothing(self):
        os.makedirs(self.claude)
        self.sh("scripts/install.sh")
        before = self.mtimes()
        self.assertEqual(self.menu([("Quit", "q")]), 0)
        self.assertEqual(self.mtimes(), before)

    def test_menu_installs_layouts(self):
        # first selectable row is layouts: enter -> Install -> backend output -> enter -> back on the main screen
        self.assertEqual(self.menu([("Quit", "\n"), ("Install (layouts", "\n"), ("press enter", "\n"), ("Quit", "q")]), 0)
        s = self.status()
        self.assertEqual((s["layouts"], s["default_layout"], s["jump keys"]), ("[✓ installed] ›", "[✓ on] ›", "[✓ Ctrl] ›"))

    def test_menu_modifier_and_notify(self):
        self.config('theme "x"\nkeybinds {\n    normal {\n        bind "Ctrl p" { SwitchToMode "pane"; }\n    }\n}\n')
        self.sh("scripts/install.sh")
        with open(os.path.join(self.zc, "config.kdl")) as f:
            before = f.read()
        # jump keys (3rd row) -> Alt (3rd: Ctrl default, Ctrl Alt current, Alt) -> clash confirmation -> use anyway;
        # then, the cursor staying on jump keys, notify (next row) -> off
        self.assertEqual(self.menu([("Quit", "jj\n"), ("⚠ clashes with Alt Left", "jj\n"),
                                    ("which the jump keys would shadow", "j\n"), ("press enter", "\n"),
                                    ("Quit", "j\n"), ("notify:", "j\n"), ("press enter", "\n"), ("Quit", "q")]), 0)
        with open(os.path.join(self.zc, "config.kdl")) as f:
            after = f.read()
        self.assertEqual(after, before.replace('bind "Ctrl Alt ', 'bind "Alt '))
        self.sh("scripts/install.sh", "--mod", "Alt")
        s = self.status()
        self.assertEqual((s["notify"], s["jump keys"][:9]), ("[○ off] ›", "[✓ Alt] ›"))


if __name__ == "__main__":
    unittest.main()
