"""沙箱上传：只读根文件系统下的 exec 兜底。

Docker 的 put_archive 对 --read-only 容器的任何路径都返回 400（含 tmpfs），
而 bootstrap 必须把项目传进 /tmp。以前这条冲突被 seccomp 解析失败掩盖
（basic 模式 → read_only 未生效），修好 seccomp 后上传直接崩、随后静默
回退到宿主机 LocalShell。这里锁住兜底路径的行为。
"""
import base64
import io
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

from src.agent.backends.custom_opensandbox import (
    CustomOpenSandbox,
    _UPLOAD_CHUNK_CHARS,
    _is_read_only_refusal,
)

READ_ONLY = '400 Client Error for http+docker://localhost/v1.51/containers/x/archive?path=%2Ftmp: Bad Request ("container rootfs is marked read-only")'


class FakeContainer:
    def __init__(self, error=None):
        self.error = error
        self.put_archive_calls = []

    def put_archive(self, path, data):
        self.put_archive_calls.append((path, data))
        if self.error is not None:
            raise self.error
        return True


class FakeExecResponse:
    def __init__(self, exit_code=0, output=b""):
        self.exit_code = exit_code
        self.output = output


def make_sandbox(container, exit_code=0):
    """绕过 __init__（它会连 Docker），只装测试需要的属性。"""
    sandbox = object.__new__(CustomOpenSandbox)
    sandbox._container = container
    sandbox.executed = []

    def fake_execute(command, **kwargs):
        sandbox.executed.append(command)
        return FakeExecResponse(exit_code=exit_code)

    sandbox.execute = fake_execute
    return sandbox


def decode_exec_payload(commands):
    """把 printf 分块拼回去，还原出 tar 字节。"""
    encoded = "".join(
        match.group(1)
        for match in (re.search(r"printf '%s' '([^']*)'", cmd) for cmd in commands)
        if match
    )
    return base64.b64decode(encoded)


def tar_bytes(name, content):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class ReadOnlyDetectionTests(unittest.TestCase):
    def test_detects_docker_read_only_refusal(self):
        self.assertTrue(_is_read_only_refusal(RuntimeError(READ_ONLY)))

    def test_detects_case_insensitively(self):
        self.assertTrue(_is_read_only_refusal(Exception("Container RootFS is marked READ-ONLY")))

    def test_ignores_other_errors(self):
        self.assertFalse(_is_read_only_refusal(Exception("connect: permission denied")))


class UploadFilesTests(unittest.TestCase):
    def test_read_only_refusal_falls_back_to_exec(self):
        container = FakeContainer(error=RuntimeError(READ_ONLY))
        sandbox = make_sandbox(container)

        results = sandbox.upload_files([("/tmp/erp-agent-project.tar.gz", b"hello sandbox")])

        self.assertEqual(results[0].error, None)
        self.assertEqual(len(container.put_archive_calls), 1)
        self.assertTrue(any(cmd.startswith("mkdir -p '/tmp'") for cmd in sandbox.executed))
        self.assertIn("base64 -d /tmp/.erp-upload.b64 | tar -x -C '/tmp'", sandbox.executed[-1])
        with tarfile.open(fileobj=io.BytesIO(decode_exec_payload(sandbox.executed))) as tar:
            self.assertEqual(tar.extractfile("erp-agent-project.tar.gz").read(), b"hello sandbox")

    def test_large_payload_is_chunked(self):
        container = FakeContainer(error=RuntimeError(READ_ONLY))
        sandbox = make_sandbox(container)
        payload = b"x" * (_UPLOAD_CHUNK_CHARS * 2)

        sandbox.upload_files([("/tmp/big.bin", payload)])

        printf_steps = [c for c in sandbox.executed if c.startswith("printf '%s' ")]
        self.assertGreaterEqual(len(printf_steps), 2)
        self.assertTrue(all(len(c) < _UPLOAD_CHUNK_CHARS + 64 for c in printf_steps))
        with tarfile.open(fileobj=io.BytesIO(decode_exec_payload(sandbox.executed))) as tar:
            self.assertEqual(tar.extractfile("big.bin").read(), payload)

    def test_each_exec_stays_under_max_arg_strlen(self):
        # 回归：曾把全部分块拼成一条命令 → "exec /usr/bin/bash: argument list too long"
        # 内核限制单个 argv 元素 128 KiB（MAX_ARG_STRLEN）。
        container = FakeContainer(error=RuntimeError(READ_ONLY))
        sandbox = make_sandbox(container)

        sandbox.upload_files([("/tmp/big.bin", b"x" * (_UPLOAD_CHUNK_CHARS * 3))])

        oversized = [cmd for cmd in sandbox.executed if len(cmd) >= 131072]
        self.assertEqual(oversized, [], "single docker exec argv element exceeded MAX_ARG_STRLEN")

    def test_other_put_archive_errors_are_not_retried(self):
        container = FakeContainer(error=RuntimeError("permission denied"))
        sandbox = make_sandbox(container)

        results = sandbox.upload_files([("/tmp/x.bin", b"data")])

        self.assertIn("permission denied", results[0].error)
        self.assertEqual(sandbox.executed, [])

    def test_exec_failure_is_reported(self):
        container = FakeContainer(error=RuntimeError(READ_ONLY))
        sandbox = make_sandbox(container, exit_code=1)

        results = sandbox.upload_files([("/tmp/x.bin", b"data")])

        self.assertIn("exec upload failed", results[0].error)

    def test_healthy_container_skips_exec_path(self):
        container = FakeContainer(error=None)
        sandbox = make_sandbox(container)

        results = sandbox.upload_files([("/tmp/ok.bin", b"data")])

        self.assertEqual(results[0].error, None)
        self.assertEqual(sandbox.executed, [])


class UploadDirectoryTests(unittest.TestCase):
    def test_read_only_refusal_extracts_tree_via_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            (root / "sub").mkdir(parents=True)
            (root / "AGENTS.md").write_text("hello", encoding="utf-8")
            (root / "sub" / "data.txt").write_text("42", encoding="utf-8")

            container = FakeContainer(error=RuntimeError(READ_ONLY))
            sandbox = make_sandbox(container)
            status = sandbox.upload_directory(str(root), "/workspace")

        self.assertTrue(status.startswith("OK: uploaded"), status)
        # upload_directory 用 arcname="." 且 gzip 压缩，成员名是 "\\./AGENTS.md" 这种形式。
        payload = decode_exec_payload(sandbox.executed)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
            members = {m.name.lstrip("./"): m for m in tar.getmembers()}
            self.assertEqual(tar.extractfile(members["AGENTS.md"]).read(), b"hello")
            self.assertEqual(tar.extractfile(members["sub/data.txt"]).read(), b"42")


if __name__ == "__main__":
    unittest.main()
