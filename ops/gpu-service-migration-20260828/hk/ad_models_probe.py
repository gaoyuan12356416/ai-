#!/usr/bin/env python3
"""One direct read-only Codex model-catalog comparison; never logs credentials."""
import argparse
import hashlib
import html
import json
import os
import pathlib
import re
import shlex
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.request

RUN_ID = "gpu-service-migration-20260828T1502"
PROBE_BASE = pathlib.PurePosixPath("/data/migrations") / RUN_ID / "hk-access-probe"
SOURCE_AUTH = pathlib.Path("/root/.codex/auth.json")
US = "43.166.178.132"
HK = "43.154.250.89"
RELATIVE_SCRIPT = "ops/gpu-service-migration-20260828/hk/ad_models_probe.py"
MODEL = "gpt-5.5"
CLIENT_VERSION = "0.147.0"
URL = "https://chatgpt.com/backend-api/codex/models?client_version=" + CLIENT_VERSION
MAX_BODY = 2 * 1024 * 1024
KNOWN_ERRORS = {
    "invalid_token", "token_expired", "invalid_api_key", "unauthorized",
    "forbidden", "access_denied", "authentication_error", "insufficient_quota",
    "rate_limit_exceeded", "unsupported_country_region_territory",
    "unsupported_country", "country_not_supported", "unsupported_region",
    "account_deactivated", "account_disabled", "organization_deactivated",
}


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def extract_fragment(auth):
    if auth.get("auth_mode") != "chatgpt":
        raise ValueError("unexpected authentication mode")
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("missing authentication fields")
    result = {key: tokens.get(key) for key in ("access_token", "account_id")}
    if any(not isinstance(value, str) or not value or "\r" in value or "\n" in value
           for value in result.values()):
        raise ValueError("invalid authentication field")
    return result


def validate_fragment(fragment):
    if not isinstance(fragment, dict) or set(fragment) != {"access_token", "account_id"}:
        raise ValueError("probe fragment must contain only required fields")
    return extract_fragment({"auth_mode": "chatgpt", "tokens": fragment})


def safe_metadata(body, headers=None):
    """Reduce untrusted HTTP data to fixed categories; never retain raw values."""
    def header(name):
        value = headers.get(name, "") if headers is not None else ""
        return value.strip().lower() if isinstance(value, str) else ""

    media_type = header("Content-Type").split(";", 1)[0].strip()
    if media_type == "application/json" or media_type.endswith("+json"):
        content_type = "json"
    elif media_type in ("text/html", "application/xhtml+xml"):
        content_type = "html"
    elif media_type.startswith("text/"):
        content_type = "text"
    else:
        content_type = "other" if media_type else "missing"
    server = header("Server").split("/", 1)[0].strip()
    server_category = (server if server in {"cloudflare", "nginx", "envoy", "apache"}
                       else "other" if server else "missing")
    challenge = header("CF-Mitigated") == "challenge"
    # A bounded in-memory sample may identify a known page. No title is emitted.
    sample = body[:65536].decode("utf-8", errors="replace")
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", sample, re.I | re.S)
    title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip().lower() if match else ""
    is_html = content_type == "html" or bool(match)
    if challenge or (is_html and title == "just a moment..."):
        page = "cloudflare_challenge"
    elif is_html and title in {"unsupported country, region, or territory",
                               "unsupported country, region, or territory."}:
        page = "region_restriction_notice"
    elif is_html and title in {"account deactivated", "account disabled"}:
        page = "account_restriction_notice"
    elif is_html and server_category == "cloudflare" and title in {
            "attention required! | cloudflare", "sorry, you have been blocked", "access denied"}:
        page = "cloudflare_block"
    else:
        page = "unclassified_html" if is_html else "not_html"
    return {"content_type_category": content_type, "server_category": server_category,
            "cf_mitigated_challenge": challenge, "page_category": page}


