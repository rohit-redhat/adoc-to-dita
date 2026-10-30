"""Read immutable Git snapshots without checking out or modifying user branches."""
from __future__ import annotations

import fnmatch
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import subprocess
import tarfile
import tempfile
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .converter import ROOT, convert_files
from .report import file_diff


INCLUDE_DIRECTIVE = re.compile(r"^\s*include::([^\[]+)\[", re.M)
ATTRIBUTE_DEFINITION = re.compile(r"^:([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", re.M)
PATH_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
DEFAULT_GITLAB_HOSTS = frozenset({"gitlab.com", "gitlab.cee.redhat.com"})
PUBLIC_TLS_HOSTS = frozenset({"github.com", "api.github.com", "gitlab.com"})


def gitlab_hosts():
    hosts = set(DEFAULT_GITLAB_HOSTS)
    for host in os.environ.get("ADOC_DITA_GITLAB_HOSTS", "").split(","):
        host = host.strip().casefold()
        if host:
            hosts.add(host)
    return hosts


def remote_provider(hostname):
    if not hostname:
        return None
    host = hostname.casefold()
    if host == "github.com":
        return "github"
    if host in gitlab_hosts():
        return "gitlab"
    return None


def is_supported_remote_repository(value):
    try:
        remote_url(str(value))
        return True
    except ValueError:
        return False


