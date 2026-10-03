import argparse
import importlib.util
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("rulebuild", Path(__file__).parents[1] / "scripts" / "rulebuild.py")
rulebuild = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rulebuild
SPEC.loader.exec_module(rulebuild)


class RuleBuildTests(unittest.TestCase):
    def test_log_prefix_colors(self):
        for level, color in rulebuild.LEVEL_COLORS.items():
            for tty, env, colored in (
                (True, {"TERM": "xterm-256color"}, True),
                (False, {"TERM": "xterm-256color"}, False),
                (True, {"TERM": "dumb"}, False),
                (True, {"NO_COLOR": "1"}, False),
            ):
                with self.subTest(level=level, tty=tty, env=env):
                    output = io.StringIO()
                    with (contextlib.redirect_stderr(output),
                          patch.object(output, "isatty", return_value=tty),
                          patch.dict(os.environ, env, clear=True),
                          patch.object(rulebuild, "DEBUG_ENABLED", True)):
                        rulebuild.log(level, "message")
                    prefix = f"[{level}]"
                    if colored:
                        prefix = f"\x1b[{color}m{prefix}\x1b[0m"
                    self.assertEqual(output.getvalue(), f"{prefix} message\n")

    def test_v2ray_prepare_stages_renamed_sources_for_domi(self):
        package, data = rulebuild.discover()["v2ray"]
        with tempfile.TemporaryDirectory() as temporary:
            srcdir, builddir = Path(temporary) / "src", Path(temporary) / "build"
            srcdir.mkdir()
            builddir.mkdir()
            with patch.object(rulebuild.urllib.request, "urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(b"dat fixture")):
                rulebuild.fetch_sources("v2ray", package, data["source"], srcdir, {})
            env = dict(os.environ, SRCDIR=str(srcdir), BUILDDIR=str(builddir))
            rulebuild.run_commands(data["prepare"]["command"], builddir, env, "v2ray: prepare")
            self.assertEqual((builddir / "domi.toml").read_bytes(), (package / "domi.toml").read_bytes())
            for filename in ("dlc.dat", "geosite.dat"):
                self.assertEqual((builddir / filename).read_bytes(), b"dat fixture")

    def test_log_levels_and_stage_failure_context(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(rulebuild, "DEBUG_ENABLED", False):
            rulebuild.log_tool_line("[DEBUG] hidden", "sample")
            rulebuild.log_tool_line("[ERROR] missing config", "sample")
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(rulebuild.BuildError, "sample: build: failed .*7"):
                    rulebuild.run_commands(["echo tool-output", "exit 7"], Path(temporary), dict(os.environ), "sample: build")
        lines = output.getvalue().splitlines()
        self.assertTrue(all(line.startswith(("[INFO]", "[ERROR]")) for line in lines))
        self.assertIn("[ERROR] sample: missing config", lines)
        self.assertIn("[INFO] sample: build: tool-output", lines)
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(rulebuild, "DEBUG_ENABLED", True):
            rulebuild.log_tool_line("\x1b[37mDEBUG\x1b[0m[0000] detail", "sample")
        self.assertEqual(output.getvalue(), "[DEBUG] sample: [0000] detail\n")

    @unittest.skipUnless(shutil.which("sing-box"), "requires sing-box")
    def test_domi_template_collects_and_compiles_all_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build, package, binaries = root / "build", root / "pkg", root / "bin"
            for directory in (build, package, binaries):
                directory.mkdir()
            (build / "domi.toml").write_text("# fixture config\n")
            domi = binaries / "domi-cli"
            domi.write_text(
                "#!" + sys.executable + "\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "assert sys.argv[1:] == ['--config', str(Path.cwd() / 'domi.toml')]\n"
                "for name in ('first', 'second'):\n"
                "    Path(name + '.json').write_text(json.dumps({'version': 3, 'rules': [{'domain': [name + '.example']}]}))\n"
            )
            domi.chmod(0o755)
            env = dict(os.environ, BUILDDIR=str(build), PKGDIR=str(package),
                       PATH=str(binaries) + os.pathsep + os.environ["PATH"])
            template = rulebuild.ROOT / "templates/domi.sh"
            subprocess.run(["bash", str(template)], cwd=root, env=env, check=True)
            self.assertEqual({p.name for p in package.iterdir()},
                             {"first.json", "first.srs", "second.json", "second.srs"})
            for name in ("first", "second"):
                self.assertEqual((build / f"{name}.json").read_bytes(),
                                 (package / f"{name}.json").read_bytes())
                self.assertGreater((package / f"{name}.srs").stat().st_size, 0)

    def test_domi_template_reports_missing_config_and_empty_results(self):
        for has_config in (False, True):
            with self.subTest(has_config=has_config), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                build, package, binaries = root / "build", root / "pkg", root / "bin"
                for directory in (build, package, binaries):
                    directory.mkdir()
                if has_config:
                    (build / "domi.toml").write_text("# empty fixture\n")
                domi = binaries / "domi-cli"
                domi.write_text("#!/bin/sh\nexit 0\n")
                domi.chmod(0o755)
                env = dict(os.environ, BUILDDIR=str(build), PKGDIR=str(package),
                           PATH=str(binaries) + os.pathsep + os.environ["PATH"])
                result = subprocess.run(["bash", str(rulebuild.ROOT / "templates/domi.sh")],
                                        env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no JSON" if has_config else "missing", result.stderr)
                self.assertEqual(list(package.iterdir()), [])

    def test_rename_for_local_and_remote_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, srcdir = root / "package", root / "src"
            package.mkdir()
            srcdir.mkdir()
            (package / "original.json").write_bytes(b"{}")
            digest = rulebuild.sha256(package / "original.json")
            sources = {
                "local": {"src": "original.json", "rename": "local rules.json", "sha256": digest},
                "remote": {"src": "https://example.test/rules", "rename": "remote.json", "sha256": digest},
                "unchanged": {"src": "original.json", "sha256": "SKIP"},
            }
            with patch.object(rulebuild.urllib.request, "urlopen", return_value=io.BytesIO(b"{}")):
                records = rulebuild.fetch_sources("sample", package, sources, srcdir, {})
            self.assertEqual({p.name for p in srcdir.iterdir()}, {"local rules.json", "remote.json", "unchanged"})
            self.assertEqual((package / "original.json").read_bytes(), b"{}")
            self.assertEqual(records[1]["filename"], "remote.json")
            self.assertEqual(records[1]["sha256"], digest)

    def test_rename_rejects_paths_and_collisions(self):
        for name in ("", ".", "..", "../rules.json", "/tmp/rules.json", "nested/rules.json", "a\\b", 42):
            with self.subTest(name=name), self.assertRaises(rulebuild.BuildError):
                rulebuild.source_filenames({"input": {"rename": name}})
        with self.assertRaisesRegex(rulebuild.BuildError, "duplicate"):
            rulebuild.source_filenames({"first": {"rename": "second"}, "second": {}})

    @unittest.skipUnless(shutil.which("jq") and shutil.which("sing-box"), "requires jq and sing-box")
    def test_json_template_maps_ip_domains_and_ports(self):
        cases = [
            ({"data": [{"raddr": "192.0.2.1"}, {"raddr": "192.0.2.1"}]},
             {"select": ".data[].raddr", "target": "ip_cidr", "transform": '. + "/32"', "output": "l4d2-rpglist"},
             {"ip_cidr": ["192.0.2.1/32"]}),
            ({"domains": ["z.example", "a.example", "z.example"]},
             {"select": ".domains[]", "target": "domain_suffix"},
             {"domain_suffix": ["a.example", "z.example"]}),
            ([443, 80, 443], {"select": ".[]", "target": "port"}, {"port": [80, 443]}),
        ]
        for payload, parameters, expected in cases:
            with self.subTest(parameters=parameters), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package, stage, workspace = root / "package", root / "stage", root / "workspace"
                for directory in (package, stage, workspace):
                    directory.mkdir()
                (package / "input.json").write_text(json.dumps(payload))
                data = {
                    "source": {"list": {"src": "input.json", "rename": "renamed input.json", "sha256": "SKIP"}},
                    "build": {"type": "json", "source": "list", **parameters},
                }
                rulebuild.build_package("sample", package, data, stage, workspace, {})
                basename = parameters.get("output", "sample")
                document = json.loads((stage / "sample" / f"{basename}.json").read_text())
                self.assertEqual(document, {"version": 3, "rules": [expected]})
                self.assertGreater((stage / "sample" / f"{basename}.srs").stat().st_size, 0)

    @unittest.skipUnless(shutil.which("jq"), "requires jq")
    def test_json_template_rejects_invalid_extractions_before_packaging(self):
        for expression in (".missing", ".domains", ".domains[] | empty", "invalid("):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package, stage, workspace = root / "package", root / "stage", root / "workspace"
                for directory in (package, stage, workspace):
                    directory.mkdir()
                (package / "input.json").write_text('{"domains": ["example.com"]}')
                data = {
                    "source": {"list": {"src": "input.json", "sha256": "SKIP"}},
                    "build": {"type": "json", "source": "list", "select": expression, "target": "domain"},
                }
                with self.assertRaises(rulebuild.BuildError):
                    rulebuild.build_package("sample", package, data, stage, workspace, {})
                self.assertFalse((stage / "sample").exists())

    def test_repository_recipes_validate(self):
        packages = rulebuild.discover()
        self.assertTrue(packages)
        self.assertEqual(set(rulebuild.order_packages(packages)), set(packages))

    def test_publish_config_requires_flat_ip_or_domain_targets(self):
        invalid = (
            None, "domain", {}, {"target": "other"}, {"target": "domain/subdir"},
            {"target": ["ip"]}, {"target": "domain", "files": []},
            {"target": "domain", "files": {"nested/file.srs": "ip"}},
            {"target": "domain", "files": {"file.srs": "other"}},
        )
        for publish in invalid:
            with self.subTest(publish=publish), self.assertRaises(rulebuild.BuildError):
                rulebuild.check_publish(publish, "sample.publish")
        for target in ("ip", "domain"):
            rulebuild.check_publish({"target": target, "files": {"file.srs": "ip"}}, "sample.publish")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "rules" / "a"
            package.mkdir(parents=True)
            manifest = package / "build.toml"
            recipe = 'name = "a"\ndescription = "A"\n[build]\ntype = "self"\ncommand = ["true"]\n'
            manifest.write_text(recipe)
            with self.assertRaisesRegex(rulebuild.BuildError, r"a.publish: \[publish\] is required"):
                rulebuild.discover(root)
            manifest.write_text(recipe.replace('[build]', '[publish]\ntarget = "ip/subdir"\n[build]'))
            with self.assertRaisesRegex(rulebuild.BuildError, "publish.target"):
                rulebuild.discover(root)

    def test_flat_publication_routes_files_and_preserves_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = {}
            for name in ("a", "b", "stage"):
                package = root / name
                package.mkdir()
                packages[name] = (package, {})
            (root / "a/input.txt").write_text("from a\n")
            packages["a"][1].update({
                "publish": {"target": "domain", "files": {"addresses.txt": "ip"}},
                "source": {"input": {"src": "input.txt", "sha256": "SKIP"}},
                "build": {"type": "self", "command": [
                    'mkdir -p "$PKGDIR/nested"',
                    'cp "$SRCDIR/input" "$PKGDIR/nested/shared.txt"',
                    'echo "192.0.2.1/32" > "$PKGDIR/nested/addresses.txt"',
                ]},
            })
            packages["b"][1].update({
                "publish": {"target": "ip"}, "depends": ["a"],
                "build": {"type": "self", "command": [
                    'echo "from b" > "$PKGDIR/shared.txt"',
                    'cp "$DEPSDIR/a/nested/shared.txt" "$PKGDIR/dependency.txt"',
                ]},
            })
            packages["stage"][1].update({
                "publish": {"target": "domain"},
                "build": {"type": "self", "command": ['echo "stage fixture" > "$PKGDIR/stage.txt"']},
            })
            output = root / "dist"
            args = argparse.Namespace(output=output, lock=None)
            log = io.StringIO()
            with contextlib.redirect_stderr(log):
                rulebuild.build_all(args, packages, rulebuild.order_packages(packages))
            expected = {"domain/shared.txt", "domain/stage.txt", "ip/shared.txt", "ip/addresses.txt", "ip/dependency.txt"}
            self.assertEqual({path.name for path in output.iterdir()},
                             {"ip", "domain", "index.json", "sources.lock.json"})
            self.assertEqual({path.relative_to(output).as_posix()
                              for target in ("ip", "domain") for path in (output / target).iterdir()}, expected)
            self.assertTrue(all(path.is_file() for target in ("ip", "domain") for path in (output / target).iterdir()))
            self.assertEqual((output / "ip/dependency.txt").read_text(), "from a\n")
            self.assertEqual((output / "domain/shared.txt").read_text(), "from a\n")
            self.assertEqual((output / "ip/shared.txt").read_text(), "from b\n")
            self.assertNotIn("[WARNING]", log.getvalue())
            artifacts = json.loads((output / "index.json").read_text())["artifacts"]
            self.assertEqual({artifact["path"] for artifact in artifacts}, expected)
            for artifact in artifacts:
                path = output / artifact["path"]
                self.assertEqual(artifact["sha256"], rulebuild.sha256(path))
                self.assertEqual(artifact["size"], path.stat().st_size)
            sources = json.loads((output / "sources.lock.json").read_text())["sources"]
            self.assertEqual(sources[0]["package"], "a")

    def test_flat_publication_errors_on_collisions_and_preserves_previous_output(self):
        for target in ("ip", "domain"):
            for same_package in (False, True):
                with self.subTest(target=target, same_package=same_package), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    packages = {}
                    for name in ("a", "b", "c"):
                        package = root / name
                        package.mkdir()
                        commands = ['mkdir -p "$PKGDIR/nested"', f'echo "from {name}" > "$PKGDIR/nested/shared.txt"']
                        if same_package and name == "a":
                            commands.append('echo "from a again" > "$PKGDIR/shared.txt"')
                        packages[name] = (package, {
                            "publish": {"target": target},
                            "build": {"type": "self", "command": commands},
                        })
                    output = root / "dist"
                    (output / target).mkdir(parents=True)
                    (output / target / "shared.txt").write_text("previous output\n")
                    (output / "index.json").write_text('{"schema": 1, "artifacts": []}\n')
                    (output / "sources.lock.json").write_text('{"schema": 1, "sources": []}\n')
                    previous_files = {path.relative_to(output).as_posix(): path.read_bytes()
                                      for path in output.rglob("*") if path.is_file()}
                    log = io.StringIO()
                    with (contextlib.redirect_stderr(log),
                          patch.object(rulebuild, "discover", return_value=packages),
                          patch.object(rulebuild, "DEBUG_ENABLED", False),
                          patch.object(sys, "argv", ["rulebuild", "build", "--output", str(output)])):
                        result = rulebuild.main()
                    self.assertEqual(result, 1)
                    current = "a" if same_package else "b"
                    source = "a/shared.txt" if same_package else "b/nested/shared.txt"
                    self.assertIn(f"[ERROR] {target}/shared.txt: filename collision: "
                                  f"package {current} ({source}) conflicts with package a", log.getvalue())
                    self.assertNotIn("[WARNING]", log.getvalue())
                    self.assertNotIn(f"[INFO] Building {'b' if same_package else 'c'}", log.getvalue())
                    self.assertEqual({path.relative_to(output).as_posix(): path.read_bytes()
                                      for path in output.rglob("*") if path.is_file()}, previous_files)

    def test_failed_build_preserves_previous_flat_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "dist"
            (output / "domain").mkdir(parents=True)
            (output / "domain/previous.txt").write_text("previous output\n")
            packages = {}
            for name, commands in (("a", ['echo "new output" > "$PKGDIR/new.txt"']), ("b", ["exit 7"])):
                package = root / name
                package.mkdir()
                packages[name] = (package, {
                    "publish": {"target": "domain"},
                    "build": {"type": "self", "command": commands},
                })
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(rulebuild.BuildError, "b: build: failed"):
                rulebuild.build_all(argparse.Namespace(output=output, lock=None), packages, ["a", "b"])
            self.assertEqual({path.relative_to(output).as_posix() for path in output.rglob("*")},
                             {"domain", "domain/previous.txt"})
            self.assertEqual((output / "domain/previous.txt").read_text(), "previous output\n")

    @unittest.skipUnless(shutil.which("jq") and shutil.which("sing-box"), "requires jq and sing-box")
    def test_adguard_and_awavenue_publish_distinct_names(self):
        recipes = rulebuild.discover()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = {}
            for name in ("adguard", "awavenue"):
                package = root / name
                package.mkdir()
                (package / "filter.txt").write_text(f"[Adblock Plus 2.0]\n||{name}.example^\n")
                packages[name] = (package, {
                    **recipes[name][1], "source": {"filter": {"src": "filter.txt", "sha256": "SKIP"}},
                })
            output = root / "dist"
            log = io.StringIO()
            with contextlib.redirect_stderr(log):
                rulebuild.build_all(argparse.Namespace(output=output, lock=None), packages, ["adguard", "awavenue"])
            self.assertEqual({path.name for path in (output / "domain").iterdir()},
                             {"adguard-dns-filter.txt", "adguard-dns-filter.srs", "awavenue-ads-rule.txt", "awavenue-ads-rule.srs"})
            self.assertNotIn("[WARNING]", log.getvalue())
            for name, basename in (("adguard", "adguard-dns-filter"), ("awavenue", "awavenue-ads-rule")):
                self.assertIn(f"||{name}.example^", (output / f"domain/{basename}.txt").read_text())
                self.assertGreater((output / f"domain/{basename}.srs").stat().st_size, 0)

    def test_isolated_packages_and_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_a = root / "a"
            source_b = root / "b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "input.txt").write_text("from a\n")
            first = {
                "name": "a", "source": {"input": {"src": "input.txt", "sha256": "SKIP"}},
                "prepare": {"command": ['cp "$SRCDIR/input" "$BUILDDIR/prepared.txt"']},
                "build": {"type": "self", "command": ['cp "$BUILDDIR/prepared.txt" "$PKGDIR/a.txt"']},
                "beyond": {"command": ['test -s "$PKGDIR/a.txt"']},
            }
            second = {
                "name": "b", "depends": ["a"],
                "build": {"type": "self", "command": [
                    'test ! -e "$SRCDIR/input"',
                    'test ! -e "$PKGDIR/a.txt"',
                    'cp "$DEPSDIR/a/a.txt" "$PKGDIR/b.txt"',
                ]},
            }
            stage, workspace = root / "stage", root / "workspace"
            stage.mkdir()
            workspace.mkdir()
            records, _ = rulebuild.build_package("a", source_a, first, stage, workspace, {})
            _, artifacts = rulebuild.build_package("b", source_b, second, stage, workspace, {})
            self.assertEqual((stage / "b/b.txt").read_text(), "from a\n")
            self.assertEqual(artifacts[0]["path"], "b/b.txt")
            self.assertEqual(len(records[0]["sha256"]), 64)

    def test_bad_checksum_fails_before_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "pkg"
            package.mkdir()
            (package / "input").write_text("content")
            stage, workspace = root / "stage", root / "workspace"
            stage.mkdir()
            workspace.mkdir()
            data = {
                "source": {"input": {"src": "input", "sha256": "0" * 64}},
                "build": {"type": "self", "command": ['touch "$PKGDIR/should-not-exist"']},
            }
            with self.assertRaisesRegex(rulebuild.BuildError, "SHA-256 mismatch"):
                rulebuild.build_package("pkg", package, data, stage, workspace, {})
            self.assertFalse((workspace / "pkg/pkg/should-not-exist").exists())

    def test_template_disallows_inline_command_and_cycles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "rules" / "a").mkdir(parents=True)
            (root / "templates").mkdir()
            (root / "templates" / "shared.sh").write_text("true\n")
            (root / "rules" / "a" / "build.toml").write_text(
                'name = "a"\ndescription = "A"\n[publish]\ntarget = "domain"\n[build]\ntype = "shared"\ncommand = ["true"]\n'
            )
            with self.assertRaisesRegex(rulebuild.BuildError, "only allowed"):
                rulebuild.discover(root)
        packages = {
            "a": (Path("a"), {"depends": ["b"]}),
            "b": (Path("b"), {"depends": ["a"]}),
        }
        with self.assertRaisesRegex(rulebuild.BuildError, "cycle"):
            rulebuild.order_packages(packages)


if __name__ == "__main__":
    unittest.main()
