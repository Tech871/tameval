import time
import subprocess
from pathlib import Path
from dataclasses import dataclass


@dataclass
class RepoFailureError(Exception):
    """
    Either error in to some repo, or repo is not supported, or error in docker.

    Separate these errors from other errors that may be caused by bugs in eval-framework.
    Usually after RepoFailureError we can continue working with other repos.
    """

    message: str
    error_stage: str | None = None

    def __str__(self):
        return f"[{self.error_stage}] {self.message}"

    def __repr__(self):
        return f"RepoFailureError({self!s})"


class Timer(object):
    def __init__(self, timeout: float = 0):
        self.timeout = timeout
        self.is_active = False

    def __enter__(self):
        self.start_time = time.time()
        self.is_active = True
        return self

    def __exit__(self, type, value, traceback):
        self.end_time = time.time()
        self.is_active = False

    @property
    def elapsed_time(self) -> float:
        if self.is_active:
            return time.time() - self.start_time
        else:
            return self.end_time - self.start_time

    def get_remaining_time(self, raise_on_negative: bool = True) -> float:
        assert self.is_active, "use this object as context or run .start() first"
        if self.timeout == 0:
            return 0
        _remaining_time = round(self.start_time + self.timeout - time.time(), 2)
        if _remaining_time == 0:
            return 0.001
        elif _remaining_time < 0 and raise_on_negative:
            raise TimeoutError("negative time left in TimeoutClock")
        else:
            return _remaining_time


@dataclass(kw_only=True)
class CommandOutput:
    command: str | list[str]
    output: str
    exit_code: int
    was_timeout: bool | None = None


class CommandFailureError(RuntimeError):
    """
    For non-zero exit codes when running OS commands, or running non-existing OS commands.

    May contain CommandOutput for the command and cwd (current working directory).
    """

    def __init__(
        self,
        message: str | None = None,
        output: CommandOutput | None = None,
        cwd: str | Path | None = None,
    ):
        super().__init__(message)
        self.output = output
        self.cwd = cwd

    def __str__(self) -> str:
        if self.output:
            last_log_lines = "\\n".join(self.output.output.split("\n")[-5:])
            return (
                f"Command {self.output.command}"
                + (f' executed in dir "{self.cwd}"' if self.cwd is not None else "")
                + f" failed with exit code {self.output.exit_code}"
                + f", last log lines: {last_log_lines}"
            )
        else:
            return super().__str__()


class CommandTimeoutError(TimeoutError):
    """
    For timeout when running OS commands.

    May contain CommandOutput for the command and cwd (current working directory).
    """

    def __init__(
        self,
        message: str | None = None,
        output: CommandOutput | None = None,
        cwd: str | Path | None = None,
    ):
        super().__init__(message)
        self.output = output
        self.cwd = cwd

    def __str__(self) -> str:
        if self.output:
            last_log_lines = "\\n".join(self.output.output.split("\n")[-5:])
            return (
                f"Timeout for command {self.output.command}"
                + (f' executed in dir "{self.cwd}"' if self.cwd is not None else "")
                + f", last log lines: {last_log_lines}"
            )
        else:
            return super().__str__()


def run_command(
    command: list[str], cwd: str | Path | None = None, raise_on_error: bool = False
) -> CommandOutput:
    """
    Runs `command` with `subprocess.run` and converts the results into `CommandOutput`
    that is designed to be seiralizable.

    If raise_on_error, CommandFailureError raised on errors (no command or non-zero exit code).
    Otherwise `CommandOutput` is returned. If `subprocess.run` itself raises an error
    (this happens, for example, for unexisting commands), then the CommandOutput's
    `.exit_code` will be set to 127.
    """
    try:
        completed_process: subprocess.CompletedProcess = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(cwd) if cwd else None,
        )

    except (OSError, ValueError) as e:
        # https://docs.python.org/3/library/subprocess.html#exceptions
        # usually file not found for unexisting commands
        if raise_on_error:
            raise CommandFailureError(
                message=(
                    f"subprocess.run failed to run command {command}"
                    + (f' executed in dir "{cwd}"' if cwd else "")
                    + f": {e}"
                )
            ) from e
        else:
            # imitate bash -c cmd return code 127
            return CommandOutput(
                command=command,
                output=f"subprocess.run failed to run command: {e}",
                exit_code=127,
            )

    output = CommandOutput(
        command=command,
        output=completed_process.stdout,
        exit_code=completed_process.returncode,
    )
    if raise_on_error and output.exit_code:
        raise CommandFailureError(output=output, cwd=cwd)
    return output