def safe_result(status, body, headers=None):
    result = {"http_status": status, "safe_error_code": None,
              "target_model_visible": False}
    result.update(safe_metadata(body, headers))
    if len(body) > MAX_BODY:
        result["safe_error_code"] = "response_too_large"
        return result
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        result["safe_error_code"] = "non_json_response"
        return result
    if status == 200 and isinstance(data, dict):
        models = data.get("models")
        if not isinstance(models, list):
            result["safe_error_code"] = "unexpected_models_schema"
            return result
        result["target_model_visible"] = any(
            isinstance(item, dict) and item.get("slug", item.get("id")) == MODEL
            for item in models
        )
        if not result["target_model_visible"]:
            result["safe_error_code"] = "target_model_not_in_catalog"
        return result
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    result["safe_error_code"] = code if isinstance(code, str) and code in KNOWN_ERRORS else "unclassified_http_error"
    return result


def catalog_request(fragment, opener=None):
    fragment = validate_fragment(fragment)
    headers = {
        "Authorization": "Bearer " + fragment["access_token"],
        "ChatGPT-Account-Id": fragment["account_id"],
        "originator": "codex_exec",
        "User-Agent": "codex_exec/" + CLIENT_VERSION,
        "Accept": "application/json",
    }
    request = urllib.request.Request(URL, headers=headers, method="GET")
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RefuseRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
    try:
        with opener.open(request, timeout=20) as response:
            return safe_result(response.status, response.read(MAX_BODY + 1), response.headers)
    except urllib.error.HTTPError as error:
        try:
            return safe_result(error.code, error.read(MAX_BODY + 1), error.headers)
        finally:
            error.close()
    except (OSError, urllib.error.URLError):
        return {"http_status": None, "safe_error_code": "network_or_tls_error",
                "target_model_visible": False}


def probe_directory(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("an exact pushed SHA is required")
    directory = pathlib.Path(str(PROBE_BASE / sha))
    if directory.resolve() != directory:
        raise ValueError("probe directory contains a symbolic link")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(PROBE_BASE), 0o700)
    os.chmod(str(directory), 0o700)
    return directory


def private_json(path, value):
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(str(path), flags, 0o600), "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def remote_probe(role, sha, script_sha256):
    if hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest() != script_sha256:
        raise ValueError("probe code checksum mismatch")
    directory = probe_directory(sha)
    report = directory / ("result-" + role + ".json")
    if report.exists():
        raise ValueError("this read-only probe has already run")
    fragment_path = directory / "access-fragment.json"
    if role == "US":
        fragment = extract_fragment(json.loads(SOURCE_AUTH.read_text()))
        private_json(fragment_path, fragment)
    else:
        if fragment_path.is_symlink() or stat.S_IMODE(fragment_path.stat().st_mode) != 0o600:
            raise ValueError("probe fragment permissions are not private")
        fragment = validate_fragment(json.loads(fragment_path.read_text()))
    result = catalog_request(fragment)
    private_json(report, result)
    return result


