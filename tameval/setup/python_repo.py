import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, override
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
import xml.etree.ElementTree as ET

from ruamel.yaml import YAML
import ruamel


from setup.repo import Repo, Timer, RepoFailureError


@dataclass(kw_only=True)
class PythonRepo(Repo):
    lang: ClassVar[str | None] = "Python"
    python_version: str | None = None
    has_requirements: bool = False  # is not used, for backward compatibility
    python_cfg_file: (
        Literal[
            "pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "Dockerfile"
        ]
        | None
    ) = None

    @override
    def get_lang_info(self) -> dict[str, Any]:
        """
        Should be extended in subclasses to include other fields
        """
        return {
            **super().get_lang_info(),
            "python_version": self.python_version,
            "python_cfg_file": self.python_cfg_file,
        }

    @property
    @override
    def extension(self) -> str:
        return ".py"

    @staticmethod
    def _get_matching_version(version_pattern) -> str | None:
        # there should exist a much easier way to get python version !!
        try:
            version_pattern = version_pattern.replace(" ", "")
            if (
                not ">" in version_pattern
                and not "<" in version_pattern
                and not "=" in version_pattern
            ):
                versions = [Version(str(x)) for x in version_pattern.split(",")]
                return str(max(versions))
            else:
                spec = SpecifierSet(version_pattern)
                # Define the range of versions you care about
                possible_versions = []

                for major in [3]:
                    for minor in range(13, 4, -1):  # no docker image for 3.14
                        version = Version(f"{major}.{minor}")
                        if version in spec:
                            possible_versions.append(version)

                # Return the first valid version (e.g., the lowest)
                return str(possible_versions[0]) if possible_versions else None
        except (InvalidVersion, InvalidSpecifier):
            return None

    @override
    def resolve_lang_version(self):
        # todo refactor and simplify
        for file in [
            "pyproject.toml",
            "requirements.txt",
            "setup.py",
            "Dockerfile",
            "Pipfile",
        ]:
            if (path := self.repo_dir / file).is_file():
                if self.python_cfg_file is None:
                    # important: prioretize pyproject.toml > requirements.txt > ...
                    self.python_cfg_file = file
                content = re.sub(r"#.*", "", path.read_text(errors="replace"))
                version_pattern1 = re.compile(
                    r"""
                    (?:\bpython[:\s=]*[\"']?|requires-python\s*=\s*[\"']|python_version\s*=\s*[\"']) # Start of the pattern
                    ([><=]+\s*[2-3]\.\d+(?:\.\d+)?             # Captures the exact version or lower bound
                    (?:,\s*[><=]*\s*[2-3](?:\.\d+)?(?:\.\d+)?)?)    # Optionally сaptures upper bound
                    """,
                    re.VERBOSE | re.IGNORECASE,
                )
                if match := re.search(version_pattern1, content):
                    self.python_version = self._get_matching_version(match.group(1))
                    break
                version_pattern2 = re.compile(
                    r"""
                    (?:\bpython[:\s=]*[\"']?|requires-python\s*=\s*[\"']|python_version\s*=\s*[\"']) # Start of the pattern
                    (?:=*)(\s*[2-3]\.\d+(?:\.\d+)?)             # Captures the exact version
                    """,
                    re.VERBOSE | re.IGNORECASE,
                )
                if matches := version_pattern2.findall(content):
                    try:
                        versions = [Version(str(m)) for m in matches]
                        self.python_version = str(max(versions)).replace(" ", "")
                    except InvalidVersion:
                        self.python_version = None
                    break

    @override
    def is_unit_test_file(self, file: Path) -> bool:
        if "integration" in str(file).lower():
            return False
        if "site-packages" in str(file).lower():
            return False
        return file.name.startswith("test_") or file.name.endswith("_test.py")

    @override
    def test_path_to_focal_name(self, test_path: Path) -> str:
        return (
            Path(test_path.name).stem.replace("test_", "").replace("_test", "") + ".py"
        )

    @override
    def get_default_docker_image(self) -> str:
        if (
            image := self.get_docker_image_from_dockerfile(prefix="python:")
        ) is not None:
            return image
        elif (
            lang_version := self.python_version
        ) is not None:  # use the language version
            if ";" in lang_version:
                lang_version = "{" + lang_version + "}"
            image_name = re.sub(r"[<>=]*", "", f"python:{lang_version}")
            return image_name
        else:  # try default python:3 image
            return "python:3"

    @override
    def get_custom_environment_vars(self) -> dict[str, str]:
        return {
            "PIP_CACHE_DIR": "/.pip_cache",
            "PATH": "_HOME_/.local/bin:$PATH",
            "PYTHONUSERBASE": "_HOME_/.local/",
        }

    @override
    def _build_project(self, timeout: float):
        self.misc["build_commands"] = build_commands = []

        with Timer(timeout=timeout) as timeout_clock:
            for path in list(self.repo_dir.glob("*requirements*.txt")) + list(
                self.repo_dir.glob("requirements/*.txt")
            ):
                path = path.relative_to(self.repo_dir)
                self.run_docker_command(
                    cmd=f"pip install -r {path}",
                    timeout=timeout_clock.get_remaining_time(),
                    raise_on_error=False,  # it may be ok to fail some requirements files, trying to continue
                )

            # TODO support poetry as in example repos:
            # - https://github.com/canonical/opensearch-operator/blob/2/edge/pyproject.toml
            # - https://github.com/All-Hands-AI/openhands-aci
            # repo.run_docker_command('apt update && apt install pipx -y', as_root=True, timeout=...)
            # repo.run_docker_command('pipx install poetry', timeout=...)
            # repo.run_docker_command('poetry install', timeout=...)
            # implement extracting python version from poetry.lock

            if self.python_cfg_file == "Pipfile":
                # TODO check more repos with pipenv for command validation
                cmds = [
                    "pip install pipenv",
                    "pipenv lock",
                    "pipenv install --deploy --system --dev --user",
                ]
            elif self.python_cfg_file == "pyproject.toml":
                # if all and/or tests extras do not exist, this command will skip them
                cmds = ["pip install .[all,test] --user"]
            elif self.python_cfg_file == "setup.py":
                cmds = ["pip install . --user"]
            elif self.python_cfg_file == "requirements.txt":
                cmds = []  # already installed
            else:
                cmds = []
                print(
                    "No dependency file found (Pipfile, pyproject.toml, setup.py, requirements.txt)."
                    " Skipping dependency installation."
                )

            cmds.append(
                "pip install --user coverage pytest pytest_cov covdefaults Cython mock ddt pytest_mock testfixtures"
            )
            cmds.append(
                "pip install --use-pep517 --user git+https://github.com/Klema17/mutpy.git"
            )
            build_command = " && ".join(cmds)

            self.run_docker_command(
                cmd=build_command,
                timeout=timeout_clock.get_remaining_time(),
            )
