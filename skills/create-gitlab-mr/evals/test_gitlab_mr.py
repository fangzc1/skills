"""scripts/gitlab_mr.py 的回归测试。仅用标准库、临时 Git 仓库和本机 mock HTTP 服务，不访问真实远端或凭据。

运行（任意目录）：python3 -I evals/test_gitlab_mr.py
"""
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import gitlab_mr as g  # noqa: E402

TREE = "a" * 40


# ---------------------------------------------------------------- 纯函数 ----

class RedactionTests(unittest.TestCase):
    def test_url_userinfo_query_fragment_removed(self):
        raw = "fatal: https://oauth2:FAKE_TOKEN_NOT_REAL@gitlab.example.invalid/group/repo.git?auth=FAKE_QUERY#frag failed"
        out = g.redact_text(raw)
        for secret in ("FAKE_TOKEN_NOT_REAL", "FAKE_QUERY", "frag", "oauth2"):
            self.assertNotIn(secret, out)
        self.assertIn("gitlab.example.invalid/group/repo.git", out)

    def test_scp_user_removed(self):
        self.assertNotIn("git@", g.redact_text("cannot reach git@gitlab.example.invalid:g/p.git"))

    def test_env_token_masked(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-SECRET123"}):
            self.assertEqual(g.redact_text("bad glpat-SECRET123 here"), "bad *** here")


class ParseRemoteTests(unittest.TestCase):
    def test_formats(self):
        cases = {
            "https://oauth2:x@gitlab.example.com/a/b/c.git?q=1#f": ("gitlab.example.com", "a/b/c"),
            "https://gitlab.example.com:8443/a/b": ("gitlab.example.com:8443", "a/b"),
            "ssh://git@gitlab.example.com:2222/a/b.git": ("gitlab.example.com", "a/b"),
            "git@gitlab.example.com:grp/sub/proj.git": ("gitlab.example.com", "grp/sub/proj"),
        }
        for url, (host, path) in cases.items():
            with self.subTest(url=url):
                r = g.parse_remote(url)
                self.assertEqual((r["host"], r["project_path"]), (host, path))
                self.assertNotIn("x@", r["display"])

    def test_rejects_unparseable(self):
        for url in ("C:\\repos\\x", "/local/path", "https://host/onlyone", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                g.parse_remote(url)


class BranchValidationTests(unittest.TestCase):
    def test_accepts(self):
        for n in ("test", "dev", "release/pre-2026"):
            self.assertEqual(g.validate_branch(n), n)

    def test_rejects(self):
        for n in ("@{-1}", "-x", "origin/test", "refs/heads/test", "", " test", "a..b", "a b", "@"):
            with self.subTest(name=n), self.assertRaises(ValueError):
                g.validate_branch(n)

    def test_make_branch_name(self):
        now = datetime(2026, 11, 13, 16, 20, 5)
        n = g.make_branch_name("merge", "feat/" + "x" * 50, "release/pre-2026", now)
        self.assertTrue(n.startswith("merge/feat-"))
        self.assertTrue(n.endswith("-to-release-pre-2026-20261113162005"))
        self.assertEqual(g.make_branch_name("migrate", "a", "b", now), "migrate/a-to-b-20261113162005")
        with self.assertRaises(ValueError):
            g.make_branch_name("push", "a", "b")


class ResolveNameTests(unittest.TestCase):
    def test_preserves_full_names_and_local_time(self):
        now = datetime(2026, 11, 24, 18, 30, 25)
        source = "feat/TOKENHUB-Story-689-" + "x" * 40
        self.assertEqual(g.make_resolve_branch_name(source, "release/pre-2026", now),
                         f"resolve/{source}-to-release/pre-2026-20261124-183025")

    def test_rejects_invalid_branch_inputs(self):
        for source, target in (("origin/feat/x", "dev"), ("feat/x", "-dev")):
            with self.subTest(source=source), self.assertRaises(ValueError):
                g.make_resolve_branch_name(source, target)


class PolicyTests(unittest.TestCase):
    def test_policy(self):
        self.assertTrue(g.policy_requires_target_ancestor("ff"))
        self.assertTrue(g.policy_requires_target_ancestor("rebase_merge"))
        self.assertFalse(g.policy_requires_target_ancestor("merge"))
        for bad in (None, "", "squash"):
            with self.assertRaises(ValueError):
                g.policy_requires_target_ancestor(bad)


class PreviewOutputTests(unittest.TestCase):
    def run_preview(self, code, out, err=b""):
        res = subprocess.CompletedProcess([], code, stdout=out, stderr=err)
        with patch.object(g.subprocess, "run", return_value=res):
            return g.preview_merge("unused", "t", "s")

    def test_logical_conflict_without_paths(self):
        self.assertEqual(self.run_preview(1, TREE.encode() + b"\0"), ("conflict", [], TREE))

    def test_regular_conflict_and_clean_and_sha256(self):
        self.assertEqual(self.run_preview(1, TREE.encode() + b"\0a.txt\0"), ("conflict", ["a.txt"], TREE))
        self.assertEqual(self.run_preview(0, TREE.encode() + b"\0"), ("clean", [], TREE))
        self.assertEqual(self.run_preview(1, b"b" * 64 + b"\0")[0], "conflict")

    def test_errors_are_not_conflicts(self):
        for code, out, err in ((1, b"", b"not something we can merge"), (0, b"", b""),
                               (1, b"bad\0a\0", b""), (128, TREE.encode() + b"\0", b"fatal")):
            with self.subTest(code=code, out=out), self.assertRaises(RuntimeError):
                self.run_preview(code, out, err)

    def test_error_message_is_redacted(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_preview(1, b"", b"x https://u:SECRET@h.invalid/a/b?t=SECRET2")
        self.assertNotIn("SECRET", str(cm.exception))


class NetworkTests(unittest.TestCase):
    def test_network_timeout_and_batch_mode(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(g.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired("git", 120)
            with self.assertRaises(g.GitError):
                g.git("unused", "fetch", "origin")
            self.assertEqual(run.call_args.kwargs["timeout"], 120)
            self.assertIn("BatchMode=yes", run.call_args.kwargs["env"]["GIT_SSH_COMMAND"])


class TimerTests(unittest.TestCase):
    def test_skipped_is_not_zero_seconds(self):
        t = g.StepTimer()
        with t.step("验证", "test", "S"):
            pass
        t.skipped("验证", "lint", "复用")
        s = t.summary()
        self.assertEqual(len(t.records), 2)
        self.assertIsNone(t.records[1]["seconds"])
        self.assertEqual(s["slowest"]["name"], "test")

    def test_error_recorded(self):
        t = g.StepTimer()
        with self.assertRaises(ValueError), t.step("交付", "api"):
            raise ValueError("x")
        self.assertTrue(t.records[0]["status"].startswith("error:"))


class DescriptionTests(unittest.TestCase):
    def test_description(self):
        d = g.build_description({"source": "feat/a @ S", "target": "test @ T", "expected_mr_sha": "M",
                                 "conflicts": "- a.txt：保留双方文案"})
        self.assertIn("## 范围", d)
        self.assertIn("expected_mr_sha", d)
        self.assertNotIn("selected_sha", d)
        self.assertIn("未验证", d)


# ------------------------------------------------------------ 真实 Git ----

class GitRepoCase(unittest.TestCase):
    def setUp(self):
        # 隔离用户全局 hooks（例如会后台写入的 git-ai），不改变真实仓库的 hook 行为。
        env = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull,
                                     "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="mrskill-")
        self.addCleanup(self.tmp.cleanup)
        # 将生产函数新建的 worktree 父目录也收进本测试根目录，结束时统一清理。
        mkdtemp = tempfile.mkdtemp
        worktrees = patch.object(g.tempfile, "mkdtemp", side_effect=lambda **kw: mkdtemp(**dict(kw, dir=self.tmp.name)))
        worktrees.start()
        self.addCleanup(worktrees.stop)
        self.repo = str(Path(self.tmp.name) / "repo")
        os.mkdir(self.repo)
        self.run_git("init", "-q", "-b", "main")
        self.run_git("config", "user.email", "t@example.invalid")
        self.run_git("config", "user.name", "t")
        self.run_git("config", "commit.gpgsign", "false")

    def run_git(self, *args, repo=None):
        r = subprocess.run(["git", *args], cwd=repo or self.repo, capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(f"git {args} -> {r.stderr}")
        return r.stdout.strip()

    def commit(self, files, msg, repo=None):
        repo = repo or self.repo
        for name, body in files.items():
            p = Path(repo) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        self.run_git("add", "-A", repo=repo)
        self.run_git("commit", "-q", "-m", msg, repo=repo)
        return self.run_git("rev-parse", "HEAD", repo=repo)

    def branch_from(self, name, base):
        self.run_git("branch", name, base)


class GitHelperTests(GitRepoCase):
    def make_graph(self, conflict=False):
        base = self.commit({"a.txt": "base\n", "ui/x.txt": "x\n"}, "base")
        self.branch_from("target", base)
        self.branch_from("source", base)
        self.run_git("checkout", "-q", "source")
        s = self.commit({"a.txt": "source\n" if conflict else "base\n", "ui/new.txt": "n\n", "ui/x.txt": "x2\n"}, "src")
        self.run_git("checkout", "-q", "target")
        t = self.commit({"a.txt": "target\n" if conflict else "base\n", "doc/t.md": "t\n"}, "tgt")
        self.run_git("checkout", "-q", "source")
        return base, s, t

    def test_snapshot_clean_and_dirty(self):
        self.commit({"a.txt": "1\n"}, "c1")
        self.run_git("remote", "add", "origin", "https://oauth2:SECRET@gitlab.example.invalid/g/p.git")
        (Path(self.repo) / "dirty.txt").write_text("d")
        snap = g.snapshot(self.repo)
        self.assertEqual(snap["source_branch"], "main")
        self.assertEqual(snap["uncommitted_count"], 1)
        self.assertEqual(snap["problems"], [])
        self.assertNotIn("SECRET", json.dumps(snap))
        self.assertEqual(snap["remote"]["project_path"], "g/p")

    def test_snapshot_blockers(self):
        sha = self.commit({"a.txt": "1\n"}, "c1")
        self.run_git("remote", "add", "origin", "git@gitlab.example.invalid:g/p.git")
        self.run_git("checkout", "-q", "--detach", sha)
        self.assertIn("detached HEAD", g.snapshot(self.repo)["problems"])
        self.run_git("checkout", "-q", "main")
        (Path(self.repo) / ".git" / "index.lock").write_text("")
        self.assertIn("存在 index.lock", g.snapshot(self.repo)["problems"])

    def test_snapshot_push_url_mismatch(self):
        self.commit({"a.txt": "1\n"}, "c1")
        self.run_git("remote", "add", "origin", "https://h.invalid/g/p.git")
        self.run_git("remote", "set-url", "--push", "origin", "https://h.invalid/other/p.git")
        self.assertTrue(any("fetch 与 push" in p for p in g.snapshot(self.repo)["problems"]))

    def test_relation_scope_preview_clean_non_ff(self):
        base, s, t = self.make_graph()
        rel = g.relation(self.repo, t, s)
        self.assertEqual(rel, {"already_merged": False, "fast_forward": False})
        sc = g.scope(self.repo, t, s)
        self.assertEqual(sc["commit_count"], 1)
        self.assertEqual(sc["top_dirs"], {"ui": 2})
        self.assertEqual(sc["merge_base"], base)
        self.assertTrue(sc["source_parent_matches_merge_base"])
        status, paths, tree = g.preview_merge(self.repo, t, s)
        self.assertEqual((status, paths), ("clean", []))
        self.assertFalse(g.is_ancestor(self.repo, t, s))
        # 预检 tree 与 worktree 实际合并 tree 一致，且不改变原工作区
        wt = g.make_worktree(self.repo, t)
        res = g.start_merge(wt, s)
        self.assertEqual(res["state"], "clean")
        self.assertTrue(g.worktree_tree_matches(wt, tree))
        self.run_git("merge", "--abort", repo=wt)
        self.assertEqual(self.run_git("rev-parse", "--abbrev-ref", "HEAD"), "source")

    def test_scope_parent_content_is_not_hidden(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        self.commit({"prior.txt": "prior\n"}, "prior")
        source = self.commit({"feature.txt": "feature\n"}, "feature")
        sc = g.scope(self.repo, base, source)
        self.assertEqual(sc["merge_base"], base)
        self.assertFalse(sc["source_parent_matches_merge_base"])
        self.assertEqual(sc["file_count"], 2)

    def test_scope_root_has_no_parent_evidence(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        sc = g.scope(self.repo, base, base)
        self.assertIsNone(sc["source_parent_matches_merge_base"])

    def test_scope_zero_net_history_before_source(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        # 历史多了提交，但源父树与共同祖先相同；不能把历史数量等同额外内容。
        self.run_git("commit", "-q", "--allow-empty", "-m", "history")
        source = self.commit({"feature.txt": "feature\n"}, "feature")
        sc = g.scope(self.repo, base, source)
        self.assertTrue(sc["source_parent_matches_merge_base"])
        self.assertEqual(sc["commit_count"], 2)
        self.assertEqual(sc["file_count"], 1)

    def test_fast_forward_tree_equals_source(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        self.branch_from("target", base)
        s = self.commit({"a.txt": "s\n"}, "src")
        rel = g.relation(self.repo, base, s)
        self.assertTrue(rel["fast_forward"])
        _, _, tree = g.preview_merge(self.repo, base, s)
        self.assertEqual(tree, self.run_git("rev-parse", f"{s}^{{tree}}"))

    def test_already_merged(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        t = self.commit({"a.txt": "t\n"}, "t")
        self.assertTrue(g.relation(self.repo, t, base)["already_merged"])

    def test_conflict_flow_in_worktree(self):
        base, s, t = self.make_graph(conflict=True)
        status, paths, _ = g.preview_merge(self.repo, t, s)
        self.assertEqual((status, paths), ("conflict", ["a.txt"]))
        branch = g.make_branch_name("merge", "source", "target")
        wt = g.make_worktree(self.repo, t, branch)
        res = g.start_merge(wt, s)
        self.assertEqual(res["state"], "conflict")
        self.assertEqual(res["unmerged_paths"], ["a.txt"])
        self.assertEqual(res["merge_head"], s)
        # 原工作区未被触碰
        self.assertEqual(self.run_git("status", "--porcelain"), "")
        # 解决后 merge commit 双父为 target、source；ff 策略下 target 是其祖先
        Path(wt, "a.txt").write_text("both\n")
        self.run_git("add", "--", "a.txt", repo=wt)
        self.run_git("commit", "-q", "--no-edit", repo=wt)
        self.assertEqual(self.run_git("rev-parse", "HEAD^1", "HEAD^2", repo=wt).split(), [t, s])
        m = self.run_git("rev-parse", "HEAD", repo=wt)
        self.assertTrue(g.is_ancestor(self.repo, t, m))

    def test_start_merge_error_is_not_conflict(self):
        base, s, t = self.make_graph()
        wt = g.make_worktree(self.repo, t)
        res = g.start_merge(wt, "0" * 40)
        self.assertEqual(res["state"], "error")

    def test_diagnostics_forced_english(self):
        base, s, t = self.make_graph(conflict=True)
        wt = g.make_worktree(self.repo, t)
        with patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8", "LANGUAGE": "zh_CN"}):
            self.assertEqual(g.start_merge(wt, s)["state"], "conflict")

    def test_single_commit_migration_keeps_single_parent(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        self.branch_from("target", base)
        self.commit({"old.txt": "ancestor\n"}, "ancestor")
        s = self.commit({"new.txt": "selected\n"}, "selected")
        wt = g.make_worktree(self.repo, base, "migrate/x")
        self.run_git("cherry-pick", s, repo=wt)
        self.assertEqual(self.run_git("rev-list", "--count", f"{base}..HEAD", repo=wt), "1")
        self.assertEqual(self.run_git("rev-parse", "HEAD^", repo=wt), base)
        self.assertFalse(Path(wt, "old.txt").exists())

    def test_fetch_target_and_remote_head(self):
        origin = str(Path(self.tmp.name) / "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        base = self.commit({"a.txt": "base\n"}, "base")
        self.run_git("remote", "add", "origin", origin)
        self.run_git("push", "-q", "origin", "main:refs/heads/test")
        self.assertEqual(g.fetch_target(self.repo, "test"), base)
        self.assertEqual(g.remote_head(self.repo, "test"), base)
        self.assertIsNone(g.remote_head(self.repo, "nope"))
        self.assertTrue(g.branch_unused(self.repo, "merge/new-branch"))
        self.assertFalse(g.branch_unused(self.repo, "main"))
        # 拒绝的名称不触发 fetch
        with self.assertRaises(ValueError):
            g.fetch_target(self.repo, "@{-1}")
        # 推送只用普通 refspec，远端领先时 fast-forward 判断
        self.run_git("push", "-q", "origin", f"{base}:refs/heads/feat")
        ahead = self.commit({"a.txt": "ahead\n"}, "ahead")
        self.run_git("push", "-q", "origin", f"{ahead}:refs/heads/feat")
        self.branch_from("local-base", base)
        self.assertFalse(g.is_ancestor(self.repo, ahead, base))  # 远端领先 → 必须暂停


# ------------------------------------------------------------- mock API ----

class MockState:
    def __init__(self):
        self.mrs = []
        self.post_mode = "ok"       # ok | drop(先建后断开) | drop_nocreate | conflict409 | status401 | redirect
        self.posts = 0
        self.tokens = []
        self.sha_override = None
        self.omit_sha = False
        self.pid = 42


def make_handler(state, project_path):
    enc = urllib.parse.quote(project_path, safe="")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, headers=None):
            body = json.dumps(obj if obj is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _mr_view(self, mr):
            v = dict(mr)
            if state.sha_override:
                v["sha"] = state.sha_override
            if state.omit_sha:
                v.pop("sha", None)
            return v

        def do_GET(self):
            state.tokens.append(self.headers.get("PRIVATE-TOKEN"))
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = urllib.parse.unquote(u.path)
            if u.path == f"/api/v4/projects/{enc}":
                return self._send(200, {"id": state.pid, "path_with_namespace": project_path,
                                        "merge_method": "ff"})
            if path == f"/api/v4/projects/{state.pid}/merge_requests":
                items = [m for m in state.mrs if m["state"] == q["state"][0]
                         and m["source_branch"] == q["source_branch"][0]
                         and m["target_branch"] == q["target_branch"][0]]
                page, per = int(q["page"][0]), 2  # 强制小页，覆盖翻页
                chunk = items[(page - 1) * per: page * per]
                nxt = str(page + 1) if page * per < len(items) else ""
                return self._send(200, [self._mr_view(m) for m in chunk], {"X-Next-Page": nxt})
            m = re.fullmatch(rf"/api/v4/projects/{state.pid}/merge_requests/(\d+)", path)
            if m:
                for mr in state.mrs:
                    if mr["iid"] == int(m[1]):
                        return self._send(200, self._mr_view(mr))
            self._send(404, {"message": "404 Not Found"})

        def do_POST(self):
            state.tokens.append(self.headers.get("PRIVATE-TOKEN"))
            state.posts += 1
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if state.post_mode == "status401":
                return self._send(401, {"message": "401 Unauthorized token=abc"})
            if state.post_mode == "redirect":
                return self._send(302, {}, {"Location": "http://evil.invalid/steal"})
            if state.post_mode == "conflict409":
                return self._send(409, {"message": "Another open merge request already exists"})
            mr = self._new_mr(body)
            if state.post_mode in ("drop", "drop_nocreate"):
                if state.post_mode == "drop_nocreate":
                    state.mrs.pop()
                self.connection.close()  # 不返回响应 → 客户端遇到断连
                return
            self._send(201, mr)

        def _new_mr(self, body):
            iid = len(state.mrs) + 1
            mr = {"iid": iid, "state": "opened", "source_branch": body["source_branch"],
                  "target_branch": body["target_branch"], "source_project_id": state.pid,
                  "target_project_id": state.pid, "sha": state.expected_sha,
                  "web_url": f"http://127.0.0.1:{self.server.server_port}/{project_path}/-/merge_requests/{iid}",
                  "title": body["title"], "description": body["description"],
                  "remove_source_branch": body.get("remove_source_branch")}
            state.mrs.append(mr)
            return mr

    return H


class ApiTests(unittest.TestCase):
    PROJECT = "group/sub/proj"
    SHA = "c" * 40

    def setUp(self):
        self.state = MockState()
        self.state.expected_sha = self.SHA
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state, self.PROJECT))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}/api/v4"
        self.client = g.GitLabClient(self.base, self.PROJECT, "tok-SECRET", timeout=3)

    def add_mr(self, **kw):
        iid = len(self.state.mrs) + 1
        mr = {"iid": iid, "state": "opened", "source_branch": "s", "target_branch": "t",
              "source_project_id": 42, "target_project_id": 42, "sha": self.SHA,
              "web_url": f"{self.base.removesuffix('/api/v4')}/{self.PROJECT}/-/merge_requests/{iid}"}
        mr.update(kw)
        self.state.mrs.append(mr)

    def ensure(self, **kw):
        return self.client.ensure_mr("s", "t", self.SHA, "标题", "描述", **kw)

    def test_ca_appended_to_default_trust(self):
        context = Mock()
        with patch.object(g.ssl, "create_default_context", return_value=context) as create:
            g.GitLabClient(self.base, self.PROJECT, "tok", ca_file="enterprise.pem")
        create.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile="enterprise.pem")

    def test_invalid_expected_sha_stops_before_api(self):
        for sha in ("abc123", " " + self.SHA, "z" * 40):
            with self.subTest(sha=sha), patch.object(self.client, "request") as request:
                with self.assertRaises(ValueError):
                    self.client.ensure_mr("s", "t", sha, "title", "desc")
                request.assert_not_called()

    def test_canonical_project_and_default_port(self):
        client = g.GitLabClient("https://EXAMPLE.invalid:443/api/v4", "Group/Proj", "fake")
        with patch.object(client, "request", return_value=(200, {}, {"id": 42, "path_with_namespace": "group/proj"})):
            client.project()
        mr = {"iid": 1, "web_url": "https://example.invalid/group/proj/-/merge_requests/1",
              "state": "opened", "source_branch": "s", "target_branch": "t",
              "source_project_id": 42, "target_project_id": 42, "sha": self.SHA}
        self.assertEqual(client.verify_mr(mr, "s", "t", self.SHA), [])

    def test_unverified_creation_does_not_publish_foreign_link(self):
        original = self.client.request
        def request(method, *args, **kw):
            result = original(method, *args, **kw)
            if method == "POST":
                result[2]["web_url"] = "https://foreign.invalid/steal"
            return result
        with patch.object(self.client, "request", side_effect=request), patch.object(
                self.client, "get_mr", side_effect=g.ApiTransportError("timeout")):
            result = self.ensure()
        self.assertEqual(result["status"], "created_unverified")
        self.assertEqual(result["mr"]["iid"], 1)
        self.assertIsNone(result["mr"]["web_url"])
        self.assertEqual(self.state.posts, 1)

    def test_missing_token(self):
        with self.assertRaises(g.ApiError):
            g.GitLabClient(self.base, self.PROJECT, None)

    def test_project_info_and_path_check(self):
        p = self.client.project()
        self.assertEqual((p["id"], p["merge_method"]), (42, "ff"))
        bad = g.GitLabClient(self.base, "group/sub/other", "tok", timeout=3)
        with self.assertRaises(g.ApiError):
            bad.project()  # 404

    def test_create_when_absent_with_safe_defaults(self):
        r = self.ensure()
        self.assertEqual(r["status"], "created")
        mr = self.state.mrs[0]
        self.assertIs(mr["remove_source_branch"], False)
        self.assertEqual(mr["title"], "标题")
        self.assertEqual(self.state.posts, 1)

    def test_reuse_with_pagination_and_no_post(self):
        for _ in range(5):  # 5 个同源同目标 MR，页大小 2 → 3 页；复用第一个
            self.add_mr()
        r = self.ensure()
        self.assertEqual((r["status"], r["duplicates"]), ("reused", 4))
        self.assertEqual(self.state.posts, 0)

    def test_check_only_never_posts(self):
        self.assertEqual(self.ensure(create=False)["status"], "none")
        self.assertEqual(self.state.posts, 0)

    def test_mismatch_sha_is_not_reused_or_recreated(self):
        self.add_mr(sha="d" * 40)
        r = self.ensure()
        self.assertEqual(r["status"], "mismatch")
        self.assertTrue(any("sha 不符" in p for p in r["problems"]))
        self.assertEqual(self.state.posts, 0)

    def test_missing_sha_field(self):
        self.add_mr()
        self.state.omit_sha = True
        r = self.ensure()
        self.assertEqual(r["status"], "mismatch")
        self.assertTrue(any("缺少 sha" in p for p in r["problems"]))

    def test_wrong_project_ids_and_foreign_url(self):
        self.add_mr(source_project_id=7)
        self.assertEqual(self.ensure()["status"], "mismatch")
        self.state.mrs.clear()
        self.add_mr(web_url="http://h/other/proj/-/merge_requests/1")
        self.assertTrue(any("web_url" in p for p in self.ensure()["problems"]))

    def test_foreign_or_unsafe_url_not_verified(self):
        root = self.base.removesuffix('/api/v4')
        for url in (f"https://foreign.invalid/{self.PROJECT}/-/merge_requests/1",
                    f"https://127.0.0.1:{self.server.server_port}/{self.PROJECT}/-/merge_requests/1",
                    f"http://user@127.0.0.1:{self.server.server_port}/{self.PROJECT}/-/merge_requests/1",
                    f"{root}/extra/{self.PROJECT}/-/merge_requests/1"):
            with self.subTest(url=url):
                self.state.mrs.clear()
                self.add_mr(web_url=url)
                self.assertEqual(self.ensure()["status"], "mismatch")
                self.assertEqual(self.state.posts, 0)

    def test_created_but_detail_request_fails(self):
        for error in (g.ApiTransportError("detail timeout"), g.ApiError(403, "Forbidden")):
            with self.subTest(error=str(error)):
                self.state.mrs.clear()
                self.state.posts = 0
                with patch.object(self.client, "get_mr", side_effect=error):
                    result = self.ensure()
                self.assertEqual(result["status"], "created_unverified")
                self.assertEqual(result["mr"]["iid"], 1)
                self.assertEqual(result["mr"]["web_url"], self.state.mrs[0]["web_url"])
                self.assertTrue(result["problems"])
                self.assertEqual(self.state.posts, 1)

    def test_created_but_detail_sha_wrong(self):
        self.state.sha_override = "e" * 40
        r = self.ensure()
        self.assertEqual(r["status"], "created_unverified")
        self.assertEqual(self.state.posts, 1)

    def test_post_connection_dropped_after_create_requeries_not_repost(self):
        self.state.post_mode = "drop"
        r = self.ensure()
        self.assertEqual((r["status"], r.get("after_uncertain_post")), ("reused", True))
        self.assertEqual(self.state.posts, 1)

    def test_post_dropped_and_nothing_created_is_unknown(self):
        self.state.post_mode = "drop_nocreate"
        r = self.ensure()
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(self.state.posts, 1)

    def test_409_requeries(self):
        self.state.post_mode = "conflict409"
        self.assertEqual(self.ensure()["status"], "unknown")
        self.add_mr()
        self.state.mrs[0]["sha"] = self.SHA
        self.assertEqual(self.ensure()["status"], "reused")  # 已存在时根本不会 POST

    def test_401_stops_without_retry_and_leaks_no_token(self):
        self.state.post_mode = "status401"
        with self.assertRaises(g.ApiError) as cm:
            self.ensure()
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(self.state.posts, 1)
        self.assertNotIn("tok-SECRET", str(cm.exception))

    def test_redirect_refused_and_token_not_forwarded(self):
        self.state.post_mode = "redirect"
        with self.assertRaises(g.ApiError):
            self.ensure()
        self.assertEqual(self.state.posts, 1)

    def test_title_regex_enforced_before_post(self):
        with self.assertRaises(g.ApiError):
            self.client.ensure_mr("s", "t", self.SHA, "无前缀", "d", title_regex=r"^\[[A-Z]+-\d+\]")
        self.assertEqual(self.state.posts, 0)
        self.assertEqual(self.client.ensure_mr("s", "t", self.SHA, "[AB-1] ok", "d",
                                               title_regex=r"^\[[A-Z]+-\d+\]")["status"], "created")

    def test_token_header_sent_on_every_request(self):
        self.client.project()
        self.ensure()
        self.assertTrue(self.state.tokens)
        self.assertTrue(all(t == "tok-SECRET" for t in self.state.tokens))


class CliTests(GitRepoCase):
    def resolve_selected(self, target, source, commits):
        # 为测试固定分支与跟踪引用，模拟 preflight 返回的命名快照。
        self.run_git("update-ref", "refs/heads/feat/demo", source)
        self.run_git("update-ref", "refs/remotes/origin/dev", target)
        return g.resolve(self.repo, target, source, commits,
                         source_branch="feat/demo", target_branch="dev")

    def test_resolve_name_collision_does_not_create_worktree(self):
        source, target = self.merge_graph()
        with patch.object(g, "branch_unused", return_value=False), patch.object(
                g, "make_worktree") as wt, self.assertRaises(ValueError):
            self.resolve_selected(target, source, [source])
        wt.assert_not_called()

    def test_resolve_rejects_changed_named_source_before_worktree(self):
        source, target = self.merge_graph()
        with patch.object(g, "make_worktree") as wt, self.assertRaises(ValueError):
            g.resolve(self.repo, target, source, [source], source_branch="target", target_branch="dev")
        wt.assert_not_called()

    def test_resolve_rejects_changed_target_snapshot(self):
        source, target = self.merge_graph()
        self.run_git("update-ref", "refs/remotes/origin/dev", source)
        with patch.object(g, "make_worktree") as wt, self.assertRaises(ValueError):
            g.resolve(self.repo, target, source, [source], source_branch="main", target_branch="dev")
        wt.assert_not_called()

    def test_resolve_cli_requires_branch_names_before_worktree(self):
        source, target = self.merge_graph()
        with patch.object(g, "make_worktree") as wt, patch("sys.stderr"):
            code = g.main(["resolve", "--repo", self.repo, "--source-sha", source,
                           "--target-sha", target, "--commits", source])
        self.assertEqual(code, 1)
        wt.assert_not_called()

    def start_merge_cli(self, wt, source, *extra):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / "gitlab_mr.py"), "start-merge",
             "--repo", wt, "--source-sha", source, *extra], capture_output=True, text=True)

    def merge_graph(self, conflict=False):
        base = self.commit({"a.txt": "base\n"}, "base")
        self.branch_from("target", base)
        source = self.commit({"a.txt": "source\n"}, "source")
        self.run_git("checkout", "-q", "target")
        target = self.commit({"a.txt": "target\n"} if conflict else {"b.txt": "target\n"}, "target")
        return source, target

    def test_start_merge_cli_clean_and_tree_match(self):
        source, target = self.merge_graph()
        _, _, tree = g.preview_merge(self.repo, target, source)
        # 每次使用独立 worktree，避免复用已开始的 merge 状态。
        for expected, matches, code in ((tree, True, 0), (TREE, False, 1)):
            with self.subTest(matches=matches):
                wt = g.make_worktree(self.repo, target)
                r = self.start_merge_cli(wt, source, "--expected-tree", expected)
                self.assertEqual(r.returncode, code, r.stderr)
                out = json.loads(r.stdout)
                self.assertEqual(out["state"], "clean")
                self.assertEqual(out["tree_matches"], matches)
                self.assertEqual(out["exit_code"], 0)
        self.assertEqual(self.run_git("rev-parse", "HEAD"), target)
        self.assertEqual(self.run_git("status", "--porcelain"), "")

    def test_start_merge_cli_conflict_is_expected(self):
        source, target = self.merge_graph(conflict=True)
        wt = g.make_worktree(self.repo, target)
        r = self.start_merge_cli(wt, source, "--expected-tree", TREE)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["state"], "conflict")
        self.assertEqual(out["exit_code"], 1)
        self.assertEqual(out["merge_head"], source)
        self.assertEqual(out["unmerged_paths"], ["a.txt"])
        self.assertNotIn("tree_matches", out)

    def test_start_merge_cli_error_is_not_conflict(self):
        source, target = self.merge_graph()
        wt = g.make_worktree(self.repo, target)
        r = self.start_merge_cli(wt, "0" * 40)
        self.assertEqual(r.returncode, 1, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["state"], "error")
        self.assertNotEqual(out["exit_code"], 0)
        self.assertNotIn("tree_matches", out)
        self.assertEqual(self.run_git("status", "--porcelain", repo=wt), "")

    def test_preflight_clean_does_not_create_worktree(self):
        source, target = self.merge_graph()
        with patch.object(g, "snapshot", return_value={"source_branch": "main", "source_sha": source,
                "problems": []}), patch.object(g, "fetch_target", return_value=target), patch.object(
                g, "make_worktree") as wt:
            out = g.preflight(self.repo, "target", check_only=True)
        self.assertEqual(out["status"], "clean")
        wt.assert_not_called()

    def test_resolve_only_selected_commits_and_preserves_original(self):
        source, target = self.merge_graph()
        with patch.object(g, "branch_unused", return_value=True):
            out = self.resolve_selected(target, source, [source])
        self.assertEqual(out["status"], "ready")
        self.assertRegex(out["mr_source_branch"], r"^resolve/feat/demo-to-dev-\d{8}-\d{6}$")
        self.assertEqual(out["source_branch"], "feat/demo")
        self.assertEqual(out["target_branch"], "dev")
        self.assertEqual(self.run_git("rev-parse", "HEAD", repo=out["worktree"]), out["expected_mr_sha"])
        self.assertEqual(self.run_git("rev-parse", "HEAD^", repo=out["worktree"]), target)
        self.assertEqual(self.run_git("rev-parse", "HEAD"), target)

    def test_resolve_conflict_and_continue(self):
        source, target = self.merge_graph(conflict=True)
        with patch.object(g, "branch_unused", return_value=True):
            out = self.resolve_selected(target, source, [source])
        self.assertEqual(out["status"], "conflict")
        wt = out["worktree"]
        Path(wt, "a.txt").write_text("both\n")
        self.run_git("add", "--", "a.txt", repo=wt)
        continued = g.continue_resolve(wt)
        self.assertEqual(continued["status"], "ready")
        self.assertEqual(continued["mr_source_branch"], out["mr_source_branch"])

    def test_deliver_remote_divergence_never_pushes(self):
        source, target = self.merge_graph()
        with patch.object(g, "fetch_target", side_effect=[target, target]), patch.object(
                g, "remote_head", return_value=target), patch.object(g, "client_from_repo") as api:
            out = g.deliver(self.repo, "main", source, "target", target, "title", "description")
        self.assertEqual(out["status"], "remote_diverged")
        api.assert_not_called()

    def test_resolve_multiple_selected_commits_in_topological_order(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        first = self.commit({"a.txt": "first\n"}, "first")
        second = self.commit({"a.txt": "second\n"}, "second")
        with patch.object(g, "branch_unused", return_value=True):
            out = self.resolve_selected(base, second, [second, first])
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["selected_commits"], [first, second])
        self.assertEqual(self.run_git("rev-list", "--count", f"{base}..HEAD", repo=out["worktree"]), "2")

    def test_resolve_merge_commit_rejected_before_worktree(self):
        source, target = self.merge_graph()
        self.run_git("merge", "--no-ff", "--no-edit", source)
        merge = self.run_git("rev-parse", "HEAD")
        with patch.object(g, "make_worktree") as wt, self.assertRaises(ValueError):
            self.resolve_selected(target, merge, [merge])
        wt.assert_not_called()

    def test_deliver_new_target_conflict_never_pushes(self):
        source, target = self.merge_graph(conflict=True)
        with patch.object(g, "fetch_target", return_value=target), patch.object(g, "remote_head") as remote:
            out = g.deliver(self.repo, "main", source, "target", "a" * 40, "t", "d")
        self.assertEqual(out["status"], "target_changed")
        remote.assert_not_called()

    def test_deliver_reuses_remote_without_push(self):
        source, target = self.merge_graph()
        client = Mock()
        client.project.return_value = {"merge_method": "merge"}
        client.ensure_mr.return_value = {"status": "reused"}
        with patch.object(g, "fetch_target", return_value=target), patch.object(
                g, "remote_head", return_value=source), patch.object(
                g, "client_from_repo", return_value=(client, {})), patch.object(g, "git_text", wraps=g.git_text) as run:
            out = g.deliver(self.repo, "main", source, "target", target, "t", "d")
        self.assertEqual(out["status"], "reused")
        self.assertFalse(any(c.args[1] == "push" for c in run.call_args_list))

    def test_deliver_pushes_to_local_remote_then_creates_mr(self):
        source, target = self.merge_graph()
        origin = str(Path(self.tmp.name) / "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        self.run_git("remote", "add", "origin", origin)
        self.run_git("push", "-q", "origin", "target")
        client = Mock()
        client.project.return_value = {"merge_method": "merge"}
        client.ensure_mr.return_value = {"status": "created", "mr": {"web_url": "mock"}}
        with patch.object(g, "client_from_repo", return_value=(client, {})):
            out = g.deliver(self.repo, "main", source, "target", target, "t", "d")
        self.assertEqual(out["status"], "created")
        self.assertEqual(g.remote_head(self.repo, "main"), source)
        self.assertEqual(g.remote_head(self.repo, "target"), target)
        client.ensure_mr.assert_called_once()

    def equivalent_graph(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        gateway = self.commit({"gateway.txt": "fix\n"}, "gateway")
        source = self.commit({"frontend.txt": "modal\n"}, "frontend")
        self.run_git("checkout", "-q", "-b", "target", base)
        target = self.commit({"gateway.txt": "fix\n"}, "gateway integrated with new SHA")
        return base, gateway, source, target

    def test_candidate_classification_excludes_equivalent_patch(self):
        _, gateway, source, target = self.equivalent_graph()
        out = g.classify_candidates(self.repo, target, source)
        self.assertEqual([c["sha"] for c in out["pending"]], [source])
        self.assertEqual(out["excluded"][0]["sha"], gateway)
        self.assertEqual(out["excluded"][0]["reason"], "patch_equivalent")

    def test_candidate_display_truncates_after_full_classification(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        for i in range(12):
            source = self.commit({f"f{i}.txt": str(i)}, f"change {i}")
        with patch.object(g, "snapshot", return_value={"source_branch": "main", "source_sha": source,
                "problems": []}), patch.object(g, "fetch_target", return_value=base), patch.object(
                g, "preview_merge", return_value=("conflict", [], TREE)):
            out = g.preflight(self.repo, "target", check_only=True)
        self.assertEqual(len(out["candidates"]["pending"]), 10)
        self.assertEqual(out["candidates"]["pending_count"], 12)
        self.assertEqual(out["candidates"]["omitted_pending"], 2)

    def test_candidate_classification_rejects_invalid_output(self):
        with patch.object(g, "git_text", return_value="unexpected"), self.assertRaises(g.GitError):
            g.classify_candidates(self.repo, TREE, TREE)

    def test_conflict_preflight_has_no_raw_commit_list(self):
        _, gateway, source, target = self.equivalent_graph()
        with patch.object(g, "snapshot", return_value={"source_branch": "main", "source_sha": source,
                "problems": []}), patch.object(g, "fetch_target", return_value=target), patch.object(
                g, "preview_merge", return_value=("conflict", [], TREE)):
            out = g.preflight(self.repo, "target", check_only=True)
        self.assertNotIn("recent_commits", out["scope"])
        self.assertNotIn("commit_count", out["scope"])
        self.assertEqual([c["sha"] for c in out["candidates"]["pending"]], [source])
        self.assertEqual(out["candidates"]["excluded"][0]["sha"], gateway)

    def test_equivalent_history_reverted_still_requires_explicit_review(self):
        _, gateway, source, target = self.equivalent_graph()
        self.run_git("revert", "--no-edit", target)
        reverted = self.run_git("rev-parse", "HEAD")
        with patch.object(g, "make_worktree") as wt:
            out = self.resolve_selected(reverted, source, [gateway])
        self.assertEqual(out["status"], "equivalent_selected")
        wt.assert_not_called()

    def test_selected_ancestor_rejected_before_worktree(self):
        base, _, source, target = self.equivalent_graph()
        with patch.object(g, "make_worktree") as wt, self.assertRaises(ValueError):
            self.resolve_selected(target, source, [base])
        wt.assert_not_called()

    def test_equivalent_pick_stops_before_worktree(self):
        base = self.commit({"a.txt": "base\n"}, "base")
        source = self.commit({"b.txt": "same\n"}, "source")
        self.run_git("checkout", "-q", "-b", "target", base)
        target = self.commit({"b.txt": "same\n"}, "equivalent")
        with patch.object(g, "make_worktree") as wt:
            out = self.resolve_selected(target, source, [source])
        self.assertEqual(out["status"], "equivalent_selected")
        self.assertEqual(out["excluded"][0]["sha"], source)
        wt.assert_not_called()

    def test_file_errors_are_json(self):
        with patch.object(g, "client_from_repo", side_effect=FileNotFoundError("missing CA")), patch(
                "sys.stderr") as stderr:
            code = g.main(["project-info"])
        self.assertEqual(code, 1)
        value = json.loads(stderr.write.call_args_list[0].args[0])
        self.assertIn("missing CA", value["error"])

    def test_prepare_stops_at_scope_gate(self):
        origin = str(Path(self.tmp.name) / "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        base = self.commit({"a.txt": "base\n"}, "base")
        self.run_git("remote", "add", "origin", origin)
        self.run_git("push", "-q", "origin", "main:refs/heads/test")
        self.run_git("checkout", "-q", "-b", "feat/x")
        self.commit({"ui/a.txt": "n\n"}, "feat")
        # 本地路径 origin 无法解析为 GitLab 项目 → snapshot 报问题并阻断，不继续 fetch
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "gitlab_mr.py"), "prepare", "--repo", self.repo,
                            "--target-branch", "test"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertTrue(out["blocked"])
        self.assertTrue(any("origin 无法确认" in p for p in out["snapshot"]["problems"]))

    def test_cli_rejects_reflog_style_target_before_fetch(self):
        self.commit({"a.txt": "1\n"}, "c")
        self.run_git("remote", "add", "origin", "https://gitlab.example.invalid/g/p.git")
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "gitlab_mr.py"), "prepare", "--repo", self.repo,
                            "--target-branch", "@{-1}"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error", json.loads(r.stderr))


# ------------------------------------------------------------ 文档一致性 ----

ROOT = Path(__file__).resolve().parents[1]


class DocConsistencyTests(unittest.TestCase):
    def md_files(self):
        return [*sorted(ROOT.glob("*.md")), *sorted((ROOT / "references").glob("*.md"))]

    def test_relative_links_resolve(self):
        for f in self.md_files():
            for target in re.findall(r"\]\((?!https?:)([^)#]+)(?:#[^)]*)?\)", f.read_text(encoding="utf-8")):
                with self.subTest(file=f.name, link=target):
                    self.assertTrue((f.parent / target).exists(), target)

    def test_frontmatter(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        m = re.match(r"---\nname: (.+)\ndescription: \"(.+)\"\ncompatibility: \"(.+)\"\n---\n", text)
        self.assertIsNotNone(m)
        self.assertEqual(m[1], "create-gitlab-mr")
        self.assertLessEqual(len(m[2]), 1024)

    def test_cli_subcommands_documented_and_real(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        script = (SCRIPTS / "gitlab_mr.py").read_text(encoding="utf-8")
        declared = re.search(r"for name in \(([^)]*)\):", script)[1]
        names = re.findall(r'"([a-z\-]+)"', declared)
        self.assertGreaterEqual(len(names), 7)
        for n in names:
            with self.subTest(cmd=n):
                docs = skill + (ROOT / "使用说明.md").read_text(encoding="utf-8")
                self.assertIn(f"`{n}", docs)  # 每个子命令都在 SKILL.md 中出现

    def test_script_functions_referenced_exist(self):
        text = "".join(f.read_text(encoding="utf-8") for f in self.md_files())
        for fn in set(re.findall(r"`((?:start_merge|worktree_tree_matches|preview_merge|validate_branch|"
                                 r"is_ancestor|remote_head|parse_remote|redact_text|build_description|"
                                 r"policy_requires_target_ancestor|StepTimer|GitLabClient|NoRedirect)\b)", text)):
            with self.subTest(fn=fn):
                self.assertTrue(hasattr(g, fn), fn)

    def test_evals_well_formed(self):
        data = json.loads((ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        ids = [e["id"] for e in data["evals"]]
        self.assertEqual(len(ids), len(set(ids)))
        for e in data["evals"]:
            with self.subTest(id=e["id"]):
                self.assertTrue(e["prompt"] and e["expected_output"])
                self.assertGreaterEqual(len(e["assertions"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
