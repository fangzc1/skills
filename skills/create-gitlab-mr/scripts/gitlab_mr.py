#!/usr/bin/env python3
"""create-gitlab-mr 的可测试实现：Git 辅助、URL/文本脱敏、GitLab API 客户端。

只用 Python 标准库（≥3.9）。既可 `import gitlab_mr`，也可作为命令行使用：

    python gitlab_mr.py snapshot  --repo <dir>
    python gitlab_mr.py prepare   --repo <dir> --target-branch test
    python gitlab_mr.py preview   --repo <dir> --target-sha T --source-sha S
    python gitlab_mr.py project-info --repo <dir>
    python gitlab_mr.py ensure-mr --repo <dir> --source-branch B --target-branch test \
        --expected-sha M --title "..." --description-file desc.md [--check-only]

所有输出为 JSON（UTF-8）。凭据只从环境变量 GITLAB_TOKEN 读取，任何输出都先脱敏。
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Optional

MIN_GIT = (2, 38)
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TREE_RE = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")


# --------------------------------------------------------------------------
# 脱敏与远端地址解析
# --------------------------------------------------------------------------

_URL_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]*://)([^\s/?#@]*@)?([^\s?#]*)(\?[^\s#]*)?(#\S*)?"
)
_SCP_USER_RE = re.compile(r"\b[\w.\-]+@([\w.\-]+:(?!//))")


def redact_text(text: str) -> str:
    """去掉文本中的 URL userinfo、查询参数、fragment、scp 用户名及 GITLAB_TOKEN 原值。"""
    text = _URL_RE.sub(lambda m: m.group(1) + m.group(3), text)
    text = _SCP_USER_RE.sub(r"\1", text)
    token = os.environ.get("GITLAB_TOKEN")
    if token:
        text = text.replace(token, "***")
    return text


def parse_remote(url: str) -> dict[str, str]:
    """解析 https://、ssh://、git@host:group/project.git，返回 API 主机与项目路径。

    ssh:// 的端口是 SSH 端口，不用于 API 地址；https 端口保留。解析不了就抛 ValueError，
    由调用方询问用户，不猜测。
    """
    url = url.strip()
    m = re.match(
        r"^(?P<scheme>https?|ssh|git)://(?:[^@/]*@)?(?P<host>\[[^\]]+\]|[^/:?#]+)"
        r"(?::(?P<port>\d+))?/(?P<path>[^?#]+)",
        url,
    )
    if m:
        host = m["host"]
        if m["port"] and m["scheme"] in ("http", "https"):
            host = f"{host}:{m['port']}"
        path = m["path"]
    else:
        m = re.match(r"^(?:[\w.\-]+@)?(?P<host>[\w.\-]+):(?P<path>(?!//)[^?#]+)$", url)
        if not m or len(m["host"]) < 2:  # 排除 Windows 盘符 C:\
            raise ValueError("无法识别的远端地址格式")
        host, path = m["host"], m["path"]
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if "/" not in path or path.startswith(("/", ".")):
        raise ValueError("远端地址缺少 group/project 路径")
    return {"host": host, "project_path": path, "display": f"{host}/{path}"}


def validate_branch(name: str) -> str:
    """校验完整裸分支名；拒绝 @{-1}、-开头、origin/ 等远端引用前缀。"""
    if not name or name != name.strip():
        raise ValueError("分支名为空或含首尾空白")
    if name.startswith("-"):
        raise ValueError("分支名不能以 - 开头")
    if name == "@" or "@{" in name:
        raise ValueError("分支名不能含 @{ 引用语法")
    if name.startswith(("origin/", "refs/", "remotes/")):
        raise ValueError("请给出完整裸分支名，不接受远端引用或 refs 前缀")
    # 用 refs/heads/<name> 形式，避免 --branch 把 @{-1} 展开成上一个分支
    r = subprocess.run(["git", "check-ref-format", f"refs/heads/{name}"], capture_output=True)
    if r.returncode != 0:
        raise ValueError("不是合法的 Git 分支名")
    return name


# --------------------------------------------------------------------------
# Git 辅助
# --------------------------------------------------------------------------

class GitError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"  # 诊断固定为英文，便于判断 CONFLICT 等关键字
    env["LANGUAGE"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"  # 缺凭据时失败而不是挂起等待输入
    if not env.get("GIT_SSH_COMMAND") and not env.get("GIT_SSH") and env.get("GIT_SSH_VARIANT", "ssh") == "ssh":
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def git(repo: str, *args: str, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    """以参数数组和显式 cwd 调用 Git；网络命令超时抛 GitError。"""
    cmd = ["git"]
    if os.name == "nt":
        cmd += ["-c", "core.longpaths=true"]  # 仅本次命令生效，不写入仓库配置
    cmd += list(args)
    timeout = 120 if args and args[0] in ("fetch", "ls-remote", "push") else None
    try:
        return subprocess.run(cmd, cwd=repo, capture_output=True, input=input, env=_git_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} 网络命令超时（{timeout} 秒）；写入结果需另行核对，不自动重试") from None


def git_text(repo: str, *args: str) -> str:
    """成功返回去尾部换行的 stdout；失败抛 GitError（stderr 已脱敏）。"""
    r = git(repo, *args)
    if r.returncode != 0:
        err = redact_text(r.stderr.decode(errors="replace").strip())
        raise GitError(f"git {args[0]} 失败（退出码 {r.returncode}）：{err}")
    return r.stdout.decode(errors="replace").rstrip("\n")


def git_version() -> tuple[int, int]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        raise GitError("无法读取 Git 版本")
    return int(m[1]), int(m[2])


def require_git() -> None:
    if git_version() < MIN_GIT:
        raise GitError(f"需要 Git ≥ {MIN_GIT[0]}.{MIN_GIT[1]}（merge-tree --write-tree）")


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """退出码 0→True，1→False，其余（如 128）抛错。"""
    r = git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if r.returncode in (0, 1):
        return r.returncode == 0
    raise GitError(redact_text(r.stderr.decode(errors="replace").strip()) or "merge-base --is-ancestor 失败")


def preview_merge(repo: str, target_sha: str, source_sha: str) -> tuple[str, list[str], str]:
    """试合并（不写工作区）。返回 (状态, 冲突路径, 结果 tree OID)。

    状态为 "clean" 或 "conflict"；冲突路径列表可以为空（逻辑冲突，如目录重命名），
    绝不能因此降级为 clean。其余情况（引用无效、输出异常、退出码异常）一律抛错，不当作冲突。
    """
    r = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z",
            target_sha, source_sha)
    items = r.stdout.split(b"\0")
    valid_tree = _TREE_RE.fullmatch(items[0]) is not None
    if r.returncode in (0, 1) and valid_tree and r.stdout.endswith(b"\0"):
        paths = [os.fsdecode(p) for p in items[1:] if p]
        return ("clean" if r.returncode == 0 else "conflict"), paths, items[0].decode()
    err = redact_text(r.stderr.decode(errors="replace").strip())
    raise RuntimeError(err or "merge-tree 输出无效或命令失败")


def _git_path(repo: str, name: str) -> str:
    p = git_text(repo, "rev-parse", "--git-path", name)
    return p if os.path.isabs(p) else os.path.join(repo, p)


def snapshot(repo: str) -> dict[str, Any]:
    """固定源快照并检查必须暂停的仓库状态。problems 非空时调用方应暂停并说明。"""
    require_git()
    problems: list[str] = []
    r = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = r.stdout.decode().strip() if r.returncode == 0 else None
    if branch is None:
        problems.append("detached HEAD")
    sha = git_text(repo, "rev-parse", "HEAD")

    for marker, label in (("MERGE_HEAD", "merge 进行中"), ("CHERRY_PICK_HEAD", "cherry-pick 进行中"),
                          ("REVERT_HEAD", "revert 进行中"), ("rebase-merge", "rebase 进行中"),
                          ("rebase-apply", "rebase/am 进行中"), ("index.lock", "存在 index.lock")):
        if os.path.exists(_git_path(repo, marker)):
            problems.append(label)
    if git_text(repo, "rev-parse", "--is-shallow-repository") == "true":
        problems.append("浅克隆（无法可靠确定共同祖先）")
    if git(repo, "ls-files", "-u").stdout.strip():
        problems.append("存在未解决的索引项")

    remote: dict[str, str] = {}
    try:
        fetch_url = git_text(repo, "remote", "get-url", "origin")
        push_urls = git_text(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
        remote = parse_remote(fetch_url)
        if len(push_urls) != 1:
            problems.append("origin 有多个 push 地址")
        elif parse_remote(push_urls[0])["display"] != remote["display"]:
            problems.append("origin 的 fetch 与 push 指向不同项目")
    except (GitError, ValueError) as e:
        problems.append(f"origin 无法确认：{redact_text(str(e))}")

    status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal").stdout
    paths: list[str] = []
    parts = status.split(b"\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            paths.append(os.fsdecode(entry[3:]))
            if entry[:1] in (b"R", b"C") or entry[1:2] in (b"R", b"C"):
                i += 1  # 重命名/复制后跟原路径
        i += 1
    return {"source_branch": branch, "source_sha": sha, "remote": remote,
            "uncommitted_count": len(paths), "uncommitted_sample": paths[:10],
            "problems": problems}


def fetch_target(repo: str, target_branch: str) -> str:
    """fetch 指定目标到跟踪引用并返回 target_sha；失败即抛错，不退回旧引用。"""
    validate_branch(target_branch)
    ref = f"refs/remotes/origin/{target_branch}"
    git_text(repo, "fetch", "--no-tags", "origin", f"+refs/heads/{target_branch}:{ref}")
    return git_text(repo, "rev-parse", ref)


def scope(repo: str, target_sha: str, source_sha: str) -> dict[str, Any]:
    """范围摘要与源父树证据；不展开逐文件差异，也不替代用户范围判断。"""
    merge_base = git_text(repo, "merge-base", target_sha, source_sha)
    parent = git(repo, "rev-parse", "--verify", f"{source_sha}^1^{{tree}}")
    # 比较源的第一父树，而非共同祖先的父树；根提交没有该证据。
    parent_matches = (parent.stdout.decode().strip() == git_text(repo, "rev-parse", f"{merge_base}^{{tree}}")
                      if parent.returncode == 0 else None)
    count = int(git_text(repo, "rev-list", "--count", f"{target_sha}..{source_sha}"))
    recent = git_text(repo, "log", "-n", "10", "--oneline", f"{target_sha}..{source_sha}").splitlines()
    names = [os.fsdecode(p) for p in
             git(repo, "diff", "--name-only", "-z", f"{target_sha}...{source_sha}").stdout.split(b"\0") if p]
    tops: dict[str, int] = {}
    for n in names:
        top = n.split("/", 1)[0] if "/" in n else "(根目录)"
        tops[top] = tops.get(top, 0) + 1
    ordered = sorted(tops.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"merge_base": merge_base, "source_parent_matches_merge_base": parent_matches,
            "commit_count": count, "recent_commits": recent, "omitted_commits": max(0, count - len(recent)),
            "file_count": len(names), "top_dirs": dict(ordered[:10]),
            "omitted_dirs": max(0, len(ordered) - 10)}


def relation(repo: str, target_sha: str, source_sha: str) -> dict[str, bool]:
    """源与目标的关系。无共同祖先时抛错（不使用 --allow-unrelated-histories）。"""
    mb = git(repo, "merge-base", target_sha, source_sha)
    if mb.returncode != 0 or not mb.stdout.strip():
        raise GitError("源与目标没有共同祖先")
    return {"already_merged": is_ancestor(repo, source_sha, target_sha),
            "fast_forward": is_ancestor(repo, target_sha, source_sha)}


def remote_head(repo: str, branch: str) -> Optional[str]:
    """ls-remote 查询远端分支 SHA；不存在返回 None。"""
    out = git_text(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return sha
    return None


def make_branch_name(kind: str, source: str, target: str, now: Optional[datetime] = None) -> str:
    """merge|migrate/<源短名>-to-<目标短名>-<YYYYmmddHHMMSS>；仅用于临时分支名。"""
    if kind not in ("merge", "migrate"):
        raise ValueError("kind 只能是 merge 或 migrate")
    short = lambda s: s.replace("/", "-")[:30]  # noqa: E731
    stamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S")
    name = f"{kind}/{short(source)}-to-{short(target)}-{stamp}"
    r = subprocess.run(["git", "check-ref-format", f"refs/heads/{name}"], capture_output=True)
    if r.returncode != 0:
        raise ValueError("生成的临时分支名不合法")
    return name


def make_resolve_branch_name(source: str, target: str, now: Optional[datetime] = None) -> str:
    """保留完整源/目标名称，使用本地时间；不通过名称反解析溯源信息。"""
    validate_branch(source)
    validate_branch(target)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    name = f"resolve/{source}-to-{target}-{stamp}"
    validate_branch(name)
    return name


def branch_unused(repo: str, name: str) -> bool:
    """本地与远端都没有同名分支才返回 True。"""
    local = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0
    return not local and remote_head(repo, name) is None


def make_worktree(repo: str, base_sha: str, branch: Optional[str] = None) -> str:
    """在临时父目录创建 worktree：有 branch 则新建分支，否则 detach。返回 worktree 路径。"""
    parent = tempfile.mkdtemp(prefix="mr-")  # 前缀短，减少 Windows 长路径问题
    wt = os.path.join(parent, "wt")
    args = ["worktree", "add"] + (["-b", branch, wt, base_sha] if branch else ["--detach", wt, base_sha])
    git_text(repo, *args)
    return wt


def start_merge(wt: str, source_sha: str) -> dict[str, Any]:
    """在 worktree 执行 merge --no-ff --no-commit，并分类结果。

    state: clean（退出码 0）/ conflict（退出码 1、MERGE_HEAD==source_sha 且诊断为合并冲突）/ error。
    预检为 clean 而这里出现 conflict 或 error（rerere、merge driver、换行归一化等），
    调用方不得继续按无冲突路径交付。
    """
    r = git(wt, "merge", "--no-ff", "--no-commit", source_sha)
    out = (r.stdout + r.stderr).decode(errors="replace")
    head_file = _git_path(wt, "MERGE_HEAD")
    merge_head = None
    if os.path.exists(head_file):
        with open(head_file, encoding="utf-8") as f:
            merge_head = f.read().strip()
    unmerged = sorted({os.fsdecode(line.split(b"\t", 1)[1])
                       for line in git(wt, "ls-files", "-u", "-z").stdout.split(b"\0") if b"\t" in line})
    if r.returncode == 0:
        state = "clean"
    elif (r.returncode == 1 and merge_head == source_sha
          and ("CONFLICT" in out or "Automatic merge failed" in out)):
        state = "conflict"
    else:
        state = "error"
    return {"state": state, "exit_code": r.returncode, "merge_head": merge_head,
            "unmerged_paths": unmerged, "diagnostic": redact_text(out.strip())[:2000]}


def worktree_tree_matches(wt: str, expected_tree: str) -> bool:
    """merge --no-commit 之后，索引 tree 是否与 merge-tree 预检的 tree OID 一致。"""
    return git_text(wt, "write-tree") == expected_tree


def policy_requires_target_ancestor(merge_method: Optional[str]) -> bool:
    """ff 与 rebase_merge（半线性）都要求最终 MR 源提交已包含目标；merge 不要求。未知值抛错。"""
    if merge_method in ("ff", "rebase_merge"):
        return True
    if merge_method == "merge":
        return False
    raise ValueError(f"未知或缺失的 merge_method：{merge_method!r}，先确认项目策略")


# --------------------------------------------------------------------------
# GitLab API
# --------------------------------------------------------------------------

class ApiError(Exception):
    """GitLab 返回了非 2xx。status 为 HTTP 状态码，message 已脱敏。"""

    def __init__(self, status: Optional[int], message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


class ApiTransportError(ApiError):
    """超时、连接中断、响应无法解析：请求结果未知，调用方必须先查询再决定是否重试。"""

    def __init__(self, message: str):
        super().__init__(None, message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向，避免 PRIVATE-TOKEN 被带到其他地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"拒绝重定向: {msg}", headers, fp)


class GitLabClient:
    def __init__(self, api_base: str, project_path: str, token: Optional[str], *,
                 timeout: float = 30, ca_file: Optional[str] = None,
                 opener: Optional[urllib.request.OpenerDirector] = None):
        if not token:
            raise ApiError(None, "缺少环境变量 GITLAB_TOKEN，请在本机配置后重试")
        self.api_base = api_base.rstrip("/")
        self.project_path = project_path
        self._token = token
        self.timeout = timeout
        self.project_id: Optional[int] = None
        if opener is None:
            handlers: list = [NoRedirect()]
            ca = ca_file or os.environ.get("GITLAB_CA_BUNDLE")
            if ca:  # 仅追加信任的 CA（自签企业证书），不关闭校验
                context = ssl.create_default_context()
                context.load_verify_locations(cafile=ca)
                handlers.append(urllib.request.HTTPSHandler(context=context))
            opener = urllib.request.build_opener(*handlers)
        self._opener = opener

    def request(self, method: str, path: str, params: Optional[dict] = None,
                body: Optional[dict] = None) -> tuple[int, Any, Any]:
        url = self.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"PRIVATE-TOKEN": self._token, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw, status, hdrs = resp.read(), resp.status, resp.headers
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                parsed = json.loads(e.read(4000) or b"{}")
                detail = str(parsed.get("message") or parsed.get("error") or "") if isinstance(parsed, dict) else ""
            except Exception:
                pass
            raise ApiError(e.code, redact_text(detail or str(e.reason))[:300]) from None
        except (urllib.error.URLError, OSError, socket.timeout, http.client.HTTPException) as e:
            raise ApiTransportError(redact_text(str(e))[:300]) from None
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            raise ApiTransportError("响应无法解析为 JSON") from None
        return status, hdrs, payload

    # -- 项目 --------------------------------------------------------------
    def project(self) -> dict[str, Any]:
        enc = urllib.parse.quote(self.project_path, safe="")
        _, _, p = self.request("GET", f"/projects/{enc}")
        if not isinstance(p, dict) or "id" not in p:
            raise ApiTransportError("项目响应缺少 id")
        if p.get("path_with_namespace", "").lower() != self.project_path.lower():
            raise ApiError(None, "项目路径与 origin 不一致，先向用户确认")
        self.project_id = int(p["id"])
        self.project_path = p["path_with_namespace"]  # 使用服务端确认的规范大小写。
        return p

    def _pid(self) -> int:
        if self.project_id is None:
            self.project()
        return int(self.project_id)  # type: ignore[arg-type]

    # -- MR ----------------------------------------------------------------
    def list_open_mrs(self, source: str, target: str) -> list[dict[str, Any]]:
        pid, page, out = self._pid(), 1, []
        for _ in range(50):  # 翻页上限，防止异常响应导致死循环
            _, hdrs, items = self.request(
                "GET", f"/projects/{pid}/merge_requests",
                {"state": "opened", "source_branch": source, "target_branch": target,
                 "per_page": 100, "page": page})
            if not isinstance(items, list):
                raise ApiTransportError("MR 列表响应不是数组")
            out += items
            nxt = (hdrs.get("x-next-page") or "").strip()
            if not nxt:
                break
            page = int(nxt)
        return [m for m in out if m.get("source_branch") == source and m.get("target_branch") == target]

    def get_mr(self, iid: int) -> dict[str, Any]:
        _, _, mr = self.request("GET", f"/projects/{self._pid()}/merge_requests/{iid}")
        if not isinstance(mr, dict):
            raise ApiTransportError("MR 详情响应不是对象")
        return mr

    def verify_mr(self, mr: dict[str, Any], source: str, target: str, expected_sha: str) -> list[str]:
        """返回不符合项列表；空列表才算核验通过。sha 是 MR 源分支头，对照 expected_mr_sha。"""
        pid, bad = self._pid(), []
        if mr.get("state") != "opened":
            bad.append(f"state={mr.get('state')!r}，不是 opened")
        if mr.get("source_branch") != source or mr.get("target_branch") != target:
            bad.append("源/目标分支与预期不符")
        if mr.get("source_project_id") != pid or mr.get("target_project_id") != pid:
            bad.append("源或目标项目 ID 与已确认项目不符")
        if "sha" not in mr or not mr.get("sha"):
            bad.append("详情缺少 sha 字段")
        elif mr["sha"] != expected_sha:
            bad.append(f"sha 不符：MR 为 {str(mr['sha'])[:12]}，预期 {expected_sha[:12]}")
        if not self._valid_mr_url(mr):
            bad.append("web_url 不属于已确认的项目")
        return bad

    def _valid_mr_url(self, mr: dict[str, Any]) -> bool:
        """仅接受同源项目链接；默认端口等价，路径采用项目 API 的规范值。"""
        try:
            url = urllib.parse.urlparse(str(mr.get("web_url", "")))
            api = urllib.parse.urlparse(self.api_base)
            default = {"https": 443, "http": 80}
            return (url.scheme == api.scheme and url.hostname == api.hostname
                    and (url.port or default.get(url.scheme)) == (api.port or default.get(api.scheme))
                    and url.username is None and url.password is None
                    and url.path == f"/{self.project_path}/-/merge_requests/{mr.get('iid')}"
                    and not url.query and not url.fragment)
        except ValueError:
            return False

    @staticmethod
    def _summary(mr: dict[str, Any]) -> dict[str, Any]:
        return {k: mr.get(k) for k in ("iid", "web_url", "state", "sha", "source_branch",
                                       "target_branch", "merge_status", "detailed_merge_status")}

    def _find_and_verify(self, source: str, target: str, expected_sha: str) -> Optional[dict[str, Any]]:
        found = self.list_open_mrs(source, target)
        if not found:
            return None
        mr = self.get_mr(int(found[0]["iid"]))
        problems = self.verify_mr(mr, source, target, expected_sha)
        status = "reused" if not problems else "mismatch"
        return {"status": status, "problems": problems, "mr": self._summary(mr),
                "duplicates": len(found) - 1}

    def ensure_mr(self, source: str, target: str, expected_sha: str, title: str, description: str,
                  *, create: bool = True, title_regex: Optional[str] = None) -> dict[str, Any]:
        """查询→复用或创建→读详情核验。永不修改已有 MR，永不盲目重试 POST。

        status:
          reused            已有同源同目标 opened MR，核验通过
          created           新建并核验通过
          created_unverified 已创建（HTTP 201）但详情核验未通过，不重复 POST
          mismatch          已有 MR 但核验不通过，暂停报告
          none              check-only 且没有 MR
          unknown           POST 结果未知且重新查询也查不到
        """
        if not _SHA_RE.fullmatch(expected_sha):
            raise ValueError("expected_sha 必须是完整的 40 或 64 位小写十六进制提交 SHA")
        existing = self._find_and_verify(source, target, expected_sha)
        if existing:
            return existing
        if not create:
            return {"status": "none", "problems": [], "mr": None}
        if title_regex:
            try:
                if not re.search(title_regex, title):
                    raise ApiError(None, "标题不满足项目 merge_request_title_regex，请调整标题后重试")
            except re.error:
                pass  # GitLab 用 RE2/Ruby 语法，Python 解析不了时交给服务端判定
        body = {"source_branch": source, "target_branch": target, "title": title,
                "description": description, "remove_source_branch": False}
        try:
            _, _, created = self.request("POST", f"/projects/{self._pid()}/merge_requests", body=body)
        except ApiTransportError:
            return self._after_uncertain_post(source, target, expected_sha)
        except ApiError as e:
            if e.status == 409:
                return self._after_uncertain_post(source, target, expected_sha)
            raise
        if not isinstance(created, dict) or not created.get("iid") or not created.get("web_url") \
                or created.get("source_branch") != source or created.get("target_branch") != target:
            return self._after_uncertain_post(source, target, expected_sha)
        try:
            mr = self.get_mr(int(created["iid"]))
        except ApiError as e:
            # POST 已确认创建，后续读取失败不能抹去外部写入事实或触发重复创建。
            summary = self._summary(created)
            problems = [f"已创建，但详情核验失败：{redact_text(str(e))}"]
            if not self._valid_mr_url(created):
                summary["web_url"] = None
                problems.append("创建响应的链接未通过同源项目校验，仅保留 IID")
            return {"status": "created_unverified", "problems": problems,
                    "mr": summary, "duplicates": 0}
        problems = self.verify_mr(mr, source, target, expected_sha)
        return {"status": "created" if not problems else "created_unverified",
                "problems": problems, "mr": self._summary(mr), "duplicates": 0}

    def _after_uncertain_post(self, source: str, target: str, expected_sha: str) -> dict[str, Any]:
        try:
            found = self._find_and_verify(source, target, expected_sha)
        except ApiError:
            found = None
        if found:
            found["after_uncertain_post"] = True
            return found
        return {"status": "unknown", "problems": ["POST 结果未知且无法查到 MR，不要盲目重试"], "mr": None}


def client_from_repo(repo: str, *, timeout: float = 30) -> tuple[GitLabClient, dict[str, str]]:
    remote = parse_remote(git_text(repo, "remote", "get-url", "origin"))
    host = remote["host"]
    client = GitLabClient(f"https://{host}/api/v4", remote["project_path"],
                          os.environ.get("GITLAB_TOKEN"), timeout=timeout)
    return client, remote


def classify_candidates(repo: str, target_sha: str, source_sha: str) -> dict[str, list[dict[str, str]]]:
    """统一按补丁等价分类普通提交；历史等价不保证目标当前仍保留该行为。"""
    groups: dict[str, list[dict[str, str]]] = {"pending": [], "excluded": []}
    # 不在 Git 命令中限流，先完整分类，再在展示边界截断。
    for line in git_text(repo, "cherry", "-v", target_sha, source_sha).splitlines():
        fields = line.split(" ", 2)
        if len(fields) < 2 or fields[0] not in ("+", "-") or not _SHA_RE.fullmatch(fields[1]):
            raise GitError("git cherry 输出异常，不能可靠生成迁移候选")
        item = {"sha": fields[1], "subject": redact_text(fields[2] if len(fields) > 2 else ""),
                "reason": "patch_equivalent" if fields[0] == "-" else "no_equivalent_found"}
        groups["excluded" if fields[0] == "-" else "pending"].append(item)
    return groups


def preflight(repo: str, target: str, source: Optional[str] = None,
              check_only: bool = False) -> dict[str, Any]:
    """一次完成快照、目标获取与冲突预检；不创建工作区或 MR。"""
    validate_branch(target)
    snap = snapshot(repo)
    if source:
        validate_branch(source)
        snap["source_branch"] = source
        snap["source_sha"] = git_text(repo, "rev-parse", "--verify", f"refs/heads/{source}^{{commit}}")
    if snap["source_branch"] == target:
        snap["problems"].append("源分支与目标相同")
    if snap["problems"]:
        return {"status": "blocked", "snapshot": snap}
    t = fetch_target(repo, target)
    s = snap["source_sha"]
    out = {"snapshot": snap, "target_branch": target, "target_sha": t}
    rel = relation(repo, t, s)
    if rel["already_merged"]:
        return dict(out, status="already_merged")
    status, paths, _ = preview_merge(repo, t, s)
    out.update(status=status, scope=scope(repo, t, s), conflict_paths=paths[:10],
               omitted_paths=max(0, len(paths) - 10))
    if status == "conflict":
        classified = classify_candidates(repo, t, s)
        out["candidates"] = {key: items[:10] for key, items in classified.items()}
        out["candidates"].update({key + "_count": len(items) for key, items in classified.items()})
        out["candidates"].update({"omitted_" + key: max(0, len(items) - 10)
                                  for key, items in classified.items()})
        # 冲突确认只允许使用统一候选，去掉可被误用为迁移列表的原始日志。
        out["scope"]["source_ahead_count"] = out["scope"].pop("commit_count")
        out["scope"].pop("recent_commits")
        out["scope"].pop("omitted_commits")
    if not check_only:
        client, _ = client_from_repo(repo)
        project = client.project()
        out["project"] = {key: project.get(key) for key in
                          ("id", "merge_method", "squash_option", "merge_request_title_regex")}
    return out


def _pick_result(wt: str, result: subprocess.CompletedProcess) -> dict[str, Any]:
    paths = [os.fsdecode(p) for p in git(wt, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split(b"\0") if p]
    picking = os.path.exists(_git_path(wt, "CHERRY_PICK_HEAD"))
    status = "ready" if result.returncode == 0 else "conflict" if picking and paths else "blocked"
    return {"status": status, "worktree": wt,
            "mr_source_branch": git_text(wt, "symbolic-ref", "--short", "HEAD"),
            "expected_mr_sha": git_text(wt, "rev-parse", "HEAD"), "conflict_paths": paths,
            "diagnostic": redact_text((result.stdout + result.stderr).decode(errors="replace"))[-3000:]}


def resolve(repo: str, target_sha: str, source_sha: str, commits: list[str], *,
            source_branch: str, target_branch: str) -> dict[str, Any]:
    """只迁移明确选定的普通提交；在写工作区前校验范围并按源拓扑顺序排序。"""
    if not commits:
        raise ValueError("必须明确选择至少一个提交")
    if not _SHA_RE.fullmatch(target_sha) or not _SHA_RE.fullmatch(source_sha):
        raise ValueError("resolve 需要预检返回的完整快照 SHA")
    validate_branch(source_branch)
    validate_branch(target_branch)
    if source_branch == target_branch:
        raise ValueError("源分支与目标相同")
    if git_text(repo, "rev-parse", "--verify", f"refs/heads/{source_branch}") != source_sha:
        raise ValueError("源分支已变化或与固定快照不符，请重新预检")
    if git_text(repo, "rev-parse", "--verify", f"refs/remotes/origin/{target_branch}") != target_sha:
        raise ValueError("目标跟踪引用与固定快照不符，请重新预检")
    selected = set()
    for value in commits:
        if value.startswith("-"):
            raise ValueError("提交引用不能以 - 开头")
        sha = git_text(repo, "rev-parse", "--verify", f"{value}^{{commit}}")
        if not is_ancestor(repo, sha, source_sha):
            raise ValueError("选中提交不在固定源快照中")
        if len(git_text(repo, "rev-list", "--parents", "-n", "1", sha).split()) > 2:
            raise ValueError("不能自动迁移 merge commit：需明确 mainline")
        if is_ancestor(repo, sha, target_sha):
            raise ValueError("选中提交已在目标中，请确认迁移范围")
        selected.add(sha)
    classified = classify_candidates(repo, target_sha, source_sha)
    excluded = [item for item in classified["excluded"] if item["sha"] in selected]
    if excluded:
        return {"status": "equivalent_selected", "target_sha": target_sha, "excluded": excluded,
                "problems": ["选中提交存在历史等价补丁，未创建 worktree；如需重新引入 reverted 改动须明确确认"]}
    ordered = [sha for sha in git_text(repo, "rev-list", "--reverse", "--topo-order", source_sha).splitlines()
               if sha in selected]
    branch = make_resolve_branch_name(source_branch, target_branch)
    if not branch_unused(repo, branch):
        raise ValueError("解决分支名已存在，请重新生成")
    wt = make_worktree(repo, target_sha, branch)
    # 一次提交给 Git sequencer；冲突后 --continue 自动处理剩余已选提交。
    out = _pick_result(wt, git(wt, "-c", "core.editor=true", "cherry-pick", *ordered))
    out.update(selected_commits=ordered, source_branch=source_branch, source_sha=source_sha,
               target_branch=target_branch, target_sha=target_sha)
    return out


def continue_resolve(wt: str) -> dict[str, Any]:
    if git(wt, "ls-files", "-u").stdout.strip():
        raise ValueError("仍有未解决索引项，请显式暂存确认的冲突路径")
    return _pick_result(wt, git(wt, "-c", "core.editor=true", "cherry-pick", "--continue"))


def deliver(repo: str, source: str, expected: str, target: str, target_sha: str,
            title: str, description: str) -> dict[str, Any]:
    """一次复检目标、普通推送及创建/复用；不循环重建、不重试写入。"""
    validate_branch(source)
    validate_branch(target)
    if source == target or not _SHA_RE.fullmatch(expected) or not _SHA_RE.fullmatch(target_sha):
        raise ValueError("源目标相同或预期 SHA 非完整提交")
    if git_text(repo, "rev-parse", f"refs/heads/{source}") != expected:
        return {"status": "source_changed"}
    if git(repo, "ls-files", "-u").stdout.strip() or any(
            os.path.exists(_git_path(repo, name)) for name in ("CHERRY_PICK_HEAD", "MERGE_HEAD", "sequencer")):
        return {"status": "blocked", "problems": ["迁移或合并尚未完成"]}
    current_target = fetch_target(repo, target)
    if current_target != target_sha:
        status, paths, _ = preview_merge(repo, current_target, expected)
        if status == "conflict":
            return {"status": "target_changed", "target_sha": current_target, "conflict_paths": paths}
    remote = remote_head(repo, source)
    if remote and remote != expected:
        fetched = fetch_target(repo, source)
        if not is_ancestor(repo, fetched, expected):
            return {"status": "remote_diverged", "remote_sha": fetched}
    client, _ = client_from_repo(repo)
    project = client.project()  # 写入前核对凭据、项目策略；未知策略不写入。
    requires_sync = (policy_requires_target_ancestor(project.get("merge_method"))
                     and not is_ancestor(repo, current_target, expected))
    if remote != expected:
        git_text(repo, "push", "origin", f"{expected}:refs/heads/{source}")
    if remote_head(repo, source) != expected:
        return {"status": "push_unverified"}
    out = client.ensure_mr(source, target, expected, title, description,
                           title_regex=project.get("merge_request_title_regex"))
    out["target_sha"] = current_target
    out["requires_source_sync"] = requires_sync
    return out


def build_description(info: dict[str, Any]) -> str:
    """生成 MR 描述（Markdown）。只放摘要；不贴完整差异、测试输出或带凭据的地址。"""
    def line(label: str, key: str) -> Optional[str]:
        return f"- {label}：{info[key]}" if info.get(key) else None

    lines = ["## 范围",
             line("源分支 / 原始源提交", "source"), line("目标分支 / 目标快照", "target"),
             line("MR 源提交（expected_mr_sha）", "expected_mr_sha"),
             line("选中提交（selected_sha，未带入其他祖先提交）", "selected_sha"),
             line("提交与文件概况", "scope"),
             "", "## 冲突处理", info.get("conflicts") or "- 无冲突",
             "", "## 验证", info.get("verification") or "- 未验证（见下）",
             "", "## 未验证项与风险", info.get("risks") or "- 无"]
    return "\n".join(x for x in lines if x is not None) + "\n"


# --------------------------------------------------------------------------
# 可选计时（默认只报告总耗时与最慢项，详细记录仅在用户要求时留存）
# --------------------------------------------------------------------------

class StepTimer:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def step(self, phase: str, name: str, snapshot: str = ""):
        t0, status = time.perf_counter(), "ok"
        try:
            yield
        except BaseException as e:
            status = f"error:{type(e).__name__}"
            raise
        finally:
            self.records.append({"phase": phase, "name": name, "snapshot": snapshot,
                                 "seconds": round(time.perf_counter() - t0, 3), "status": status})

    def skipped(self, phase: str, name: str, status: str = "未运行") -> None:
        """跳过或复用的步骤记状态，不记 0 秒。"""
        self.records.append({"phase": phase, "name": name, "snapshot": "", "seconds": None, "status": status})

    def summary(self) -> dict[str, Any]:
        timed = [r for r in self.records if r["seconds"] is not None]
        phases: dict[str, float] = {}
        for r in timed:
            phases[r["phase"]] = round(phases.get(r["phase"], 0) + r["seconds"], 3)
        slowest = max(timed, key=lambda r: r["seconds"], default=None)
        return {"phases": phases, "total_seconds": round(sum(phases.values()), 3), "slowest": slowest}


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def _out(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("snapshot", "prepare", "preview", "project-info", "ensure-mr", "branch-name", "worktree", "start-merge", "preflight", "resolve", "deliver"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        if name == "preflight":
            p.add_argument("--target-branch", required=True)
            p.add_argument("--source-branch")
            p.add_argument("--check-only", action="store_true")
        if name == "resolve":
            p.add_argument("--source-branch")
            p.add_argument("--target-branch")
            p.add_argument("--target-sha")
            p.add_argument("--source-sha")
            p.add_argument("--commits", nargs="+")
            p.add_argument("--continue", dest="resume", action="store_true")
        if name == "deliver":
            p.add_argument("--source-branch", required=True)
            p.add_argument("--expected-sha", required=True)
            p.add_argument("--target-branch", required=True)
            p.add_argument("--target-sha", required=True)
            p.add_argument("--title", required=True)
            p.add_argument("--description-file", required=True)
        if name == "prepare":
            p.add_argument("--target-branch", required=True)
        if name == "preview":
            p.add_argument("--target-sha", required=True)
            p.add_argument("--source-sha", required=True)
        if name == "start-merge":
            p.add_argument("--source-sha", required=True)
            p.add_argument("--expected-tree")
        if name == "ensure-mr":
            p.add_argument("--source-branch", required=True)
            p.add_argument("--target-branch", required=True)
            p.add_argument("--expected-sha", required=True)
            p.add_argument("--title", required=True)
            p.add_argument("--description-file", required=True)
            p.add_argument("--check-only", action="store_true")
        if name == "branch-name":
            p.add_argument("--kind", choices=["merge", "migrate"], required=True)
            p.add_argument("--source", required=True)
            p.add_argument("--target", required=True)
        if name == "worktree":
            p.add_argument("--base-sha", required=True)
            p.add_argument("--branch")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "preflight":
            _out(preflight(a.repo, a.target_branch, a.source_branch, a.check_only))
        elif a.cmd == "resolve":
            if a.resume:
                _out(continue_resolve(a.repo))
            elif a.target_sha and a.source_sha and a.commits and a.source_branch and a.target_branch:
                _out(resolve(a.repo, a.target_sha, a.source_sha, a.commits,
                             source_branch=a.source_branch, target_branch=a.target_branch))
            else:
                raise ValueError("resolve 需要 source-branch、target-branch、target-sha、source-sha、commits，或 --continue")
        elif a.cmd == "deliver":
            with open(a.description_file, encoding="utf-8") as f:
                _out(deliver(a.repo, a.source_branch, a.expected_sha, a.target_branch,
                             a.target_sha, a.title, f.read()))
        elif a.cmd == "snapshot":
            _out(snapshot(a.repo))
        elif a.cmd == "prepare":
            snap = snapshot(a.repo)
            if snap["source_branch"] == a.target_branch:
                snap["problems"].append("源分支与目标相同")
            if snap["problems"]:
                _out({"snapshot": snap, "blocked": True})
                return 2
            target_sha = fetch_target(a.repo, a.target_branch)
            rel = relation(a.repo, target_sha, snap["source_sha"])
            out: dict[str, Any] = {"snapshot": snap, "target_branch": a.target_branch,
                                   "target_sha": target_sha, "relation": rel, "blocked": False}
            if not rel["already_merged"]:
                out["scope"] = scope(a.repo, target_sha, snap["source_sha"])
            _out(out)  # 范围门槛：到此为止，不跑 merge-tree
        elif a.cmd == "preview":
            status, paths, tree = preview_merge(a.repo, a.target_sha, a.source_sha)
            res: dict[str, Any] = {"status": status, "conflict_paths": paths[:10],
                                   "omitted_paths": max(0, len(paths) - 10), "tree_oid": tree}
            if status == "clean":
                res["fast_forward"] = is_ancestor(a.repo, a.target_sha, a.source_sha)
            _out(res)
        elif a.cmd == "start-merge":
            res = start_merge(a.repo, a.source_sha)
            # 预期冲突是可处理的结果；只有执行错误或预检 tree 不一致才使 CLI 失败。
            if res["state"] == "clean" and a.expected_tree:
                res["tree_matches"] = worktree_tree_matches(a.repo, a.expected_tree)
            _out(res)
            if res["state"] == "error" or res.get("tree_matches") is False:
                return 1
        elif a.cmd == "project-info":
            client, remote = client_from_repo(a.repo)
            p = client.project()
            _out({"project_id": p["id"], "path_with_namespace": p["path_with_namespace"],
                  "merge_method": p.get("merge_method"), "squash_option": p.get("squash_option"),
                  "merge_request_title_regex": p.get("merge_request_title_regex"),
                  "remote": remote["display"]})
        elif a.cmd == "ensure-mr":
            client, _ = client_from_repo(a.repo)
            proj = client.project()
            with open(a.description_file, encoding="utf-8") as f:
                desc = f.read()
            _out(client.ensure_mr(a.source_branch, a.target_branch, a.expected_sha, a.title, desc,
                                  create=not a.check_only,
                                  title_regex=proj.get("merge_request_title_regex")))
        elif a.cmd == "branch-name":
            name = make_branch_name(a.kind, a.source, a.target)
            _out({"branch": name, "unused": branch_unused(a.repo, name)})
        elif a.cmd == "worktree":
            _out({"worktree": make_worktree(a.repo, a.base_sha, a.branch)})
    except (GitError, RuntimeError, ValueError, ApiError, OSError) as e:
        print(json.dumps({"error": redact_text(str(e))}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
