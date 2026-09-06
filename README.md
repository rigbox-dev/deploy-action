# Deploy to Rigbox

A composite GitHub Action that exchanges GitHub OIDC for a short-lived Rigbox deployment credential and deploys the repository to its bound workspace. No Rigbox API key or SSH key needs to be stored in GitHub secrets.

Requires Rigbox CLI **v0.12.64 or newer** and GitHub OIDC deployments enabled for your Rigbox account. The Action rejects earlier CLI versions.

## Setup

Enable GitHub OIDC deployments for your Rigbox account. With the supported CLI installed, authenticate locally and bind this repository to an existing workspace:

```sh
rig login
rig ci link --repo YOUR_ORG/YOUR_REPO --workspace production
```

The initial binding has a 24-hour claim window. The first claim relies on control of the configured repository name during that window, so verify the name before linking. Run the workflow during that window, or run `rig ci link` again to renew the pending binding. The workspace comes from this server-side binding; a fresh checkout does not create a new workspace.

Every app deployed from CI must use `source.kind: git`. Add the following fields to your existing app definition in `rig.yaml`, using the repository's HTTPS URL:

```yaml
source:
  kind: git
  repo: https://github.com/YOUR_ORG/YOUR_REPO
  branch: main
reproducible: true
```

Private repositories require `reproducible: true`. The Action passes the job's GitHub token only when the OIDC exchange identifies a private repository, and the builder fetches the exact workflow commit. Public repositories clone anonymously. Application environment variables can still come from workflow `env:` through the existing manifest configuration.

The first reproducible deployment to an existing workspace may require replacing its disk to pin the workspace to the built image. After backing up data you need to keep, explicitly set `with: { reimage: "true" }` on the Action for that setup deployment. Disk replacement discards existing workspace disk contents. Remove the input after the workspace is image-pinned; the default is `false`, and the Action never enables it automatically.

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  id-token: write
concurrency:
  group: rigbox-production
  cancel-in-progress: false
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          persist-credentials: false
      - uses: rigbox-dev/deploy-action@v1
        id: deploy
```

For reproducible workflow configuration, pin the Action to a reviewed commit and set `cli-version` to a supported release rather than `latest`.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `cli-version` | `latest` | Stable CLI release, with or without `v`; minimum v0.12.64. |
| `api-url` | `https://api.rigbox.dev` | Rigbox API base URL, using HTTPS. |
| `audience` | `https://api.rigbox.dev` | Explicit GitHub OIDC audience; must match the server configuration. |
| `working-directory` | `.` | Directory containing `rig.yaml`. |
| `reimage` | `false` | `true` explicitly permits workspace disk replacement; use only when prepared to discard its existing disk contents. |

Linux x64 and macOS Intel/Apple Silicon use the corresponding raw assets from the public [CLI artifact releases](https://github.com/rigbox-dev/cli-artifacts/releases). Linux ARM fails with an actionable error until a `rigbox-linux-arm64` asset exists. Windows is unsupported. The runner needs Bash and Python 3.9 or newer; GitHub-hosted Linux and macOS runners provide them.

The installer checks the stable release version and GitHub's SHA256 asset digest before making the downloaded binary executable. Missing checksums, checksum mismatches, and unexpected download URLs fail closed. macOS binaries receive the same local ad-hoc signing step as the CLI installer.

## Outputs and failures

| Output | Meaning |
| --- | --- |
| `workspace-id` | Workspace from the repository binding. |
| `url` | First app URL reported by the CLI, or an empty string. |
| `apps` | JSON array with app names, slugs, URLs, and tagged outcomes. |
| `success` | `true` only when both the CLI result and exit status succeed. |
| `failures` | Number of failed apps. |
| `duration-ms` | CLI deployment duration in milliseconds. |
| `result` | Full `deploy_finished` JSON event. |
| `exit-code` | Original CLI exit code. |

Partial failures still populate the structured outputs and fail the Action. A failure before the terminal event sets `workspace-id`, `success`, and `exit-code`, but cannot provide a complete deployment result. A malformed result or unexpected workspace also fails. When a CLI error occurs, the Action preserves its nonzero exit code.

Use a follow-up step with `if: always()` to inspect outputs after a failed deployment:

```yaml
      - name: Inspect deployment result
        if: always()
        env:
          DEPLOY_RESULT: ${{ steps.deploy.outputs.result }}
        run: python3 -c 'import os; print(os.environ["DEPLOY_RESULT"])'
```

## Credential handling

The Action requests the configured audience explicitly, posts the GitHub ID token to `/v1/auth/github/token`, and masks the ID token and exchanged credential before running the CLI. The exchanged credential is exported as `RIG_API_KEY` only within the CLI subprocess. For private repositories, `RIG_GIT_PRIVATE=true` and `RIG_GIT_TOKEN` contain the job's ephemeral token; public deployments receive neither variable. OIDC request credentials are removed from the subprocess environment.

Credentials are not placed in command-line arguments, output files, or later-step environment files. JSON endpoint redirects are rejected rather than forwarding credentials. CLI output is streamed with GitHub workflow command interpretation disabled, and output values use distinct delimiters so app text cannot inject additional outputs.

## Development

The Action has no Python package dependencies or vendored JavaScript. Run its offline tests with:

```sh
python3 -m unittest discover -s tests -v
```

Tests cover audience selection, token masking, repository/commit binding, public/private credential separation, release versions and assets, checksum enforcement, redirect rejection, output injection, partial failures, and real subprocess invocation. End-to-end deployment testing requires a linked repository and a live OIDC-capable Rigbox deployment.
