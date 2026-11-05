"""Portable Actions bootstrap. Copy with harbor-evaluate.yml to an execution repo."""

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Keep authorization headers on the original HTTPS endpoint only.

        Returns:
            None, preventing automatic redirects.
        """
        return None


def request_studio(path, payload=None, download=None):
    """Authenticate to Studio using a fresh GitHub OIDC token.

    Args:
        path: Studio API path, relative to this run's worker endpoints.
        payload: Optional JSON progress body.
        download: Optional destination for a bounded bundle download.

    Returns:
        Decoded JSON, or the bundle SHA-256 when downloading.

    Raises:
        RuntimeError: If the response, download size or identity is invalid.
    """
    origin = os.environ["STUDIO_URL"].rstrip("/")
    parsed = urllib.parse.urlparse(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("STUDIO_URL must be an HTTPS origin")
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(25):
        identity_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
        identity_url += "&audience=" + urllib.parse.quote(origin, safe="")
        identity_request = urllib.request.Request(
            identity_url,
            headers={
                "Authorization": "Bearer "
                + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"],
            },
        )
        try:
            with opener.open(identity_request, timeout=30) as response:
                token = json.load(response)["value"]
            request = urllib.request.Request(
                f"{origin}/api/actions/{os.environ['STUDIO_RUN_ID']}/{path}",
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            with opener.open(request, timeout=180) as response:
                if download is None:
                    return json.load(response)
                digest = hashlib.sha256()
                size = 0
                with download.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > 2 * 1024**3:
                            raise RuntimeError("Execution bundle exceeds 2 GiB")
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != response.headers.get("X-Content-SHA256"):
                    raise RuntimeError("Execution bundle checksum mismatch")
                return digest.hexdigest()
        except urllib.error.HTTPError as exc:
            if exc.code not in {425, 429, 500, 502, 503, 504, 530} or attempt == 24:
                raise RuntimeError(
                    f"Studio worker API returned HTTP {exc.code}"
                ) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 24:
                raise RuntimeError("Worker connection failed after retries") from None
        time.sleep(5)
    raise RuntimeError("Worker connection timed out")


def main():
    """Fetch a matrix manifest or bootstrap one isolated Harbor worker."""
    if sys.argv[1:] == ["manifest"]:
        manifest = request_studio("manifest")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("matrix=" + json.dumps({"include": manifest["jobs"]}) + "\n")
            output.write(f"concurrency={manifest['concurrency']}\n")
            output.write(f"runner={manifest['runner']}\n")
        return
    job_id = os.environ["STUDIO_JOB_ID"]
    document = request_studio(f"jobs/{job_id}/config")
    # Mask dynamic credentials before any dependency installer or subprocess runs.
    for name in ("api_key", "phoenix_api_key"):
        if value := document["config"].get(name):
            escaped = (
                value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            )
            print("::add-mask::" + escaped, flush=True)
    root = Path(os.environ["RUNNER_TEMP"]) / "harbor-studio"
    root.mkdir(parents=True, exist_ok=True)
    archive_path = root / "input.zip"
    request_studio(f"jobs/{job_id}/bundle", download=archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        if (
            sum(m.file_size for m in archive.infolist()) > 2 * 1024**3
            or len(archive.infolist()) > 60000
        ):
            raise RuntimeError("Expanded execution bundle exceeds limits")
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item.filename
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise RuntimeError("Unsafe execution bundle entry")
            if path.parts[0] not in {"runtime", "data"}:
                raise RuntimeError("Unexpected execution bundle entry")
            target = root / item.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
            target.chmod((item.external_attr >> 16) & 0o777 or 0o644)
    env = os.environ.copy()
    env["STUDIO_DATA_DIR"] = str(root / "data")
    env["STUDIO_RESULT_ARCHIVE"] = str(root / "results.zip")
    # Registry and checkout credentials are unnecessary in the Harbor subprocess.
    for name in ("REGISTRY_PASSWORD", "REGISTRY_USERNAME", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(name, None)
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(root / "runtime"),
            "--frozen",
            "--no-dev",
            "--python",
            "3.12",
            "python",
            "-m",
            "server.actions_worker",
        ],
        input=json.dumps(document),
        text=True,
        cwd=root / "runtime",
        env=env,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
