import os
import time
import signal
import subprocess
from rich.status import Status
from rich.console import Console

console = Console()


class Runner:

    @staticmethod
    def run_command(command: str, cwd: str = None, timeout: int = 500):
        """
        Executes a shell command in a specified working directory and returns its output, error, and exit code.

        Parameters:
            command (str): The shell command to execute.
            cwd (str, optional): The working directory in which to execute the command. Defaults to None.

        Returns:
            tuple: A tuple containing the standard output ('stdout'), standard error ('stderr'), exit code ('exit_code'), and the time of the executed command ('command_start_time').
        """
        # Get the current time before running the test command, in milliseconds
        command_start_time = int(round(time.time() * 1000))

        # Ensure the command is executed with shell=True for string commands
        display_cmd = command.split("sh -c")[-1][:100]
        try:
            with Status(
                status=f"[cyan]Running command:[dim]{display_cmd}...",
                console=console,
                spinner="line",
                refresh_per_second=10,
            ):

                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )

            # Return a dictionary with the desired information
            return result.stdout, result.stderr, result.returncode, command_start_time
        except subprocess.TimeoutExpired:
            print(
                f"Stop running due to timeout! Current timeout is {timeout}s.",
                flush=True,
            )
            return (
                f"Stop running test due to timeout! Current timeout is {timeout}s.",
                "",
                "timeout",
                command_start_time,
            )

    @staticmethod
    def run_and_save_output(
        command: str, file_path: str, cwd: str = None, timeout: int = 500
    ):
        command = command.format(timeout=timeout)
        display_cmd = command.split("sh -c")[-1][:100]
        command_start_time = int(round(time.time() * 1000))

        with Status(
            status=f"[cyan]Running command:[dim]{display_cmd}...",
            console=console,
            spinner="line",
            refresh_per_second=10,
        ):

            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                text=True,
                capture_output=True,
            )

        with open(file_path, "w") as f:
            f.write(result.stdout)

        return result.stdout, result.stderr, result.returncode, command_start_time
