"""Deployment incident regressions. Docker and Git are simulated; no daemon needed."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("deploy_cpa", Path(__file__).parents[1] / "deploy_cpa.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = (Path(self.tmp.name) / "sub2pool").resolve()
        self.repo.mkdir()
        self.files = [self.repo / "compose.yaml", self.repo / "compose.recover-data.yaml"]
        for file in self.files:
            file.touch()
        self.secret = "test-secret-must-never-appear-in-console"
        self.config = {
            "name": "sub2pool",
            "services": {"app": {
                "image": deploy.IMAGE,
                "environment": {"DJANGO_SECRET_KEY": self.secret, "PINCH_DATA_DIR": "/app/data"},
                "volumes": [{"type": "volume", "source": "pinche-data", "target": "/app/data"}],
                "networks": {"default": None, "cpa": None},
                "healthcheck": {"test": ["CMD", "python", "healthcheck.py"]},
            }},
            "volumes": {"pinche-data": {"name": "sub2pool_sub2pool-data", "external": True}},
            "networks": {"default": {"name": "sub2pool_default"}, "cpa": {"name": "cpa_cpa", "external": True}},
        }
        self.old = {
            "Id": "old-id", "Image": "sha256:old-image",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "sub2pool",
                    "com.docker.compose.project.working_dir": str(self.repo),
                    "com.docker.compose.project.config_files": ",".join(map(str, self.files)),
                    "com.docker.compose.oneoff": "False",
                },
                "Env": ["DJANGO_SECRET_KEY=" + self.secret, "PINCH_DATA_DIR=/app/data"],
            },
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "HostConfig": {"NetworkMode": "sub2pool_default"},
            "Mounts": [{"Type": "volume", "Name": "sub2pool_sub2pool-data", "Destination": "/app/data", "RW": True}],
            "NetworkSettings": {"Networks": {"sub2pool_default": {}, "cpa_cpa": {}}},
        }
        self.commands = []
        self.new = None
        self.failure = None
        self.on_pull = lambda: None
        self.on_build = lambda: None
        self.on_up = lambda: None
        self.containers = [self.old]
        self.other_data_user = False

    def fake_run(self, *args, capture=True):
        self.commands.append(args)
        if args[:3] == ("git", "branch", "--show-current"):
            return "cpa"
        if args[:2] == ("git", "pull"):
            if self.failure == "pull":
                raise deploy.DeploymentError("pull failed")
            self.on_pull()
            return ""
        if args[:3] == ("docker", "compose", "version") or args == ("docker", "info"):
            return "OK"
        if args[:3] == ("docker", "ps", "-aq"):
            return "\n".join(c["Id"] for c in self.containers)
        if args[:4] == ("docker", "ps", "-q", "--no-trunc"):
            return "old-id\nother-writer" if self.other_data_user else "old-id"
        if args[:2] == ("docker", "inspect"):
            lookup = {c["Id"]: c for c in self.containers}
            if self.new:
                lookup[self.new["Id"]] = self.new
            return json.dumps([lookup[i] for i in args[2:]])
        if args[:3] == ("docker", "image", "tag"):
            return ""
        if args[:2] == ("docker", "build"):
            if self.failure == "build":
                raise deploy.DeploymentError("build failed")
            self.on_build()
            return ""
        if args[:2] == ("docker", "stop"):
            self.old["State"]["Running"] = False
            if self.failure == "stop":
                raise deploy.DeploymentError("stop response lost")
            return "old-id"
        if args[:2] == ("docker", "start"):
            self.old["State"]["Running"] = True
            return "old-id"
        if args[:2] == ("docker", "cp"):
            if self.failure == "interrupt":
                raise KeyboardInterrupt("interrupted backup")
            if self.failure == "backup":
                raise deploy.DeploymentError("disk full")
            dest = Path(args[-1])
            if self.failure == "corrupt":
                (dest / "pinche.sqlite3").write_bytes(b"not a database")
            elif self.failure != "missing_db":
                with contextlib.closing(sqlite3.connect(dest / "pinche.sqlite3")) as db:
                    db.execute("CREATE TABLE events (id INTEGER)")
                    db.execute("INSERT INTO events VALUES (5836)")
                    db.commit()
                (dest / "cpa-spool").mkdir()
                (dest / "cpa-spool" / "pending.json").write_text('{"pending": true}')
            return ""
        if args[:2] == ("docker", "compose"):
            if "config" in args:
                return json.dumps(self.config)
            if "ps" in args:
                return (self.new or self.old)["Id"] if "-q" in args else "healthy"
            if "up" in args:
                self.assertTrue(list(self.repo.parent.glob("sub2pool-backups/*/BACKUP_COMPLETE")))
                if self.failure == "up":
                    raise deploy.DeploymentError("health timeout")
                self.new = copy.deepcopy(self.old)
                self.new["Id"] = "new-id"
                self.new["State"]["Running"] = True
                self.on_up()
                return ""
        self.fail_test(f"Unexpected command: {args}")

    def fail_test(self, message):
        raise AssertionError(message)

    def execute(self, options=None, check_only=False):
        output = io.StringIO()
        with patch.object(deploy, "run", side_effect=self.fake_run), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = deploy.deploy(self.repo, options or [], check_only=check_only)
        self.assertNotIn(self.secret, output.getvalue())
        return result, output.getvalue()

    def operations(self):
        return [args[1] for args in self.commands if args[0] == "docker"]

    def assert_service_untouched(self):
        self.assertNotIn("stop", self.operations())
        self.assertFalse(any("up" in c for c in self.commands))

    def test_recovered_volume_and_shared_network_survive_upgrade(self):
        result, output = self.execute()
        self.assertEqual(result, 0, output)
        ops = self.operations()
        self.assertLess(ops.index("build"), ops.index("stop"))
        self.assertLess(ops.index("stop"), ops.index("cp"))
        up = next(c for c in self.commands if "up" in c)
        self.assertIn(str(self.files[1]), up)
        self.assertEqual(up[-1], "app")
        for flag in ("--no-deps", "--no-build", "--wait"):
            self.assertIn(flag, up)
        self.assertFalse(any("down" in c or "prune" in c for c in self.commands))
        self.assertNotIn("start", ops)
        backup = next(self.repo.parent.glob("sub2pool-backups/*"))
        self.assertTrue((backup / "data/cpa-spool/pending.json").is_file())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / "compose.resolved.json").stat().st_mode & 0o777, 0o600)

    def test_default_source_volume_mismatch_aborts_before_pull(self):
        self.config["volumes"]["pinche-data"]["name"] = "sub2pool_pinche-data"
        result, output = self.execute()
        self.assertEqual(result, 1)
        self.assertIn("挂载", output)
        self.assert_service_untouched()
        self.assertNotIn(("git", "pull", "--ff-only"), self.commands)

    def test_check_only_does_not_pull_build_backup_or_stop(self):
        result, output = self.execute(check_only=True)
        self.assertEqual(result, 0, output)
        self.assertIn("sub2pool_sub2pool-data -> /app/data", output)
        self.assertIn("cpa_cpa", output)
        self.assertNotIn(("git", "pull", "--ff-only"), self.commands)
        self.assertNotIn("build", self.operations())
        self.assertNotIn("image", self.operations())
        self.assertFalse((self.repo.parent / "sub2pool-backups").exists())
        self.assert_service_untouched()

    def test_check_only_rejects_wrong_volume(self):
        self.config["volumes"]["pinche-data"]["name"] = "empty-volume"
        self.assertEqual(self.execute(check_only=True)[0], 1)
        self.assert_service_untouched()

    def test_another_container_using_data_source_blocks_inconsistent_backup(self):
        self.other_data_user = True
        self.assertEqual(self.execute()[0], 1)
        self.assert_service_untouched()

    def test_manually_connected_network_must_be_persisted(self):
        del self.config["services"]["app"]["networks"]["cpa"]
        result, output = self.execute()
        self.assertEqual(result, 1)
        self.assertIn("网络", output)
        self.assert_service_untouched()

    def test_secret_mismatch_fails_without_logging_either_secret(self):
        self.config["services"]["app"]["environment"]["DJANGO_SECRET_KEY"] = "different-secret"
        result, output = self.execute()
        self.assertEqual(result, 1)
        self.assertNotIn("different-secret", output)
        self.assert_service_untouched()

    def test_wrong_app_image_is_not_hidden_by_another_service(self):
        self.config["services"]["other"] = {"image": deploy.IMAGE}
        self.config["services"]["app"]["image"] = "ghcr.io/example:latest"
        self.assertEqual(self.execute()[0], 1)
        self.assert_service_untouched()

    def test_missing_original_compose_file_has_no_default_fallback(self):
        self.files[1].unlink()
        self.assertEqual(self.execute()[0], 1)
        self.assert_service_untouched()

    def test_absent_or_ambiguous_existing_deployment_is_refused(self):
        for candidates in ([], [self.old, copy.deepcopy(self.old)]):
            with self.subTest(count=len(candidates)):
                self.containers = candidates
                self.commands = []
                self.assertEqual(self.execute()[0], 1)
                self.assert_service_untouched()

    def test_custom_environment_file_is_reused(self):
        env = self.repo / ".env.production"
        env.touch()
        deploy.labels(self.old)["com.docker.compose.project.environment_file"] = str(env)
        result, output = self.execute()
        self.assertEqual(result, 0, output)
        up = next(c for c in self.commands if "up" in c)
        self.assertIn("--env-file", up)
        self.assertIn(str(env), up)

    def test_explicit_original_options_are_reused(self):
        options = ["-p", "sub2pool", "-f", str(self.files[0]), "-f", str(self.files[1])]
        result, output = self.execute(options)
        self.assertEqual(result, 0, output)
        up = next(c for c in self.commands if "up" in c)
        self.assertEqual(list(up[2:2 + len(options)]), options)

    def test_pull_and_build_failure_leave_original_running(self):
        for failure in ("pull", "build"):
            with self.subTest(failure=failure):
                self.failure = failure
                self.commands = []
                self.assertEqual(self.execute()[0], 1)
                self.assert_service_untouched()
                self.assertTrue(self.old["State"]["Running"])

    def test_changed_configuration_after_pull_is_rejected(self):
        self.on_pull = lambda: self.config["volumes"]["pinche-data"].update(name="empty-volume")
        self.assertEqual(self.execute()[0], 1)
        self.assert_service_untouched()
        self.assertNotIn("build", self.operations())

    def test_changed_configuration_during_build_is_rejected(self):
        self.on_build = lambda: self.config["services"]["app"].update(ports=[{"target": 8000, "published": "9999"}])
        self.assertEqual(self.execute()[0], 1)
        self.assert_service_untouched()

    def test_changed_container_during_build_is_rejected(self):
        self.on_build = lambda: setattr(self, "new", {"Id": "someone-else-replaced-app"})
        self.assertEqual(self.execute()[0], 1)
        self.assert_service_untouched()

    def test_backup_failure_or_invalid_db_restarts_only_original(self):
        for failure in ("stop", "backup", "interrupt", "corrupt", "missing_db"):
            with self.subTest(failure=failure):
                self.failure = failure
                self.commands = []
                self.assertEqual(self.execute()[0], 1)
                self.assertTrue(self.old["State"]["Running"])
                self.assertIn(("docker", "start", "old-id"), self.commands)
                self.assertFalse(any("up" in c for c in self.commands))
                self.assertFalse(list(self.repo.parent.glob("sub2pool-backups/*/BACKUP_COMPLETE")))

    def test_health_timeout_does_not_start_old_code_on_migrated_database(self):
        self.failure = "up"
        result, output = self.execute()
        self.assertEqual(result, 1)
        self.assertIn("不自动回滚", output)
        self.assertNotIn("start", self.operations())
        self.assertTrue(list(self.repo.parent.glob("sub2pool-backups/*/BACKUP_COMPLETE")))

    def test_post_upgrade_network_or_health_mismatch_is_failure(self):
        def drop_network():
            self.new["NetworkSettings"]["Networks"].pop("cpa_cpa")
        self.on_up = drop_network
        self.assertEqual(self.execute()[0], 1)
        self.assertNotIn("start", self.operations())
        self.new = None
        self.old["State"]["Running"] = True
        self.on_up = lambda: self.new["State"]["Health"].update(Status="unhealthy")
        self.assertEqual(self.execute()[0], 1)

    def test_bind_mount_source_is_preserved(self):
        self.old["Mounts"] = [{"Type": "bind", "Source": "/srv/existing-data", "Destination": "/app/data", "RW": True}]
        self.config["services"]["app"]["volumes"] = [{"type": "bind", "source": "/srv/existing-data", "target": "/app/data"}]
        result, output = self.execute()
        self.assertEqual(result, 0, output)

    def test_wal_backup_includes_committed_rows(self):
        source = self.repo / "source"
        source.mkdir()
        db = sqlite3.connect(source / "pinche.sqlite3")
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE events (id INTEGER)")
        db.execute("INSERT INTO events VALUES (5836)")
        db.commit()
        dest = self.repo / "copy"
        dest.mkdir()
        for path in source.glob("pinche.sqlite3*"):
            (dest / path.name).write_bytes(path.read_bytes())
        deploy.verify_backup(dest)
        with contextlib.closing(sqlite3.connect(dest / "pinche.sqlite3")) as copy_db:
            self.assertEqual(copy_db.execute("SELECT id FROM events").fetchall(), [(5836,)])


if __name__ == "__main__":
    unittest.main()
