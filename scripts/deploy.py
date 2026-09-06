#!/usr/bin/env python3
"""Composite-action entry point; Python standard library only."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

ARTIFACT_REPO = "rigbox-dev/cli-artifacts"
MIN_VERSION = (0, 12, 64)


class ActionError(Exception):
    pass


def command_data(value):
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def mask(value):
    if value:
        print(f"::add-mask::{command_data(value)}", flush=True)


def https_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ActionError("URLs must use HTTPS without embedded credentials.")
    return parsed


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        https_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


class JsonRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ActionError("A JSON endpoint redirected; refusing to forward credentials.")


def request_json(url, token=None, body=None):
    https_url(url)
    headers = {"Accept": "application/json", "User-Agent": "rigbox-deploy-action"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.build_opener(JsonRedirects()).open(request, timeout=30) as response:
            value = json.load(response)
            if not isinstance(value, dict):
                raise ActionError("The server returned an unexpected JSON response.")
            return value
    except urllib.error.HTTPError as error:
        raise ActionError(f"Request to {urllib.parse.urlsplit(url).hostname} failed (HTTP {error.code}).") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise ActionError("The server could not be reached or returned invalid JSON.") from None


def oidc_token(environ):
    url = environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise ActionError("GitHub OIDC is unavailable. Add this to your workflow job:\npermissions:\n  contents: read\n  id-token: write")
    parsed = https_url(url)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if key != "audience"]
    query.append(("audience", environ.get("ACTION_AUDIENCE", "https://api.rigbox.dev")))
    url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    token = request_json(url, bearer).get("value")
    if not isinstance(token, str) or not token:
        raise ActionError("GitHub did not return an OIDC token.")
    mask(token)
    return token


def exchange(api_url, token, environ):
    parsed = https_url(api_url)
    if parsed.query or parsed.fragment:
        raise ActionError("api-url must not contain a query or fragment.")
    binding = request_json(api_url.rstrip("/") + "/v1/auth/github/token", body={"token": token})
    deploy_token = binding.get("token")
    if isinstance(deploy_token, str):
        mask(deploy_token)
    if not isinstance(deploy_token, str) or not deploy_token.startswith("rbd_"):
        raise ActionError("Rigbox did not return a deployment credential.")
    if not re.fullmatch(r"ws-[a-z0-9]+", str(binding.get("workspace_id", ""))):
        raise ActionError("Rigbox did not return a valid bound workspace.")
    if type(binding.get("repository_private")) is not bool:
        raise ActionError("Rigbox did not report repository visibility.")
    if not isinstance(binding.get("repository"), str) or binding["repository"].lower() != environ.get("GITHUB_REPOSITORY", "").lower():
        raise ActionError("The binding does not match this GitHub repository.")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(binding.get("source_commit", ""))):
        raise ActionError("Rigbox did not return a full source commit.")
    if binding["source_commit"] != environ.get("GITHUB_SHA", "").lower():
        raise ActionError("The binding commit does not match GITHUB_SHA.")
    return binding


def release_version(tag):
    if not isinstance(tag, str):
        raise ActionError("GitHub did not return a valid CLI release version.")
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not match or tuple(map(int, match.groups())) < MIN_VERSION:
        raise ActionError("This Action requires a stable Rigbox CLI release v0.12.64 or newer; earlier releases lack CI deploy output.")
    return "v" + ".".join(str(int(part)) for part in match.groups())


def select_asset(release, system, machine):
    tag = release_version(release.get("tag_name", ""))
    if release.get("draft") or release.get("prerelease"):
        raise ActionError("Use a published stable CLI release.")
    os_name = {"Linux": "linux", "Darwin": "darwin"}.get(system)
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not os_name or not arch:
        raise ActionError(f"Unsupported runner platform: {system}/{machine}. Use Linux x64 or macOS.")
    name = f"rigbox-{os_name}-{arch}"
    asset = next((item for item in release.get("assets", []) if item.get("name") == name), None)
    if asset is None:
        raise ActionError(f"CLI asset {name} is not published for {tag}. Use a Linux x64 runner or a supported macOS runner.")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
        raise ActionError(f"GitHub has no SHA256 digest for {name}; refusing an unverified binary.")
    expected_url = f"https://github.com/{ARTIFACT_REPO}/releases/download/{tag}/{name}"
    if asset.get("browser_download_url") != expected_url:
        raise ActionError("The CLI download URL does not match its release asset.")
    return tag, asset


def download_verified(asset, destination):
    request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "rigbox-deploy-action"})
    checksum = hashlib.sha256()
    try:
        with urllib.request.build_opener(HttpsRedirects()).open(request, timeout=60) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                checksum.update(chunk)
                output.write(chunk)
    except (urllib.error.URLError, TimeoutError):
        raise ActionError("The CLI release asset could not be downloaded.") from None
    if "sha256:" + checksum.hexdigest() != asset["digest"]:
        raise ActionError("CLI SHA256 checksum mismatch; refusing to execute the download.")
    destination.chmod(0o700)


def install_cli(directory, environ):
    requested = environ.get("ACTION_CLI_VERSION", "latest")
    suffix = "latest" if requested == "latest" else "tags/" + release_version(requested)
    release = request_json(f"https://api.github.com/repos/{ARTIFACT_REPO}/releases/{suffix}", environ.get("ACTION_GITHUB_TOKEN"))
    tag, asset = select_asset(release, platform.system(), platform.machine())
    if requested != "latest" and release_version(requested) != tag:
        raise ActionError("GitHub returned a different CLI release than requested.")
    binary = directory / "rig"
    download_verified(asset, binary)
    if platform.system() == "Darwin":
        subprocess.run(["xattr", "-cr", str(binary)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True, capture_output=True)
    version_env = {key: value for key, value in environ.items() if key not in (
        "ACTION_GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
        "RIG_API_KEY", "RIG_GIT_TOKEN", "RIG_GIT_PRIVATE")}
    version = subprocess.run([str(binary), "--version"], env=version_env, check=True, capture_output=True, text=True).stdout
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", version)
    if not match or release_version(match.group(1)) != tag:
        raise ActionError("The downloaded CLI version does not match the verified release.")
    print(f"Using Rigbox CLI {tag} ({asset['name']}).", flush=True)
    return binary


def deploy_environment(binding, environ):
    env = dict(environ)
    for key in ("ACTION_GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "RIG_GIT_TOKEN", "RIG_GIT_PRIVATE"):
        env.pop(key, None)
    env.update(RIG_API_KEY=binding["token"], RIG_API_URL=environ.get("ACTION_API_URL", "https://api.rigbox.dev"),
               CI="true", GITHUB_ACTIONS="true", RIGBOX_NO_UPDATE_CHECK="1")
    if binding["repository_private"]:
        token = environ.get("ACTION_GITHUB_TOKEN")
        if not token:
            raise ActionError("Private repository deployment requires the job's GitHub token and permissions: contents: read.")
        mask(token)
        env.update(RIG_GIT_PRIVATE="true", RIG_GIT_TOKEN=token)
    return env


def reimage_enabled(environ):
    value = environ.get("ACTION_REIMAGE", "false")
    if value not in ("true", "false"):
        raise ActionError("The reimage input must be true or false; true explicitly permits workspace disk replacement.")
    return value == "true"


def run_deploy(binary, binding, environ):
    env = deploy_environment(binding, environ)
    command = [str(binary), "deploy", "--workspace", binding["workspace_id"], "--output", "json"]
    if reimage_enabled(environ):
        command.append("--reimage")
    terminal = None
    marker = uuid.uuid4().hex
    print(f"::stop-commands::{marker}", flush=True)
    try:
        with subprocess.Popen(command, cwd=environ.get("ACTION_WORKING_DIRECTORY", "."), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace") as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("event") == "deploy_finished":
                    terminal = event
            code = process.wait()
    finally:
        print(f"\n::{marker}::", flush=True)
    return code if code >= 0 else 128 - code, terminal


def publish_outputs(event, code, binding, output_path):
    if not event or type(event.get("success")) is not bool or not isinstance(event.get("apps"), list):
        raise ActionError("CLI did not emit a valid deploy_finished event. Check the deploy log and CLI version.")
    workspace = event.get("workspace")
    if workspace is not None and (not isinstance(workspace, dict) or workspace.get("id") != binding["workspace_id"]):
        raise ActionError("CLI reported a workspace different from the repository binding.")
    for field in ("failures", "duration_ms"):
        if type(event.get(field)) is not int or event[field] < 0:
            raise ActionError(f"CLI deploy_finished is missing a valid {field}.")
    for app in event["apps"]:
        if not isinstance(app, dict) or not isinstance(app.get("outcome"), dict) or not isinstance(app["outcome"].get("status"), str):
            raise ActionError("CLI returned an invalid app outcome.")
        if app.get("url") is not None and not isinstance(app["url"], str):
            raise ActionError("CLI returned an invalid app URL.")
    success = event["success"] and code == 0 and event["failures"] == 0
    if success and workspace is None:
        raise ActionError("CLI reported success without a workspace.")
    values = {"workspace-id": binding["workspace_id"], "url": next((app.get("url") for app in event["apps"] if app.get("url")), ""),
              "apps": json.dumps(event["apps"], separators=(",", ":")), "success": str(success).lower(),
              "failures": str(event["failures"]), "duration-ms": str(event["duration_ms"]),
              "result": json.dumps(event, separators=(",", ":")), "exit-code": str(code)}
    write_outputs(values, output_path)
    return code or (0 if success else 1)


def write_outputs(values, output_path):
    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            delimiter = uuid.uuid4().hex
            while delimiter in value:
                delimiter = uuid.uuid4().hex
            output.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def main(environ=None):
    environ = os.environ if environ is None else environ
    reimage_enabled(environ)
    token = oidc_token(environ)
    binding = exchange(environ.get("ACTION_API_URL", "https://api.rigbox.dev"), token, environ)
    with tempfile.TemporaryDirectory(prefix="rigbox-deploy-", dir=environ.get("RUNNER_TEMP")) as temporary:
        binary = install_cli(Path(temporary), environ)
        code, event = run_deploy(binary, binding, environ)
        try:
            return publish_outputs(event, code, binding, environ["GITHUB_OUTPUT"])
        except ActionError as error:
            write_outputs({"workspace-id": binding["workspace_id"], "success": "false", "exit-code": str(code)}, environ["GITHUB_OUTPUT"])
            print(f"::error::{command_data(str(error))}", flush=True)
            return code or 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ActionError, OSError, subprocess.SubprocessError) as error:
        print(f"::error::{command_data(str(error))}", flush=True)
        sys.exit(1)
