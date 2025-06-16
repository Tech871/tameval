from __future__ import annotations

import re
import os
import copy
import json
import time
import tempfile
from typing import TYPE_CHECKING
from collections import Counter
from typing import Any, ClassVar, Literal
import urllib.parse
import datetime
import dateutil
from difflib import SequenceMatcher
from dataclasses import dataclass, field, InitVar
from pathlib import Path
import shutil
import uuid

import docker

from setup.utils import (
    run_command,
    RepoFailureError,
    CommandFailureError,
    CommandOutput,
    Timer,
)


@dataclass(kw_only=True)
class Unit:
    focal: Code
    tests: UnitTests | None = None
    _uid: str | None = None  # custom uid if not using path
    tags: UnitTags | None = None
    misc: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        assert (result := self._uid or str(self.focal.path)), "Cannot retrieve unit uid"
        return result


@dataclass(kw_only=True)
class UnitTests:
    code: Code | None = None
    test_cases: dict[str, Code] | None = None
    results: TestResult | None = None
    results_per_test_case: dict[str, TestResult] | None = None
    utility: Code | None = None
    framework: str | None = None
    misc: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class Repo:
    origin: str | None = None
    commit: str | None = None
    unit_tests: list[Unit] = field(default_factory=list)
    unit_tests_filtered_out: list[Unit] = field(default_factory=list)
    tags: RepoTags | None = None  # previously stored in .misc['tags']: dict[str, Any]

    # repo and working dir paths are also saved in repo_to_dict
    # because it may be costly to re-create them and build project again on each run
    repo_dir: Path | Literal["auto"] = "auto"
    working_dir: Path | Literal["auto"] = "auto"

    misc: dict[str, Any] = field(
        default_factory=dict
    )  # to save any info, such a build time

    docker_instance: InitVar[docker.models.containers.Container | None] = None
    _command_outputs_container: ClassVar[list[CommandOutput] | None] = None

    lang: ClassVar[str | None] = None

    _built_flag_file = ".project_is_built"
    _lock_flag_file = ".operation_in_progress"
    _lock_flag_message = (
        "lock file exists in {working_dir}; some operation was interrupted and"
        " you need to remove working dir and re-create it; or another script is running"
    )

    def __post_init__(self, *_args):
        # *_args are InitVar-s https://docs.python.org/3/library/dataclasses.html#init-only-variables
        if self.repo_dir == "auto":
            self.repo_dir = (
                Path("tmp/repos")
                / (self.lang or "none")
                / self.shallow_name_from_origin
            )
        if self.working_dir == "auto":
            self.working_dir = (
                Path("tmp/working")
                / (self.lang or "none")
                / self.shallow_name_from_origin
            )

    def get_lang_info(self) -> dict[str, Any]:
        """
        Should be extended in subclasses to include other fields
        """
        return {"lang": self.lang}

    @property
    def uid(self) -> str:
        """
        A unique ID to refer. For example, to store generated tests for a specifid uid.

        Raises AssertionError if origin and commit is not set.
        """
        assert self.origin and self.commit, "uid: origin or commit is None"
        return f"{self.shallow_name_from_origin}__{self.commit}"

    @property
    def origin_ssh(self) -> str:
        origin = self.origin
        assert origin.startswith("https://github.com/")
        origin = origin.replace("https://github.com/", "ssh://git@github.com/")
        if not origin.endswith(".git"):
            origin = origin + ".git"
        return origin

    @property
    def shallow_name_from_origin(self) -> str:
        """
        If self.origin is set to http(s)://abc/def, returns "def__abc".

        Raises:
            AssertionError
                If self.origin is not set
            ValueError
                If urllib.parse.urlparse fails to parse URL
        """
        assert self.origin is not None, "shallow_name_from_origin: origin is None"
        path = urllib.parse.urlparse(self.origin).path[1:]
        return "__".join(path.split("/")[::-1])

    def clone(
        self,
        shallow: bool = True,
        reset: bool = True,
        ssh: bool = True,
        verbose: bool = True,
        exist_ok: bool = True,
        clean: bool = False,
    ):
        """
        Clones a repo if not cloned yet, and sets the commit version to self.commit if specified.
        Removes git-ignored files.

        If `reset`, tries to set self.commit with `git reset --hard {commit}`, or just `git reset --hard`
        if commit is not specified.

        If not `exist_ok`, removes dir conpletely and clones again.

        If `clean`, also runs `git clean -xdf` to remove untracked files.

        Raises:
            AssertionError
                if .repo_dir is None
            RuntimeError
                If cannot clone or make another required git operations

        """
        dir = self.repo_dir

        if not exist_ok and dir.exists():
            shutil.rmtree(dir)

        origin = self.origin_ssh if ssh else self.origin

        if not dir.is_dir() or not any(  # no .repo_dir
            dir.iterdir()
        ):  # .repo_dir is empty
            if verbose:
                print(f"Cloning {origin}...")
            temp_dir = Path(tempfile.mkdtemp(prefix="clone_"))
            try:
                # clone first to temp dir, to prevent half-cloned self.repo_dir
                clone_args = ["--depth=1"] if shallow else []
                run_command(
                    ["git", "clone", *clone_args, origin, str(temp_dir)],
                    raise_on_error=True,
                )
                dir.parent.mkdir(exist_ok=True, parents=True)
                temp_dir.rename(dir)
            except (RuntimeError, OSError, KeyboardInterrupt) as e:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise e

        else:
            if verbose:
                print(f"Already cloned: {origin}")

        # ['git', '-c', 'advice.detachedHead=false', 'checkout', self.commit],

        def reset():
            reset_args = [self.commit] if self.commit else []
            run_command(
                ["git", "reset", "--hard", *reset_args],
                cwd=str(dir),
                raise_on_error=True,
            )

        if reset:
            if not self.commit:
                reset()
            else:
                # or: https://stackoverflow.com/a/43136160
                try:
                    reset()
                except RuntimeError:
                    run_command(
                        ["git", "fetch", "origin", self.commit],
                        cwd=str(dir),
                        raise_on_error=True,
                    )
                    reset()

        if clean:
            run_command(["git", "clean", "-xdf"], cwd=str(dir), raise_on_error=True)

    def find_code_files(self) -> list[Path]:
        """
        Finds all code files in self.repo_dir, based on self.extension.
        """
        return [
            path.relative_to(self.repo_dir)
            for path in self.repo_dir.rglob(f"*{self.extension}")
            if path.is_file()  # can be also directory and symlink in rare cases
        ]

    @property
    def extension(self) -> str:
        raise NotImplementedError()

    def resolve_lang_version(self):
        raise NotImplementedError()

    def is_unit_test_file(self, file: Path) -> bool:
        raise NotImplementedError()

    def test_path_to_focal_name(self, test_path: Path) -> str:
        raise NotImplementedError()

    def find_unit_tests(
        self,
        allow_sequence_matching: bool = True,
    ) -> list[Unit]:
        """
        Raises OSError on OS errors, such as Permission denied
        """
        test_to_focal = {}
        try:
            code_file_paths = self.find_code_files()
        except OSError as e:
            if e.errno == 36:  # file name too long
                # if we are here, RepoFailureError will be raised instead of OSError,
                # and it will be handled to proceed with the next repo
                raise RepoFailureError(str(e)) from e
            else:
                raise e
        for file_path in code_file_paths:
            if self.is_unit_test_file(file_path):
                focal_path = self.find_focal_file(
                    file_path,
                    code_file_paths,
                    allow_sequence_matching=allow_sequence_matching,
                )
                if focal_path:
                    test_to_focal[file_path] = focal_path
        focals_counter = Counter(test_to_focal.values())
        test_to_focal = {
            test: focal
            for test, focal in test_to_focal.items()
            if focals_counter[focal] == 1
        }

        units = []
        for test_path, focal_path in test_to_focal.items():
            try:
                units.append(
                    Unit(
                        focal=Code(
                            path=focal_path,
                            content=(self.repo_dir / focal_path).read_text(),
                        ),
                        tests=UnitTests(
                            code=Code(
                                path=test_path,
                                content=(self.repo_dir / test_path).read_text(),
                            )
                        ),
                    )
                )
            except UnicodeDecodeError:
                pass

        return units

    def find_focal_file(
        self,
        test_file: Path,
        code_files: list[Path],
        allow_sequence_matching: bool = True,
    ) -> Path | None:
        focal_name = self.test_path_to_focal_name(test_file)
        focal_paths = [f for f in code_files if f.name == focal_name]
        if len(focal_paths) == 1:
            return focal_paths[0]
        elif len(focal_paths) == 0 or not allow_sequence_matching:
            return None
        else:
            similarities = [
                SequenceMatcher(None, str(test_file.parent), str(file.parent)).ratio()
                for file in focal_paths
            ]
            top_index = similarities.index(max(similarities))
            return focal_paths[top_index]

    def remove_working_dir(self):
        if self.working_dir.is_dir():
            try:
                shutil.rmtree(self.working_dir)
            except PermissionError:
                for root, dirs, files in os.walk(self.working_dir):
                    for d in dirs:
                        os.chmod(os.path.join(root, d), 0o777)
                    for f in files:
                        os.chmod(os.path.join(root, f), 0o777)
                shutil.rmtree(self.working_dir)

    def make_working_dir(self, exist_ok: bool = True):
        """
        If self.working_dir exists and not flush_if_exists, does nothing. Else removes
        and re-creates self.working_dir, copies files from self.repo_dir to self.working_dir,
        excluding .git.

        Raises:
            AssertionError
                If .repo_dir or .working_dir is not set
            OSError
                On OS errors
        """
        if self.working_dir.is_dir():
            if exist_ok:
                return
            self.remove_working_dir()
        shutil.copytree(
            self.repo_dir,
            self.working_dir,
            symlinks=True,
            # ignore=shutil.ignore_patterns('.git')
        )

    def get_docker_image_from_dockerfile(self, prefix: str = "") -> str | None:
        if (dockerfile_path := self.repo_dir / "Dockerfile").is_file():
            content = dockerfile_path.read_text(errors="ignore")
            if match := re.search(
                rf"^FROM\s+({prefix}\S+)\s.*$",
                content,
                flags=re.IGNORECASE | re.MULTILINE,
            ):
                image = match.group(1)
                if len(docker.from_env().images.list(filters={"reference": image})):
                    return image
        return None

    def get_default_docker_image(self) -> str:
        """Subclasses can raise RepoFailureError, or raise AssertionError or OSError if not a repo failure"""
        return self.get_docker_image_from_dockerfile() or "ubuntu"

    def start_docker(
        self, exist_ok: bool = True, image: str = "auto", share_cache: bool = True
    ):
        """
        Runs docker container, mounting the current .working_dir (not .repo_dir). Subsequent calls of
        various build and run functions (defined in subclasses) will use docker
        Make sure to stop container after commands execution.

        Raises:
            AssertionError
                If docker instance already exists
            RepoFailureError
                On Docker errors
        """
        if self.docker_instance is not None:
            if exist_ok:
                return
            else:
                self.stop_docker(ignore_errors=True)
        try:
            if image == "auto":
                image = self.get_default_docker_image()
                self.misc["docker_image"] = image
        except RepoFailureError as e:
            e.error_stage = "start_docker"
            raise e

        try:
            host_home = Path.home().resolve()
            uid = os.getuid()
            gid = os.getgid()
            user = f"{uid}:{gid}"

            (self.working_dir / "_HOME_").mkdir(exist_ok=True)
            environment = {"HOME": "/app/_HOME_"}
            # if self.lang.lower() == "python":
            #     environment = {
            #         "HOME": "/app/_HOME_",
            #         "PATH": "_HOME_/.local/bin:$PATH",
            #         "PYTHONUSERBASE": "_HOME_/.local/",
            #     }

            volumes_to_mount = {
                str(self.working_dir.resolve()): {"bind": "/app", "mode": "rw"}
            }

            if share_cache:
                for (
                    host_path,  # dir on host machine (for sharing maven cache, pip cache etc.)
                    mount_path,  # where to mount in docker
                    symlink_host_path,  # symlink from docker home dir (will be used from docker) - DISABLED now
                ) in [
                    (host_home / ".m2", "/.m2", self.working_dir / "_HOME_/.m2"),
                    (
                        host_home / ".cache/pip",
                        "/.pip_cache",
                        self.working_dir / "_HOME_/.cache/pip",
                    ),
                    (
                        host_home / ".cache/go-build",
                        "/.go_cache",
                        self.working_dir / "_HOME_/.cache/go-build",
                    ),
                ]:
                    host_path.mkdir(exist_ok=True, parents=True)
                    volumes_to_mount[host_path] = {
                        "bind": str(mount_path),
                        "mode": "rw",
                    }
                    # if not symlink_host_path.exists(follow_symlinks=False):
                    #     symlink_host_path.parent.mkdir(exist_ok=True, parents=True)
                    #     symlink_host_path.symlink_to(mount_path, target_is_directory=True)

            client = docker.from_env()
            volumes_to_mount[str((self.working_dir / "_HOME_/go").resolve())] = {
                "bind": "/go",
                "mode": "rw",
            }
            print(f"Starting docker image {image}")
            self.docker_instance = client.containers.run(
                image=image,
                # this command allows to exit the container using SIGTERM
                command=[
                    "/bin/sh",
                    "-c",
                    "trap true TERM; sleep 1 & while wait $!; do sleep 1 & done",
                ],
                volumes=volumes_to_mount,
                environment=environment,
                # tmpfs={'/home': f'uid={uid},gid={gid}'},
                user=user,
                working_dir="/app",
                detach=True,
                tty=True,
                stdout=True,
                stderr=True,
            )

        except (
            RepoFailureError,
            docker.errors.ContainerError,
            docker.errors.ImageNotFound,
            docker.errors.APIError,
        ) as e:
            raise RepoFailureError(
                f"Failed to start docker image {image}", error_stage="start_docker"
            ) from e

    def stop_docker(self, ignore_errors: bool = False):
        """
        Ensure the container is stopped before removal

        Raises:
            docker.errors.APIError
                On docker error, if not ignore_errors
        """
        if self.docker_instance is None:
            return

        print("Removing docker container...")
        try:
            self.docker_instance.reload()
            if self.docker_instance.status == "running":
                self.docker_instance.stop(timeout=10)

            # Attempt to remove the container, force if necessary
            self.docker_instance.remove(force=True)
        except docker.errors.APIError as e:
            if not ignore_errors:
                raise e
            else:
                print(f"WARNING! Error while stopping Docker: {e}")
            #     raise RepoFailureError(f'Failed to stop docker', error_stage='stop_docker') from e

        self.docker_instance = None

    def get_custom_environment_vars(self) -> dict[str, str]:
        """
        Returns a list of environment variables that will be used for each .run_docker_command() call.

        If the output dict contains a PATH variable, .run_docker_command() will also append the default
        PATH value and "/app/_HOME_/.local/bin" to the PATH.
        """
        return {}

    def get_PATH(self) -> str:
        # TODO find a better way
        exit_code, output = self.docker_instance.exec_run(
            cmd=["/bin/sh", "-c", "echo $PATH"],
            tty=True,
            stdout=True,
            stderr=True,
        )
        logs = output.decode("utf-8", errors="replace")
        if exit_code != 0:
            raise RuntimeError(f"Cannot append to PATH! Docker output:\n{logs}")
        PATH = logs.strip()
        return PATH

    def run_docker_command(
        self,
        cmd: str,
        timeout: float = 0,
        stream: bool = False,
        as_root: bool = False,
        raise_on_error: bool | Literal["on_timeout"] = True,
        override_custom_env_vars: dict[str, str] | None = None,
    ) -> CommandOutput | None:
        """
        Issues a command, using docker. Waits until the process ends, returns CommandOutput
        and timeout flag. For empty commands returns CommandOutput(output="", exit_code=0).

        Currently works only via docker, after .start_docker().

        If stream=True, prints the results interactively and returns None.

        If raise_on_error==True, raises CommandFailureError on non-zero exit code and
        CommandTimeoutError on timeout. If raise_on_error=='on_timeout', raises only on timeout.

        If command_outputs_container is not None, will append the current output to it
        before returning (even if it raises CommandFailureError or CommandTimeoutError)

        If override_custom_env_vars is set, will use the provided dictionary, otherwise
        will use `.get_custom_environment_vars()`.

        Raises:
            AssertionError
                If docker_instance is None, or timeout < 0
            RepoFailureError
                If something prevents to obtain the stdout and exit_code. Currently
                the only cause is that docker raises docker.errors.APIError.
        """
        stream = True
        assert timeout >= 0, "run_docker_command: timeout < 0"
        assert self.docker_instance, "run_docker_command: docker_instance is None"

        if timeout > 0:
            if "&&" in cmd or "|" in cmd or ";" in cmd:  # TODO what if " in command?
                cmd = f'sh -c "({cmd})"'  # not all docker images support bash
            # cmd = f"timeout --foreground {timeout} {cmd}"

        if len(cmd.strip()):
            print(f"Running command: {cmd}")
        else:
            print(f"Command is empty.")
            return CommandOutput(command=cmd, output="", exit_code=0)

        try:
            env_vars = (
                self.get_custom_environment_vars()
                if override_custom_env_vars is None
                else override_custom_env_vars
            )

            joint_PATH = self.get_PATH() + ":/app/_HOME_/.local/bin"
            if (custom_PATH := env_vars.pop("PATH", None)) is not None:
                joint_PATH += ":" + custom_PATH

            exit_code, output = self.docker_instance.exec_run(
                cmd=["/bin/sh", "-c", cmd],
                tty=True,
                # change tty to false for separate stderr and stdout (not tested)
                # then you can use .demux() to output to separate them
                stdout=True,
                stderr=True,
                stream=stream,
                environment={"PATH": joint_PATH, **env_vars},
                user="root" if as_root else "",
            )

            if stream:
                logs = ""
                for chunk in output:
                    _chunk = chunk.decode(errors="replace")
                    print(_chunk, end="")
                    logs += _chunk
                exit_code = 0 if exit_code is None else exit_code
            else:
                logs = output.decode(errors="replace")

            if exit_code == 126:
                raise RuntimeError(
                    f"Docker exit code 126. Maybe this is because .working_dir was be removed."
                    f" If so, stop docker and restart it. Docker output:\n{logs}"
                )

            output = CommandOutput(
                command=cmd,
                output=logs,
                exit_code=exit_code,
                was_timeout=exit_code == 124 and timeout > 0,
            )

            if self._command_outputs_container is not None:
                self._command_outputs_container.append(output)

            if output.was_timeout and raise_on_error in [True, "on_timeout"]:
                raise CommandTimeoutError(output=output)
            elif exit_code != 0 and raise_on_error is True:
                raise CommandFailureError(output=output)

            return output

        except docker.errors.APIError as e:
            try:
                logs = self.docker_instance.logs().decode(errors="replace")
                self.misc["docker_error_logs"] = logs
                msg = (
                    "Docker APIError"
                    f', see full logs in self.misc["docker_error_logs"]'
                    f', last log lines:\n{"\n".join(logs.split("\n")[-5:])}'
                )
            except docker.errors.APIError:
                msg = "Docker APIError, cannot retrieve logs"
            raise RepoFailureError(msg, error_stage=None) from e
            # .error_stage is currently unknown, may be filled from the calling code

    def _lock(self):
        if (self.working_dir / self._lock_flag_file).exists():
            raise RuntimeError(
                self._lock_flag_message.format(working_dir=self.working_dir)
            )
        (self.working_dir / self._lock_flag_file).touch()

    def _unlock(self):
        (self.working_dir / self._lock_flag_file).unlink()

    def is_built(self):
        return (self.working_dir / self._built_flag_file).exists()

    def build_project(self, exist_ok: bool = True, timeout: float = 5000, **kwargs):
        """
        Builds project in .working_dir and removes existing tests from .working_dir
        so that `.run_single_test()` can be run.

        Before building the project, checks if ".project_is_built" file, if it exists,
        skip all the next steps and returns. Otherwise creates this file after building
        the project.

        Currenly can only use docker, need to run .start_docker() first.

        Implementations may rise RepoFailureError, or AssertionError, OSError if the error
        is not related to repo failure. If implementation raises TimeoutError or CommandFailureError,
        it is wrapped into RepoFailureError.
        """
        # do we need to build, or return?
        if self.is_built():
            if exist_ok:
                print("Project is already built")
                return
            else:
                (self.working_dir / self._built_flag_file).unlink()

        # building
        start_time = time.time()
        self._lock()
        self.misc["build_commands"] = self._command_outputs_container = []
        try:
            try:
                self._build_project(timeout=timeout, **kwargs)
                self._unlock()
                self.misc["build_time"] = time.time() - start_time
                (self.working_dir / self._built_flag_file).touch()
            except (TimeoutError, CommandFailureError) as e:
                raise RepoFailureError("command timeout or non-zero exit code") from e
        except RepoFailureError as e:
            e.error_stage = "build_project"
            raise e
        finally:
            self._command_outputs_container = None

    def _build_project(self, timeout: float, **kwargs):
        raise NotImplementedError()

    def prepare(self, exist_ok: bool = True):
        self.clone(exist_ok=exist_ok, ssh=False)
        if (self.working_dir / self._lock_flag_file).exists():
            exist_ok = False  # from this point and further
        # TODO don't do this, maybe other script is running
        self.make_working_dir(exist_ok=exist_ok)
        self.start_docker(exist_ok=exist_ok)
        self.build_project(exist_ok=exist_ok)
        self.docker_instance.stop()


def get_commit(
    repo_dir: str | Path, with_datetime: bool = False
) -> str | tuple[str, datetime.datetime]:
    """
    Returns a commit SHA for the cloned repo. If `with_datetime`, also returns commit datetime.

    Raises
        RepoFailureError
            If .repo_dir does not have any commits yet, or any other git error occures
        AssertionError
            If .repo_dir / '.git' does not exist
    """
    assert (repo_dir / ".git").is_dir()
    try:
        output: CommandOutput = run_command(
            ["git", "log", "-1", "--pretty=format:%H %ad"],
            cwd=repo_dir,
            raise_on_error=True,
        )
    except RuntimeError as e:
        raise RepoFailureError(f"Cannot get commit", error_stage="get_commit") from e
    commit_sha, commit_date = output.output.strip().split(maxsplit=1)
    if with_datetime:
        return commit_sha, dateutil.parser.parse(commit_date)
    else:
        return commit_sha
