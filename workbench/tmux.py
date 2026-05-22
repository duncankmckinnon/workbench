"""Run agent commands inside tmux sessions for visibility and debugging."""

import asyncio
import os
import shlex
import shutil
import tempfile
from pathlib import Path


def check_tmux_available() -> bool:
    """Return True if tmux is on PATH."""
    return shutil.which("tmux") is not None


def _sanitize_session_name(name: str) -> str:
    """Replace characters that tmux doesn't allow in session names."""
    name = name.replace("/", "-").replace(" ", "-").replace(":", "-")
    return name.lstrip(".")


def _build_wrapper_script(
    cmd: list[str],
    output_file: str,
    exitcode_file: str,
    env: dict[str, str] | None = None,
) -> str:
    """Build the bash wrapper script, with optional exported env vars."""
    exports = ""
    if env:
        exports = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())
    return (
        "#!/usr/bin/env bash\n"
        f"{exports}"
        f"{shlex.join(cmd)} > {shlex.quote(output_file)} 2>&1\n"
        f"echo $? > {shlex.quote(exitcode_file)}\n"
    )


async def run_in_tmux(
    session_name: str,
    cmd: list[str],
    cwd: Path,
    poll_interval: float = 2.0,
    timeout: float = 1800.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a command in a named tmux session. Returns (returncode, stdout).

    Users can attach to watch progress: ``tmux attach -t <session_name>``
    """
    tmpdir = tempfile.mkdtemp(prefix="wb-")
    output_file = os.path.join(tmpdir, "output.txt")
    exitcode_file = os.path.join(tmpdir, "exitcode")

    script = _build_wrapper_script(cmd, output_file, exitcode_file, env)
    script_path = os.path.join(tmpdir, "run.sh")
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)

    safe_name = _sanitize_session_name(session_name)

    # Kill any stale session with the same name
    stale = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-session",
        "-t",
        safe_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await stale.wait()

    # Create a detached tmux session running the script
    create = await asyncio.create_subprocess_exec(
        "tmux",
        "new-session",
        "-d",
        "-s",
        safe_name,
        "-c",
        str(cwd),
        f"bash {shlex.quote(script_path)}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await create.wait()
    if create.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return (1, f"tmux new-session failed with code {create.returncode}")

    # Poll until exitcode file appears or timeout
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if os.path.exists(exitcode_file):
            break
        await asyncio.sleep(poll_interval)
    else:
        # Timeout – kill the session and clean up
        kill = await asyncio.create_subprocess_exec(
            "tmux",
            "kill-session",
            "-t",
            safe_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await kill.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)
        return (1, f"timeout after {timeout}s")

    # Read results
    try:
        with open(exitcode_file) as f:
            rc = int(f.read().strip())
    except (ValueError, OSError):
        rc = 1
    output_text = ""
    if os.path.exists(output_file):
        with open(output_file) as f:
            output_text = f.read()

    # Cleanup
    kill = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-session",
        "-t",
        safe_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await kill.wait()
    shutil.rmtree(tmpdir, ignore_errors=True)

    return (rc, output_text)
