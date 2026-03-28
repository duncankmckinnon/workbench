"""Tests for workbench.tmux module."""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from workbench.tmux import _sanitize_session_name, check_tmux_available, run_in_tmux


# ---------------------------------------------------------------------------
# check_tmux_available
# ---------------------------------------------------------------------------

class TestCheckTmuxAvailable:
    def test_returns_true_when_tmux_found(self):
        with patch("workbench.tmux.shutil.which", return_value="/usr/bin/tmux"):
            assert check_tmux_available() is True

    def test_returns_false_when_tmux_not_found(self):
        with patch("workbench.tmux.shutil.which", return_value=None):
            assert check_tmux_available() is False

    def test_calls_which_with_tmux(self):
        with patch("workbench.tmux.shutil.which") as mock_which:
            check_tmux_available()
            mock_which.assert_called_once_with("tmux")


# ---------------------------------------------------------------------------
# _sanitize_session_name
# ---------------------------------------------------------------------------

class TestSanitizeSessionName:
    def test_replaces_slashes(self):
        assert _sanitize_session_name("a/b/c") == "a-b-c"

    def test_replaces_spaces(self):
        assert _sanitize_session_name("my session") == "my-session"

    def test_replaces_both(self):
        assert _sanitize_session_name("path/to my/session") == "path-to-my-session"

    def test_replaces_colons(self):
        assert _sanitize_session_name("host:port") == "host-port"

    def test_strips_leading_dots(self):
        assert _sanitize_session_name(".hidden") == "hidden"

    def test_no_change_needed(self):
        assert _sanitize_session_name("clean-name") == "clean-name"

    def test_empty_string(self):
        assert _sanitize_session_name("") == ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_process(returncode=0):
    """Create a mock asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    return proc


def _setup_tmpdir():
    """Create a real temp dir and return (tmpdir_path, exitcode_path, output_path)."""
    d = tempfile.mkdtemp(prefix="wb-test-")
    return d, os.path.join(d, "exitcode"), os.path.join(d, "output.txt")


# ---------------------------------------------------------------------------
# run_in_tmux – success path
# ---------------------------------------------------------------------------

class TestRunInTmuxSuccess:
    @pytest.mark.asyncio
    async def test_returns_exitcode_and_output(self, tmp_path):
        real_tmpdir, exitcode_file, output_file = _setup_tmpdir()

        # Pre-write results before patching
        with open(exitcode_file, "w") as f:
            f.write("0")
        with open(output_file, "w") as f:
            f.write("hello world\n")

        async def fake_exec(*args, **kwargs):
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            rc, output = await run_in_tmux(
                "test-session",
                ["echo", "hello", "world"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert rc == 0
        assert output == "hello world\n"

    @pytest.mark.asyncio
    async def test_creates_kill_and_new_session(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()
        with open(exitcode_file, "w") as f:
            f.write("0")

        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "my-session",
                ["ls"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        # Should have: kill-session (stale), new-session, kill-session (cleanup)
        tmux_cmds = [c for c in calls if c[0] == "tmux"]
        assert len(tmux_cmds) == 3
        assert tmux_cmds[0][1] == "kill-session"  # stale cleanup
        assert tmux_cmds[1][1] == "new-session"   # create session
        assert tmux_cmds[2][1] == "kill-session"   # final cleanup

    @pytest.mark.asyncio
    async def test_writes_run_script(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()

        script_content = None

        async def fake_exec(*args, **kwargs):
            nonlocal script_content
            if "new-session" in args:
                # Capture the script content before it gets cleaned up
                script_path = os.path.join(real_tmpdir, "run.sh")
                if os.path.exists(script_path):
                    with open(script_path) as f:
                        script_content = f.read()
                # Now write the exitcode to simulate completion
                with open(exitcode_file, "w") as f:
                    f.write("0")
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "test",
                ["echo", "hello"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert script_content is not None
        assert "#!/usr/bin/env bash" in script_content
        assert "echo hello" in script_content
        assert "output.txt" in script_content
        assert "exitcode" in script_content

    @pytest.mark.asyncio
    async def test_script_is_executable(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()
        script_mode = None

        async def fake_exec(*args, **kwargs):
            nonlocal script_mode
            if "new-session" in args:
                script_path = os.path.join(real_tmpdir, "run.sh")
                if os.path.exists(script_path):
                    script_mode = os.stat(script_path).st_mode
                with open(exitcode_file, "w") as f:
                    f.write("0")
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "test",
                ["ls"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert script_mode is not None
        assert script_mode & 0o755 == 0o755

    @pytest.mark.asyncio
    async def test_sanitizes_session_name(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()
        with open(exitcode_file, "w") as f:
            f.write("0")

        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "path/to session",
                ["ls"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        # Check that the sanitized name was used
        kill_calls = [c for c in calls if c[0] == "tmux" and c[1] == "kill-session"]
        assert all("path-to-session" in c for c in kill_calls)
        new_calls = [c for c in calls if c[0] == "tmux" and c[1] == "new-session"]
        assert all("path-to-session" in c for c in new_calls)

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self, tmp_path):
        real_tmpdir, exitcode_file, output_file = _setup_tmpdir()
        with open(exitcode_file, "w") as f:
            f.write("42")
        with open(output_file, "w") as f:
            f.write("some error\n")

        async def fake_exec(*args, **kwargs):
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            rc, output = await run_in_tmux(
                "test",
                ["false"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert rc == 42
        assert output == "some error\n"


# ---------------------------------------------------------------------------
# run_in_tmux – timeout
# ---------------------------------------------------------------------------

class TestRunInTmuxTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, tmp_path):
        real_tmpdir, _, _ = _setup_tmpdir()
        # Don't create exitcode file — simulates hanging command

        async def fake_exec(*args, **kwargs):
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            rc, output = await run_in_tmux(
                "test-timeout",
                ["sleep", "9999"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=0.05,
            )

        assert rc == 1
        assert "timeout" in output.lower()
        assert "0.05s" in output

    @pytest.mark.asyncio
    async def test_timeout_kills_session(self, tmp_path):
        real_tmpdir, _, _ = _setup_tmpdir()

        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "test-timeout",
                ["sleep", "9999"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=0.05,
            )

        # Should have: kill-session (stale), new-session, kill-session (timeout cleanup)
        kill_calls = [c for c in calls if c[0] == "tmux" and c[1] == "kill-session"]
        assert len(kill_calls) == 2  # stale + timeout

    @pytest.mark.asyncio
    async def test_timeout_cleans_up_tmpdir(self, tmp_path):
        real_tmpdir, _, _ = _setup_tmpdir()

        async def fake_exec(*args, **kwargs):
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "test",
                ["sleep", "9999"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=0.05,
            )

        assert not os.path.exists(real_tmpdir)


# ---------------------------------------------------------------------------
# run_in_tmux – edge cases
# ---------------------------------------------------------------------------

class TestRunInTmuxSessionCreateFailure:
    @pytest.mark.asyncio
    async def test_returns_error_on_session_create_failure(self, tmp_path):
        real_tmpdir, _, _ = _setup_tmpdir()
        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call is kill-session (stale), second is new-session
            if call_count == 2:
                return _make_mock_process(1)
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            rc, output = await run_in_tmux(
                "test",
                ["ls"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert rc == 1
        assert "tmux new-session failed" in output

    @pytest.mark.asyncio
    async def test_cleans_up_tmpdir_on_session_create_failure(self, tmp_path):
        real_tmpdir, _, _ = _setup_tmpdir()
        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return _make_mock_process(1)
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "test",
                ["ls"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert not os.path.exists(real_tmpdir)


class TestRunInTmuxExitcodeParsingEdgeCases:
    @pytest.mark.asyncio
    async def test_corrupted_exitcode_returns_1(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()
        with open(exitcode_file, "w") as f:
            f.write("not-a-number")

        async def fake_exec(*args, **kwargs):
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            rc, output = await run_in_tmux(
                "test",
                ["true"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert rc == 1


class TestRunInTmuxEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_output_file(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()
        with open(exitcode_file, "w") as f:
            f.write("0")
        # Don't create output.txt

        async def fake_exec(*args, **kwargs):
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            rc, output = await run_in_tmux(
                "test",
                ["true"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert rc == 0
        assert output == ""

    @pytest.mark.asyncio
    async def test_success_cleans_up_tmpdir(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()
        with open(exitcode_file, "w") as f:
            f.write("0")

        async def fake_exec(*args, **kwargs):
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "test",
                ["true"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        assert not os.path.exists(real_tmpdir)

    @pytest.mark.asyncio
    async def test_cwd_passed_to_new_session(self, tmp_path):
        real_tmpdir, exitcode_file, _ = _setup_tmpdir()
        with open(exitcode_file, "w") as f:
            f.write("0")

        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _make_mock_process(0)

        with patch("workbench.tmux.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("workbench.tmux.tempfile.mkdtemp", return_value=real_tmpdir):
            await run_in_tmux(
                "test",
                ["ls"],
                cwd=tmp_path,
                poll_interval=0.01,
                timeout=1.0,
            )

        new_session_call = [c for c in calls if "new-session" in c][0]
        c_idx = list(new_session_call).index("-c")
        assert new_session_call[c_idx + 1] == str(tmp_path)