def ssl_ca_bundle():
    for name in ("ADOC_DITA_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(name, "").strip()
        if path and Path(path).expanduser().is_file():
            return str(Path(path).expanduser())
    return None


def ssl_verify_enabled(hostname=None):
    """Return whether TLS certificates should be verified for this host.

    Public GitHub/GitLab SaaS keeps verification on. Self-hosted GitLab often
    presents a private CA that stock CPython does not trust; unless a CA bundle
    is configured, verification is relaxed for those hosts. Override with
    ADOC_DITA_SSL_VERIFY=1/0 or GIT_SSL_NO_VERIFY=1.
    """
    explicit = os.environ.get("ADOC_DITA_SSL_VERIFY", "").strip().lower()
    if explicit in {"0", "false", "no", "off"}:
        return False
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if os.environ.get("GIT_SSL_NO_VERIFY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    host = (hostname or "").casefold()
    if host in PUBLIC_TLS_HOSTS:
        return True
    if host and remote_provider(host) == "gitlab":
        return bool(ssl_ca_bundle())
    return True


def ssl_context(hostname=None):
    if not ssl_verify_enabled(hostname):
        return ssl._create_unverified_context()
    ca = ssl_ca_bundle()
    if ca:
        return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def git(repo, *args, timeout=180, hostname=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    if not ssl_verify_enabled(hostname):
        env["GIT_SSL_NO_VERIFY"] = "true"
    elif ca := ssl_ca_bundle():
        env["GIT_SSL_CAINFO"] = ca
    try:
        run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        operation = args[0] if args else "operation"
        raise ValueError(f"Git {operation} timed out after {timeout} seconds. Check GitHub/GitLab connectivity and try again.") from error
    if run.returncode:
        raise ValueError(run.stderr.decode(errors="replace").strip() or "Git command failed")
    return run.stdout


def remote_url(value):
    parts = urlsplit(str(value).strip())
    provider = remote_provider(parts.hostname)
    if (parts.scheme != "https" or not provider or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError("Remote repositories must use https://github.com/OWNER/REPO or "
                         "https://gitlab.example/GROUP/PROJECT; local paths also work")
    pieces = [piece for piece in parts.path.strip("/").removesuffix(".git").split("/") if piece]
    if (len(pieces) < 2 or any(piece in {".", "..", "-", "tree", "blob", "raw", "commit", "commits",
                                         "merge_requests", "issues", "pipelines", "wikis"}
                               for piece in pieces)
            or not all(PATH_SEGMENT.fullmatch(piece) for piece in pieces)):
        raise ValueError("Use a repository URL, without /tree/, /blob/, or /-/")
    if provider == "github" and len(pieces) != 2:
        raise ValueError("Use a repository URL, without /tree/ or /blob/")
    return f"https://{parts.hostname.casefold()}/{'/'.join(pieces)}.git"


def provider_token(provider):
    """Optional host token, same pattern for GitHub and GitLab.

    Public repositories work without a token. Set GITHUB_TOKEN or GITLAB_TOKEN
    for private projects and higher API rate limits.
    """
    if provider == "github":
        return os.environ.get("GITHUB_TOKEN", "").strip()
    if provider == "gitlab":
        return os.environ.get("GITLAB_TOKEN", "").strip() or os.environ.get("PRIVATE_TOKEN", "").strip()
    return ""


def api_headers(provider):
    """Build API headers; attach a Bearer token when one is configured."""
    if provider == "github":
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        headers = {"Accept": "application/json"}
    if token := provider_token(provider):
        headers["Authorization"] = "Bearer " + token
        # Classic GitLab personal access tokens also accept PRIVATE-TOKEN.
        if provider == "gitlab":
            headers["PRIVATE-TOKEN"] = token
    return headers


def authenticated_remote_url(url):
    """Attach a host token for private Git HTTPS operations when available."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        return url
    provider = remote_provider(parts.hostname)
    token = provider_token(provider)
    if not token:
        return url
    username = "oauth2" if provider == "gitlab" else "x-access-token"
    netloc = f"{username}:{quote(token, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def pull_request_location(value):
    """Parse a GitHub pull request or GitLab merge request URL.

    Returns (owner, repository, number) for GitHub URLs so existing callers and
    tests keep working, and a location dict for GitLab URLs. Prefer
    pull_request_metadata() for provider-agnostic handling.
    """
    location = _change_request_location(value)
    if location["provider"] == "github":
        owner, repository = location["project_path"].split("/", 1)
        return owner, repository, location["number"]
    return location


def _change_request_location(value):
    parts = urlsplit(str(value).strip())
    provider = remote_provider(parts.hostname)
    github_help = "Use a public GitHub pull request URL such as https://github.com/OWNER/REPO/pull/123"
    gitlab_help = ("Use a GitLab merge request URL such as "
                   "https://gitlab.com/GROUP/PROJECT/-/merge_requests/123")
    if parts.scheme != "https" or not provider or parts.username or parts.password:
        raise ValueError(f"{github_help}, or {gitlab_help}")
    pieces = [piece for piece in parts.path.strip("/").split("/") if piece]
    host = parts.hostname.casefold()
    if provider == "github":
        if (len(pieces) not in (4, 5) or pieces[2] != "pull" or not pieces[3].isdigit()
                or (len(pieces) == 5 and pieces[4] not in {"checks", "commits", "files"})
                or not all(PATH_SEGMENT.fullmatch(piece) for piece in pieces[:2])):
            raise ValueError(github_help)
        owner, repository, _, number = pieces[:4]
        repository = repository.removesuffix(".git")
        if int(number) < 1:
            raise ValueError("Pull request number must be positive")
        project_path = f"{owner}/{repository}"
        return {
            "provider": "github",
            "host": host,
            "project_path": project_path,
            "number": int(number),
            "repository": f"https://github.com/{project_path}",
            "url": f"https://github.com/{project_path}/pull/{int(number)}",
            "head_ref": f"refs/pull/{int(number)}/head",
        }
    # GitLab: /group[/subgroup]/project/-/merge_requests/123[/diffs|/commits|...]
    try:
        marker = pieces.index("-")
    except ValueError as error:
        raise ValueError(gitlab_help) from error
    project_pieces, rest = pieces[:marker], pieces[marker + 1:]
    if (len(project_pieces) < 2 or len(rest) < 2 or rest[0] != "merge_requests"
            or not rest[1].isdigit()
            or (len(rest) > 2 and rest[2] not in {"diffs", "commits", "pipelines", "reports"})
            or not all(PATH_SEGMENT.fullmatch(piece) for piece in project_pieces)):
        raise ValueError(gitlab_help)
    number = int(rest[1])
    if number < 1:
        raise ValueError("Merge request number must be positive")
    project_path = "/".join(piece.removesuffix(".git") if index == len(project_pieces) - 1 else piece
                            for index, piece in enumerate(project_pieces))
    return {
        "provider": "gitlab",
        "host": host,
        "project_path": project_path,
        "number": number,
        "repository": f"https://{host}/{project_path}",
        "url": f"https://{host}/{project_path}/-/merge_requests/{number}",
        "head_ref": f"refs/merge-requests/{number}/head",
    }


def _api_json(url, *, provider, description, headers):
    request = Request(url, headers={
        "User-Agent": "adoc-dita/0.2.0",
        **headers,
    })
    label = "GitHub" if provider == "github" else "GitLab"
    hostname = urlsplit(url).hostname
    try:
        with urlopen(request, timeout=30, context=ssl_context(hostname)) as response:
            payload = response.read(1024 * 1024 + 1)
    except HTTPError as error:
        if error.code == 404:
            raise ValueError(f"{label} {description} not found or not accessible") from error
        if error.code in (401, 403):
            token_hint = ("Set GITHUB_TOKEN for private GitHub access or higher rate limits."
                          if provider == "github"
                          else "Set GITLAB_TOKEN for private GitLab access or higher rate limits.")
            if error.code == 403 and provider == "github":
                raise ValueError(f"GitHub API rate limit or permission error. {token_hint}") from error
            raise ValueError(f"{label} denied access to the {description}. {token_hint}") from error
        raise ValueError(f"{label} could not load the {description} (HTTP {error.code})") from error
    except URLError as error:
        reason = str(error.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason or "certificate verify failed" in reason.lower():
            raise ValueError(
                f"Could not reach {label}: TLS certificate verification failed. "
                "Set ADOC_DITA_CA_BUNDLE or SSL_CERT_FILE to your corporate CA bundle, "
                "or set ADOC_DITA_SSL_VERIFY=0 for local use with a private CA."
            ) from error
        raise ValueError(f"Could not reach {label}: {error.reason}") from error
    if len(payload) > 1024 * 1024:
        raise ValueError(f"{label} {description} response exceeded 1 MB")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} returned an invalid {description} response") from error


def _github_pull_request_metadata(location):
    owner, repository = location["project_path"].split("/", 1)
    number = location["number"]
    headers = api_headers("github")
    data = _api_json(f"https://api.github.com/repos/{owner}/{repository}/pulls/{number}",
                     provider="github", description="pull request", headers=headers)
    try:
        base = data["base"]
        head = data["head"]
        full_name = data["base"]["repo"]["full_name"]
        # base.sha is the current tip of the base branch; head.sha is the PR tip.
        tip, head_sha = str(base["sha"]), str(head["sha"])
    except (KeyError, TypeError) as error:
        raise ValueError("GitHub returned an invalid pull request response") from error
    if full_name.casefold() != location["project_path"].casefold():
        raise ValueError("Pull request base repository does not match its URL")
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in [tip, head_sha]):
        raise ValueError("GitHub returned invalid pull request commits")
    # PR diffs are based on the common ancestor, not the current base tip. Ask
    # the compare endpoint for that exact merge base. A page beyond the commit
    # list omits file patches and keeps this response bounded.
    comparison = _api_json(
        f"https://api.github.com/repos/{owner}/{repository}/compare/{tip}...{head_sha}?per_page=1&page=2147483647",
        provider="github", description="pull request comparison", headers=headers)
    try:
        merge_base = str(comparison["merge_base_commit"]["sha"])
    except (KeyError, TypeError) as error:
        raise ValueError("GitHub returned an invalid pull request comparison") from error
    if not re.fullmatch(r"[0-9a-fA-F]{40}", merge_base):
        raise ValueError("GitHub returned an invalid pull request merge base")
    return {
        "url": location["url"],
        "number": number,
        "title": str(data.get("title") or f"Pull request #{number}"),
        "state": str(data.get("state") or "unknown"),
        "provider": "github",
        "repository": location["repository"],
        "base": {"ref": str(base["ref"]), "commit": merge_base, "tip": tip},
        "target": {"ref": location["head_ref"], "label": str(head.get("label") or "PR head"),
                   "commit": head_sha},
    }


def _gitlab_merge_request_metadata(location):
    """Resolve an MR the same way GitHub PRs are resolved: tip + merge base + head."""
    host = location["host"]
    project = quote(location["project_path"], safe="")
    number = location["number"]
    headers = api_headers("gitlab")
    data = _api_json(f"https://{host}/api/v4/projects/{project}/merge_requests/{number}",
                     provider="gitlab", description="merge request", headers=headers)
    try:
        diff_refs = data.get("diff_refs") or {}
        head_sha = str(diff_refs.get("head_sha") or data.get("sha") or "")
        merge_base = str(diff_refs.get("base_sha") or "")
        tip = str(diff_refs.get("start_sha") or "")
        target_branch = str(data.get("target_branch") or "")
        source_branch = str(data.get("source_branch") or "")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("GitLab returned an invalid merge request response") from error
    web_url = str(data.get("web_url") or location["url"])
    project_url = location["repository"]
    if isinstance(data.get("references"), dict) and "full" in data["references"]:
        # references.full looks like group/project!123 — strip the IID suffix.
        full_ref = str(data["references"]["full"])
        if "!" in full_ref:
            project_url = f"https://{host}/{full_ref.rsplit('!', 1)[0]}"
    elif data.get("web_url"):
        project_url = web_url.rsplit("/-/merge_requests/", 1)[0]
    if head_sha and not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise ValueError("GitLab returned invalid merge request commits")
    if not head_sha:
        raise ValueError("GitLab merge request is missing a head commit")
    # Same shape as GitHub: tip = current target-branch tip.
    if target_branch:
        try:
            branch = _api_json(
                f"https://{host}/api/v4/projects/{project}/repository/branches/{quote(target_branch, safe='')}",
                provider="gitlab", description="target branch", headers=headers)
            tip = str(branch.get("commit", {}).get("id") or tip)
        except ValueError:
            pass
    if tip and not re.fullmatch(r"[0-9a-fA-F]{40}", tip):
        raise ValueError("GitLab returned an invalid target branch tip")
    # Same as GitHub's compare call: resolve the merge base from tip + head when
    # the MR payload does not already include diff_refs.base_sha.
    if not merge_base:
        if not tip:
            raise ValueError("GitLab merge request is missing diff refs; reopen or update the MR and try again")
        merge = _api_json(
            f"https://{host}/api/v4/projects/{project}/repository/merge_base"
            f"?refs[]={quote(tip, safe='')}&refs[]={quote(head_sha, safe='')}",
            provider="gitlab", description="merge base", headers=headers)
        merge_base = str(merge.get("id") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", merge_base):
        raise ValueError("GitLab returned an invalid merge request merge base")
    if not tip:
        tip = merge_base
    return {
        "url": web_url,
        "number": number,
        "title": str(data.get("title") or f"Merge request !{number}"),
        "state": str(data.get("state") or "unknown"),
        "provider": "gitlab",
        "repository": project_url,
        "base": {"ref": target_branch or "target", "commit": merge_base, "tip": tip},
        "target": {"ref": location["head_ref"],
                   "label": source_branch or f"MR !{number}",
                   "commit": head_sha},
    }


def pull_request_metadata(value):
    location = _change_request_location(value)
    if location["provider"] == "github":
        return _github_pull_request_metadata(location)
    return _gitlab_merge_request_metadata(location)


def refs(repo):
    if str(repo).startswith("https:"):
        url = remote_url(str(repo))
        host = urlsplit(url).hostname
        fetch_url = authenticated_remote_url(url)
        raw = git(ROOT, "ls-remote", "--heads", "--tags", fetch_url, hostname=host).decode()
        return sorted({line.split("\t")[1].removeprefix("refs/heads/").removeprefix("refs/tags/") for line in raw.splitlines() if not line.endswith("^{}")})
    return git(Path(repo).expanduser().resolve(), "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/tags", "refs/remotes").decode().splitlines()


def resolve(repo, ref, hostname=None):
    if not ref or ref.startswith("-") or "\x00" in ref:
        raise ValueError("Select a branch, tag, or commit")
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", hostname=hostname).decode().strip()


def prepare_repository(value, base, target, cache=None):
    if not str(value).startswith("https:"):
        repo = Path(value).expanduser().resolve()
        return repo, resolve(repo, base), resolve(repo, target)
    url = remote_url(str(value))
    host = urlsplit(url).hostname
    fetch_url = authenticated_remote_url(url)
    cache = Path(cache or ROOT / ".cache/repos")
    repo = cache / hashlib.sha256(url.encode()).hexdigest()[:24]
    cache.mkdir(parents=True, exist_ok=True)
    # Git's own lock files survive a killed process. Serialize this private
    # cache across CLI processes and remove those stale transaction files only
    # after acquiring our independent lock.
    with (cache / (repo.name + ".adoc-dita.lock")).open("a+b") as cache_lock:
        fcntl.flock(cache_lock, fcntl.LOCK_EX)
        if repo.is_symlink():
            raise ValueError("Invalid remote repository cache")
        repo.mkdir(parents=True, exist_ok=True)
        for lock in repo.rglob("*.lock"):
            if lock.is_file() or lock.is_symlink():
                lock.unlink()
        if not (repo / "HEAD").exists():
            git(repo, "init", "--bare", hostname=host)
        advertised = {}
        for line in git(ROOT, "ls-remote", "--heads", "--tags", fetch_url, hostname=host).decode().splitlines():
            if not line.strip():
                continue
            sha, name = line.split("\t")
            advertised[name] = sha

        def fetch(ref):
            if not ref or ref.startswith("-"):
                raise ValueError("Invalid release reference")
            if re.fullmatch(r"refs/(?:pull|merge-requests)/[1-9][0-9]*/head", ref):
                lines = [line for line in git(ROOT, "ls-remote", fetch_url, ref, hostname=host).decode().splitlines() if line.strip()]
                if len(lines) != 1:
                    raise ValueError(
                        "Pull/merge request head is not available. "
                        "For private GitLab projects set GITLAB_TOKEN; for private GitHub set GITHUB_TOKEN.")
                sha, name = lines[0].split("\t")
                advertised[name] = sha
            names = [ref] if ref.startswith("refs/") else ["refs/heads/" + ref, "refs/tags/" + ref]
            names = [name for name in names if name in advertised]
            if len(names) > 1:
                raise ValueError(f"Ambiguous branch/tag {ref}; use refs/heads/ or refs/tags/")
            if names:
                sha = advertised.get(names[0] + "^{}", advertised[names[0]])
            elif re.fullmatch(r"[0-9a-fA-F]{40}", ref):
                sha = ref
            else:
                raise ValueError(f"Reference not found: {ref}")
            # Fetch only the chosen snapshot, pinned to the advertised object ID.
            try:
                git(repo, "cat-file", "-e", sha + "^{commit}", timeout=30, hostname=host)
            except ValueError:
                try:
                    git(repo, "fetch", "--no-tags", "--depth=1", fetch_url, sha, hostname=host)
                except ValueError:
                    # Some hosts reject bare SHA wants; fall back to the PR/MR head ref.
                    mr_refs = [name for name in advertised if re.fullmatch(r"refs/(?:pull|merge-requests)/[1-9][0-9]*/head", name)
                               and advertised.get(name + "^{}", advertised[name]) == sha]
                    if not mr_refs and re.fullmatch(r"refs/(?:pull|merge-requests)/[1-9][0-9]*/head", ref):
                        mr_refs = [ref]
                    if not mr_refs:
                        raise
                    git(repo, "fetch", "--no-tags", "--depth=1", fetch_url, mr_refs[0], hostname=host)
            return resolve(repo, sha, hostname=host)

        base_sha, target_sha = fetch(base), fetch(target)
    return repo, base_sha, target_sha


def snapshot(repo, commit, destination):
    data = git(repo, "archive", "--format=tar", commit, timeout=240)
    if len(data) > 300 * 1024 * 1024:
        raise ValueError("Snapshot exceeds the 300 MB limit")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        links = []
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe path in Git archive")
            if member.issym():
                links.append((path, member.linkname))
            if not member.isfile():
                continue
            output = destination.joinpath(*path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.extractfile(member).read())
        # Preserve repository include aliases, but never links outside the snapshot.
        for path, target in links:
            output = destination.joinpath(*path.parts)
            if Path(target).is_absolute() or not (output.parent / target).resolve().is_relative_to(destination.resolve()):
                continue
            if output.exists() or output.is_symlink():
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            output.symlink_to(target)
        for path, _ in links:
            output = destination.joinpath(*path.parts)
            if output.is_symlink() and not output.resolve().is_relative_to(destination.resolve()):
                output.unlink()


def changed_paths(repo, base, target):
    pieces = git(repo, "diff", "--name-status", "-z", "--find-renames=50%", base, target).decode().split("\0")
    result = []
    index = 0
    while index < len(pieces) and pieces[index]:
        status, path = pieces[index:index + 2]
        index += 2
        if status.startswith("R"):
            result.append({"change": "renamed", "before": path, "after": pieces[index]})
            index += 1
        else:
            result.append({"change": {"A": "added", "D": "deleted"}.get(status[0], "modified"),
                           "before": None if status == "A" else path, "after": None if status == "D" else path})
    return result


def topic_paths(root, patterns, kind="auto"):
    selected = []
    for file in sorted(root.rglob("*.adoc")):
        relative = file.relative_to(root).as_posix()
        if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
            continue
        text = file.read_text(encoding="utf-8")
        if re.search(r"^:_(?:mod-docs-content-type|content-type|module-type):\s*(SNIPPET|ATTRIBUTES)\s*$", text, re.M | re.I):
            continue
        if kind == "auto" and file.name.endswith(".template.adoc"):
            continue
        declared_type = re.search(r"^:_(?:mod-docs-content-type|content-type|module-type):\s*(CONCEPT|PROCEDURE|REFERENCE)\s*$", text, re.M | re.I)
        named_type = file.name.startswith(("con-", "proc-", "ref-"))
        # In automatic mode, only select sources whose topic specialization is
        # deterministic. Assemblies and guide entry documents still remain in
        # the Git change/dependency inventory, but are not emitted as topics.
        explicitly_typed = kind != "auto" and re.search(r"^=\s+\S", text, re.M)
        if declared_type or named_type or explicitly_typed:
            selected.append(relative)
    return selected


def source_dependencies(root, path, attributes=None, collected_attributes=None):
    """Return a conservative static include closure without running a build."""
    root = Path(root).resolve()
    initial = Path(path)
    known = {str(name): str(value) for name, value in (attributes or {}).items()}
    dependencies, visited = set(), set()

    def inspect(relative, inherited):
        relative = PurePosixPath(relative).as_posix()
        if relative in visited:
            return
        visited.add(relative)
        file = root.joinpath(*PurePosixPath(relative).parts)
        if not file.is_file() or not file.resolve().is_relative_to(root):
            return
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        local = dict(inherited)
        # This intentionally over-approximates conditional definitions. Extra
        # candidates cost conversion time; missing a dependency would be wrong.
        for name, value in ATTRIBUTE_DEFINITION.findall(text):
            local.setdefault(name, value)
            if collected_attributes is not None:
                collected_attributes.setdefault(name, value)
        local.update(known)
        local.update(docdir=PurePosixPath(relative).parent.as_posix(),
                     docname=PurePosixPath(relative).stem, docfile=relative)
        for target in INCLUDE_DIRECTIVE.findall(text):
            expanded = target.strip()
            for _ in range(8):
                updated = re.sub(r"\{([A-Za-z_][A-Za-z0-9_-]*)\}",
                                 lambda match: local.get(match.group(1), match.group(0)), expanded)
                if updated == expanded:
                    break
                expanded = updated
            if "{" in expanded or re.match(r"^[a-z][a-z0-9+.-]*:", expanded, re.I):
                continue
            candidate = (file.parent / expanded).resolve()
            if not candidate.is_relative_to(root) or not candidate.exists():
                candidate = (root / expanded).resolve()
            if not candidate.is_relative_to(root):
                continue
            dependency = candidate.relative_to(root).as_posix()
            dependencies.add(dependency)
            inspect(dependency, local)

    inspect(initial, known)
    dependencies.discard(initial.as_posix())
    return dependencies


def affected_topics(root, selected, touched, attribute_files, attributes=None):
    """Narrow a snapshot to directly changed and include-affected topics."""
    selected, touched = set(selected), set(touched)
    affected = selected & touched
    shared_dependencies = set()
    shared_attributes = {str(name): str(value) for name, value in (attributes or {}).items()}
    for path in attribute_files:
        shared_dependencies.add(path)
        shared_dependencies.update(source_dependencies(root, path, shared_attributes,
                                                       collected_attributes=shared_attributes))
    if shared_dependencies & touched:
        return selected
    for path in selected - affected:
        if not (Path(root) / path).is_file():
            continue
        dependencies = source_dependencies(root, path, shared_attributes) | shared_dependencies
        if dependencies & touched:
            affected.add(path)
    return affected


def normalize_repository_attribute_path(value):
    """Accept a repo-relative path, or extract one from a GitHub/GitLab blob URL."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("Attributes file path cannot be empty")
    expanded = Path(text).expanduser()
    if expanded.is_absolute():
        return text
    parts = urlsplit(text)
    if parts.scheme in {"http", "https"}:
        provider = remote_provider(parts.hostname)
        pieces = [piece for piece in parts.path.strip("/").split("/") if piece]
        relative = None
        if provider == "github" and len(pieces) >= 4 and pieces[2] == "blob":
            relative = "/".join(pieces[4:])
        elif provider == "gitlab":
            try:
                marker = pieces.index("-")
            except ValueError:
                marker = -1
            if marker >= 2 and len(pieces) > marker + 2 and pieces[marker + 1] == "blob":
                relative = "/".join(pieces[marker + 3:])
        if not relative or not relative.lower().endswith(".adoc"):
            raise ValueError(
                "Attributes file must be a repository-relative path such as common/attributes.adoc, "
                "not a browser page URL. You can also upload the .adoc file.")
        text = relative
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or not text.lower().endswith(".adoc"):
        raise ValueError("Attributes file must be a repository-relative .adoc path such as common/attributes.adoc")
    return path.as_posix()


def compare(repository, base, target, *, patterns=None, attributes=None, attribute_files=None,
            attribute_text="", attribute_filename=None, kind="auto", progress=None, guide=None):
    progress = progress or (lambda message: None)
    patterns = patterns or ["*.adoc"]
    attribute_text = attribute_text or ""
    if attribute_files is not None:
        attribute_files = [normalize_repository_attribute_path(path) for path in attribute_files]
    if attribute_text:
        if len(attribute_text.encode()) > 2 * 1024 * 1024:
            raise ValueError("Uploaded attributes file must be smaller than 2 MB")
        if not attribute_filename or Path(attribute_filename).name != attribute_filename or not attribute_filename.lower().endswith(".adoc"):
            raise ValueError("Uploaded attributes must use a filename ending in .adoc")
        if re.search(r"^include::", attribute_text, re.M):
            raise ValueError("Uploaded comparison attributes cannot include other files. Use repository-relative attribute files for include chains.")
    progress("Resolving the selected Git snapshots…")
    repo, base_sha, target_sha = prepare_repository(repository, base, target)
    changes = changed_paths(repo, base_sha, target_sha)
    touched = {p for change in changes for p in [change["before"], change["after"]] if p}
    report = {"format_version": 1, "repository": str(repository), "base": {"ref": base, "commit": base_sha},
              "target": {"ref": target, "commit": target_sha}, "settings": {"patterns": patterns, "attributes": attributes or {},
              "attribute_files": attribute_files, "uploaded_attribute_file": attribute_filename if attribute_text else None,
              "uploaded_attribute_sha256": hashlib.sha256(attribute_text.encode()).hexdigest() if attribute_text else None,
              "topic_type": kind, "guide": guide},
              "toolchain": {"adoc-dita": "0.2.0", "asciidoctor": "2.0.26", "dita-topic": "1.5.3", "dita-convert": "1.4.9", "dita_schema": "1.3-errata02"},
              "changes": [], "files": [], "notes": []}
    if base_sha == target_sha:
        report["summary"] = {"files": 0, "errors": 0, "review": 0, "xml_changed": 0}
        return report
    with tempfile.TemporaryDirectory(prefix="adoc-dita-compare-") as tmp:
        roots = [Path(tmp) / "before", Path(tmp) / "after"]
        outputs = []
        sources = []
        for label, commit, root in zip(["baseline", "target"], [base_sha, target_sha], roots):
            progress(f"Reading the {label} snapshot…")
            root.mkdir()
            snapshot(repo, commit, root)
        uploaded_path = None
        if attribute_text:
            uploaded_path = ".adoc-dita-uploaded-attributes-" + hashlib.sha256(attribute_text.encode()).hexdigest()[:16] + ".adoc"
            for root in roots:
                file = root / uploaded_path
                if file.exists() or file.is_symlink():
                    raise ValueError("Repository uses the reserved uploaded-attributes path")
                file.write_text(attribute_text, encoding="utf-8")
        selected = set(topic_paths(roots[0], patterns, kind)) | set(topic_paths(roots[1], patterns, kind))
        snapshot_attribute_files = {}
        for root in roots:
            if attribute_files is not None:
                files = list(attribute_files)
            elif guide:
                files = []
            else:
                files = []
                for candidate in ("artifacts/attributes.adoc", "common/attributes.adoc"):
                    if (root / candidate).is_file():
                        files = [candidate]
                        break
            if uploaded_path:
                files.append(uploaded_path)
            missing = [path for path in files if path != uploaded_path and not (root / path).is_file()]
            if missing:
                raise ValueError(
                    "Missing attribute file in the selected snapshot: " + ", ".join(missing)
                    + ". Use a repository-relative path such as common/attributes.adoc.")
            snapshot_attribute_files[root] = files
        if not guide:
            progress("Mapping changed and dependency-affected topics…")
            selected = (affected_topics(roots[0], selected, touched,
                                        snapshot_attribute_files[roots[0]], attributes)
                        | affected_topics(roots[1], selected, touched,
                                          snapshot_attribute_files[roots[1]], attributes))
        guide_data = {}
        if guide:
            from .context import RepositoryContext, convert_repository_files
            included = set()
            for root in roots:
                files = snapshot_attribute_files[root]
                if len(files) > 1:
                    raise ValueError("Guide comparison accepts either one repository attributes file or one uploaded attributes file")
                index = RepositoryContext(root)
                inspections = index.inspect([guide], attributes, files[0] if files else None)
                included.update(o['path'] for i in inspections for o in i['occurrences'])
                guide_data[root] = (index, inspections)
            if not included:
                raise ValueError('The guide has no active topics in either snapshot. Check the guide path and conditional attributes.')
            selected &= included
        for label, root in zip(["baseline", "target"], roots):
            # A title removed in one snapshot is an invalid topic, not a deleted file.
            paths = sorted(path for path in selected if (root / path).is_file())
            # Auto-load shared definitions when present, then apply an uploaded
            # attributes file as the final source-level definition set.
            attrs_files = snapshot_attribute_files[root]
            progress(f"Converting {len(paths)} {label} topics and checking dependencies…")
            if guide:
                index, inspections = guide_data[root]
                items = convert_repository_files(root, paths, guide=guide, attributes=attributes,
                                                 attribute_files=attrs_files, kind=kind, index=index, inspections=inspections)
            else:
                items = convert_files(root, paths, attributes=attributes, attribute_files=attrs_files,
                                      kind=kind, standalone=True) if paths else []
            if uploaded_path:
                for item in items:
                    item["dependencies"] = [path for path in item.get("dependencies", []) if path != uploaded_path]
            outputs.append({item["path"]: item for item in items})
            sources.append({path: (root / path).read_text(encoding="utf-8") for path in paths})
        old, new = outputs
        progress("Building the comparison report…")
        renames = {c["after"]: c["before"] for c in changes if c["change"] == "renamed" and c["before"] in old and c["after"] in new}
        pairs = [(renames.get(path, path), path) for path in sorted(new)]
        pairs.extend((path, None) for path in sorted(set(old) - set(new) - set(renames.values())))
        handled = set()
        for before_path, after_path in pairs:
            before = old.get(before_path)
            after = new.get(after_path)
            if before is None:
                before_path = None
            dependencies = set((before or {}).get("dependencies", [])) | set((after or {}).get("dependencies", []))
            source_changed = (before_path in touched or after_path in touched)
            xml_changed = (before or {}).get("xml") != (after or {}).get("xml")
            impacted = bool(dependencies & touched)
            failed = any(side and side["status"] == "error" for side in [before, after])
            if not (source_changed or xml_changed or (impacted and failed)):
                continue
            handled.update(p for p in [before_path, after_path] if p)
            change = "added" if before is None else "deleted" if after is None else "renamed" if before_path != after_path else "modified" if source_changed else "dependency"
            old_source, new_source = sources[0].get(before_path, ""), sources[1].get(after_path, "")
            item = {"change": change, "before_path": before_path, "after_path": after_path, "before": before, "after": after,
                    "xml_changed": xml_changed, "affected_dependencies": sorted(dependencies & touched),
                    "source_diff": file_diff(old_source, new_source, before_path or "/dev/null", after_path or "/dev/null"),
                    "xml_diff": file_diff((before or {}).get("xml") or "", (after or {}).get("xml") or "", (before or {}).get("output_path", "/dev/null"), (after or {}).get("output_path", "/dev/null"))}
            # A failed conversion is unknown XML, not an XML deletion/addition.
            if any(side and side["status"] == "error" for side in [before, after]):
                item["xml_diff"] = {"patch": "", "hunks": [], "unavailable": True}
                item["xml_changed"] = None
            report["files"].append(item)
        for change in changes:
            paths = [p for p in [change["before"], change["after"]] if p and any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns)]
            if paths:
                entry = dict(change)
                entry["conversion"] = "topic" if set(paths) & handled else "dependency-only or no document title"
                report["changes"].append(entry)
        report["summary"] = {"files": len(report["files"]),
                             "errors": sum(any(side and side["status"] == "error" for side in [i["before"], i["after"]]) for i in report["files"]),
                             "review": sum(any(side and side["status"] == "review" for side in [i["before"], i["after"]]) for i in report["files"]),
                             "xml_changed": sum(i["xml_changed"] is True for i in report["files"])}
        report["notes"] = ["Source and XML hunks are separate diffs, not a one-to-one source map.",
                           "Every selected topic is converted at both commits to detect include and attribute effects.",
                           "Images and other assets are referenced, not copied. Cross-file links may need review."]
    return report


def compare_pull_request(pull_request_url, *, patterns=None, attributes=None, attribute_files=None,
                         attribute_text="", attribute_filename=None, kind="auto", progress=None, guide=None):
    progress = progress or (lambda message: None)
    progress("Resolving the pull request…")
    metadata = pull_request_metadata(pull_request_url)
    # The merge base is the comparison base for this PR/MR. The named base
    # branch can advance independently and must not turn those later changes
    # into apparent PR deletions or modifications.
    # Pin both sides to the exact SHAs already resolved from the host API. This
    # avoids depending on refs/pull/*/head or refs/merge-requests/*/head being
    # advertised to an unauthenticated git ls-remote.
    report = compare(metadata["repository"], metadata["base"]["commit"], metadata["target"]["commit"],
                     patterns=patterns, attributes=attributes, attribute_files=attribute_files,
                     attribute_text=attribute_text, attribute_filename=attribute_filename,
                     kind=kind, progress=progress, guide=guide)
    if (report["base"]["commit"] != metadata["base"]["commit"]
            or report["target"]["commit"] != metadata["target"]["commit"]):
        raise ValueError("The pull request changed during conversion. Run it again to use one consistent head commit.")
    report["base"]["ref"] = metadata["base"]["ref"]
    report["target"]["ref"] = f"PR #{metadata['number']}"
    report["pull_request"] = metadata
    report["settings"]["workflow"] = "pull_request"
    targets = [item.get("after") for item in report["files"] if item.get("after")]
    report["summary"].update(
        target_errors=sum(item["status"] == "error" for item in targets),
        target_review=sum(item["status"] == "review" for item in targets),
        target_xml=sum(bool(item.get("xml")) for item in targets),
    )
    return report
