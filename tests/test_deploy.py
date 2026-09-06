import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

SPEC = importlib.util.spec_from_file_location("deploy", Path(__file__).parents[1] / "scripts/deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def binding(private=False):
    return {"token": "rbd_test-secret", "workspace_id": "ws-test", "repository": "acme/app",
            "source_commit": "a" * 40, "repository_private": private}


def finished(success=True):
    return {"event": "deploy_finished", "workspace": {"id": "ws-test", "name": "prod", "fresh": False},
            "apps": [{"name": "app", "slug": "app", "url": "https://app-test.rigbox.dev",
                      "outcome": {"status": "healthy" if success else "restart_failed"}}],
            "failures": 0 if success else 1, "success": success, "error": None, "duration_ms": 1234}


def outputs(path):
    result = {}
    lines = path.read_text().splitlines()
    while lines:
        key, delimiter = lines.pop(0).split("<<")
        end = lines.index(delimiter)
        result[key] = "\n".join(lines[:end])
        lines = lines[end + 1:]
    return result


class ActionTests(unittest.TestCase):
    def test_missing_oidc_permission_explains_workflow_fix_before_network(self):
        with patch.object(deploy, "request_json") as request:
            with self.assertRaisesRegex(deploy.ActionError, "id-token: write"):
                deploy.oidc_token({})
            request.assert_not_called()

    def test_oidc_explicit_audience_replaces_default_and_token_is_masked(self):
        env = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc?a=1&audience=wrong",
               "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret"}
        with patch.object(deploy, "request_json", return_value={"value": "jwt-secret"}) as request:
            with contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(deploy.oidc_token(env), "jwt-secret")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query["audience"], ["https://api.rigbox.dev"])
        self.assertEqual(query["a"], ["1"])
        self.assertEqual(log.getvalue(), "::add-mask::jwt-secret\n")

    def test_exchange_posts_jwt_and_masks_deploy_token(self):
        env = {"GITHUB_REPOSITORY": "Acme/App", "GITHUB_SHA": "a" * 40}
        with patch.object(deploy, "request_json", return_value=binding()) as request:
            with contextlib.redirect_stdout(io.StringIO()) as log:
                deploy.exchange("https://api.rigbox.dev", "jwt-secret", env)
        request.assert_called_once_with("https://api.rigbox.dev/v1/auth/github/token", body={"token": "jwt-secret"})
        self.assertEqual(log.getvalue(), "::add-mask::rbd_test-secret\n")

    def test_exchange_rejects_wrong_commit_repository_or_missing_visibility(self):
        env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_SHA": "a" * 40}
        for fields in [{"source_commit": "b" * 40}, {"repository": "other/app"}, {"repository_private": None}]:
            with patch.object(deploy, "request_json", return_value={**binding(), **fields}), patch.object(deploy, "mask"):
                with self.assertRaises(deploy.ActionError):
                    deploy.exchange("https://api.rigbox.dev", "jwt", env)

    def test_public_deploy_does_not_receive_git_or_oidc_request_credentials(self):
        env = {"ACTION_GITHUB_TOKEN": "github-secret", "RIG_GIT_TOKEN": "stale-secret",
               "RIG_GIT_PRIVATE": "true", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-request"}
        child = deploy.deploy_environment(binding(), env)
        self.assertEqual(child["RIG_API_KEY"], "rbd_test-secret")
        for key in env:
            self.assertNotIn(key, child)

    def test_private_deploy_receives_only_job_token_for_git(self):
        with patch.object(deploy, "mask") as mask:
            child = deploy.deploy_environment(binding(True), {"ACTION_GITHUB_TOKEN": "job-secret"})
        self.assertEqual(child["RIG_GIT_PRIVATE"], "true")
        self.assertEqual(child["RIG_GIT_TOKEN"], "job-secret")
        self.assertNotIn("ACTION_GITHUB_TOKEN", child)
        mask.assert_called_once_with("job-secret")

    def test_private_deploy_requires_job_token(self):
        with self.assertRaisesRegex(deploy.ActionError, "contents: read"):
            deploy.deploy_environment(binding(True), {})

    def test_invalid_reimage_input_fails_before_network(self):
        for value in ["yes", "1", "TRUE", ""]:
            with patch.object(deploy, "oidc_token") as oidc:
                with self.assertRaisesRegex(deploy.ActionError, "reimage input must be true or false"):
                    deploy.main({"ACTION_REIMAGE": value})
                oidc.assert_not_called()

    def test_old_prerelease_and_invalid_versions_are_rejected(self):
        for version in ["v0.12.63", "0.12.63", "v0.12.64-rc.1", "../main", "latest"]:
            with self.assertRaises(deploy.ActionError):
                deploy.release_version(version)
        self.assertEqual(deploy.release_version("0.12.64"), "v0.12.64")

    def test_actual_asset_names_and_required_github_digest(self):
        asset = {"name": "rigbox-linux-amd64", "digest": "sha256:" + "a" * 64,
                 "browser_download_url": "https://github.com/rigbox-dev/cli-artifacts/releases/download/v0.12.64/rigbox-linux-amd64"}
        release = {"tag_name": "v0.12.64", "assets": [asset]}
        self.assertEqual(deploy.select_asset(release, "Linux", "x86_64"), ("v0.12.64", asset))
        with self.assertRaisesRegex(deploy.ActionError, "rigbox-linux-arm64.*not published"):
            deploy.select_asset(release, "Linux", "aarch64")
        for digest in [None, "", "md5:abcdef"]:
            with self.assertRaisesRegex(deploy.ActionError, "SHA256"):
                deploy.select_asset({**release, "assets": [{**asset, "digest": digest}]}, "Linux", "x86_64")

    def test_checksum_mismatch_never_makes_binary_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rig"
            asset = {"browser_download_url": "https://example.test/rig", "digest": "sha256:" + "0" * 64}
            with patch.object(deploy.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"binary")
                with self.assertRaisesRegex(deploy.ActionError, "checksum mismatch"):
                    deploy.download_verified(asset, path)
            self.assertFalse(path.stat().st_mode & 0o111)
            asset["digest"] = "sha256:" + hashlib.sha256(b"binary").hexdigest()
            with patch.object(deploy.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"binary")
                deploy.download_verified(asset, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_credential_requests_reject_redirects(self):
        with self.assertRaises(deploy.ActionError):
            deploy.JsonRedirects().redirect_request(None, None, 302, "", {}, "https://other.test")
        with self.assertRaises(deploy.ActionError):
            deploy.HttpsRedirects().redirect_request(None, None, 302, "", {}, "http://other.test")

    def test_partial_failure_outputs_and_exit_code_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            event = finished(False)
            self.assertEqual(deploy.publish_outputs(event, 7, binding(), path), 7)
            result = outputs(path)
            self.assertEqual(result["success"], "false")
            self.assertEqual(result["exit-code"], "7")
            self.assertEqual(result["failures"], "1")
            self.assertEqual(json.loads(result["result"]), event)
            self.assertEqual(json.loads(result["apps"])[0]["outcome"]["status"], "restart_failed")

    def test_invalid_or_wrong_workspace_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            for event in [None, {}, {**finished(), "workspace": {"id": "ws-other"}},
                          {**finished(), "workspace": None}, {**finished(), "apps": ["bad"]}]:
                with self.assertRaises(deploy.ActionError):
                    deploy.publish_outputs(event, 0, binding(), path)

    def test_early_cli_failure_keeps_exit_code_and_available_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            env = {"GITHUB_OUTPUT": str(path), "RUNNER_TEMP": directory}
            with patch.object(deploy, "oidc_token", return_value="jwt"), \
                 patch.object(deploy, "exchange", return_value=binding()), \
                 patch.object(deploy, "install_cli", return_value=Path(directory) / "rig"), \
                 patch.object(deploy, "run_deploy", return_value=(23, None)), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(deploy.main(env), 23)
            self.assertEqual(outputs(path), {"workspace-id": "ws-test", "success": "false", "exit-code": "23"})

    def test_inconsistent_terminal_success_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            self.assertEqual(deploy.publish_outputs({**finished(), "failures": 1}, 0, binding(), path), 1)
            self.assertEqual(outputs(path)["success"], "false")

    def test_output_delimiter_prevents_newline_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            value = "https://app.test\nsuccess=true\nEOF"
            deploy.write_outputs({"url": value}, path)
            self.assertEqual(outputs(path), {"url": value})

    def test_real_subprocess_uses_bound_workspace_and_captures_terminal_event(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "rig"
            capture = Path(directory) / "argv.json"
            event = finished(False)
            binary.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                              "open(os.environ['TEST_CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n"
                              "print('::warning::untrusted log')\n"
                              f"print({json.dumps(json.dumps(event))})\nsys.exit(9)\n")
            binary.chmod(0o700)
            env = {**os.environ, "TEST_CAPTURE": str(capture), "ACTION_WORKING_DIRECTORY": directory}
            with contextlib.redirect_stdout(io.StringIO()) as log:
                code, parsed = deploy.run_deploy(binary, binding(), env)
            self.assertEqual(code, 9)
            self.assertEqual(parsed, event)
            self.assertEqual(json.loads(capture.read_text()), ["deploy", "--workspace", "ws-test", "--output", "json"])
            self.assertTrue(log.getvalue().startswith("::stop-commands::"))
            for consent in ["false", "true"]:
                with contextlib.redirect_stdout(io.StringIO()):
                    deploy.run_deploy(binary, binding(), {**env, "ACTION_REIMAGE": consent})
                expected = ["deploy", "--workspace", "ws-test", "--output", "json"]
                if consent == "true":
                    expected.append("--reimage")
                self.assertEqual(json.loads(capture.read_text()), expected)


if __name__ == "__main__":
    unittest.main()