def pushed_script(repo, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("an exact pushed SHA is required")
    subprocess.check_call(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha,
         "origin/codex/gpu-service-migration-20260828"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    blob = subprocess.check_output(["git", "-C", str(repo), "show", sha + ":" + RELATIVE_SCRIPT])
    local = pathlib.Path(__file__).read_bytes()
    if blob.replace(b"\r\n", b"\n") != local.replace(b"\r\n", b"\n"):
        raise ValueError("local probe does not match the pushed code")
    return blob


def connect(host, key, known_hosts):
    import paramiko
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(host, username="root", key_filename=str(key), allow_agent=False,
                   look_for_keys=False, timeout=20, auth_timeout=20, banner_timeout=20)
    return client


def remote_json(client, argv):
    _, stdout, stderr = client.exec_command(" ".join(shlex.quote(v) for v in argv), timeout=35)
    payload = stdout.read()
    stderr.read()  # Never emit an unreviewed exception or HTTP response.
    code = stdout.channel.recv_exit_status()
    if code:
        raise RuntimeError("remote probe setup or execution failed")
    return json.loads(payload.decode("utf-8"))


def upload_code(client, folder, blob):
    boot = (
        "import pathlib,os; p=pathlib.Path(%r); "
        "assert p.resolve()==p; p.mkdir(parents=True,exist_ok=True,mode=0o700); "
        "os.chmod(str(p.parent),0o700); os.chmod(str(p),0o700)"
    ) % folder
    _, stdout, stderr = client.exec_command("python3 -c " + shlex.quote(boot))
    stdout.read()
    stderr.read()
    if stdout.channel.recv_exit_status():
        raise RuntimeError("could not prepare protected probe directory")
    destination = folder + "/ad_models_probe.py"
    with client.open_sftp() as sftp:
        with sftp.open(destination, "wb") as output:
            sftp.chmod(destination, 0o600)
            output.write(blob)
        with sftp.open(destination, "rb") as source:
            if hashlib.sha256(source.read()).digest() != hashlib.sha256(blob).digest():
                raise RuntimeError("uploaded probe checksum mismatch")
    return destination


def remove_fragment(client, path):
    with client.open_sftp() as sftp:
        try:
            sftp.remove(path)
        except FileNotFoundError:
            pass


def compare(repo, sha, key, known_hosts):
    blob = pushed_script(repo, sha)
    checksum = hashlib.sha256(blob).hexdigest()
    folder = str(PROBE_BASE / sha)
    clients = {}
    result = {}
    try:
        clients["US"] = connect(US, key, known_hosts)
        source_script = upload_code(clients["US"], folder, blob)
        result["US"] = remote_json(clients["US"], [
            "python3", source_script, "remote", "--role", "US", "--sha", sha,
            "--script-sha256", checksum,
        ])
        # A failed control cannot establish HK readiness; do not copy its credential.
        if result["US"]["http_status"] != 200 or not result["US"]["target_model_visible"]:
            result["HK"] = {"http_status": None, "safe_error_code": "source_control_failed",
                            "target_model_visible": False}
            return result
        clients["HK"] = connect(HK, key, known_hosts)
        target_script = upload_code(clients["HK"], folder, blob)
        path = folder + "/access-fragment.json"
        with clients["US"].open_sftp() as source, clients["HK"].open_sftp() as target:
            with source.open(path, "rb") as file:
                payload = file.read(64 * 1024)
            validate_fragment(json.loads(payload.decode("utf-8")))
            with target.open(path, "wb") as file:
                target.chmod(path, 0o600)
                file.write(payload)
            with target.open(path, "rb") as file:
                if hashlib.sha256(file.read()).digest() != hashlib.sha256(payload).digest():
                    raise RuntimeError("probe fragment transfer integrity mismatch")
        result["HK"] = remote_json(clients["HK"], [
            "python3", target_script, "remote", "--role", "HK", "--sha", sha,
            "--script-sha256", checksum,
        ])
        return result
    finally:
        cleanup_failed = False
        for client in clients.values():
            try:
                remove_fragment(client, folder + "/access-fragment.json")
            except Exception:
                cleanup_failed = True
            finally:
                client.close()
        if cleanup_failed:
            raise RuntimeError("probe credential cleanup needs operator attention")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["compare", "remote"])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[3])
    parser.add_argument("--key", type=pathlib.Path, default=pathlib.Path.home()/".ssh/id_ed25519_codex_remote")
    parser.add_argument("--known-hosts", type=pathlib.Path, default=pathlib.Path.home()/".ssh/known_hosts")
    parser.add_argument("--role", choices=["US", "HK"])
    parser.add_argument("--script-sha256")
    args = parser.parse_args()
    if args.action == "remote":
        if args.role is None or args.script_sha256 is None:
            parser.error("remote role and code checksum required")
        result = remote_probe(args.role, args.sha, args.script_sha256)
    else:
        result = compare(args.repo, args.sha, args.key, args.known_hosts)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exception messages may contain remote data: only emit the safe class.
        print(json.dumps({"http_status": None,
                          "safe_error_code": "probe_execution_" + type(error).__name__,
                          "target_model_visible": False}), file=sys.stderr)
        raise SystemExit(1)
