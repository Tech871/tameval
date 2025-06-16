import re
import time
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, ClassVar, override
from packaging.version import Version

import pandas as pd
import xml.etree.ElementTree as ET


from setup.repo import Repo, Timer, RepoFailureError


@dataclass(kw_only=True)
class GoRepo(Repo):
    lang: ClassVar[str | None] = "Go"
    go_version: str | None = None

    @override
    def get_lang_info(self) -> dict[str, Any]:
        """
        Should be extended in subclasses to include other fields
        """
        return {
            **super().get_lang_info(),
            "go_version": self.go_version,
        }

    @property
    @override
    def extension(self) -> str:
        return ".go"

    @override
    def resolve_lang_version(self):
        if (path := self.repo_dir / "go.mod").is_file():
            content = re.sub(r"#.*", "", path.read_text(errors="replace"))
            version_pattern = r"(?:\bgo\s+)(\d+\.\d+(?:\.\d+)?)"
            if match := re.search(version_pattern, content, re.IGNORECASE):
                self.go_version = match.group(1)

    @override
    def is_unit_test_file(self, file: Path) -> bool:
        if "integration" in str(file).lower():
            return False
        return file.name.endswith("_test.go")

    @override
    def test_path_to_focal_name(self, test_path: Path) -> str:
        return Path(test_path.name).stem.replace("_test", "") + ".go"

    @override
    def get_default_docker_image(self) -> str:
        if (
            image := self.get_docker_image_from_dockerfile(prefix="golang:")
        ) is not None:
            return image
        elif self.go_version is not None:  # use the language version
            return re.sub(r"[<>=]*", "", f"golang:{self.go_version}")
        else:  # try default image
            return "golang"

    @override
    def get_custom_environment_vars(self) -> dict[str, str]:
        return {
            "GOPATH": "/app/_HOME_/go",
            "GOBIN": "/app/_HOME_/go/bin",
            "GOCACHE": "/.go_cache",
            "PATH": "/app/_HOME_/go/bin",
        }

    @override
    def _build_project(self, timeout: float):
        if not (self.working_dir / "go.mod").exists():
            raise RepoFailureError(f"File go.mod does not exist")

        with Timer(timeout=timeout) as timeout_clock:
            self.run_docker_command(
                # cmd="go mod download -v",
                cmd="go build ./...",
                timeout=timeout_clock.get_remaining_time(),
            )
            mutation_dependency = (
                "github.com/VirtualRoyalty/go-mutesting/cmd/go-mutesting@v1.0.9"
            )
            self.run_docker_command(
                cmd=f"go install {mutation_dependency}",
                # timeout=timeout_clock.get_remaining_time(),
            )
            self.run_docker_command(
                cmd="go install github.com/jstemmer/go-junit-report@latest",
                # timeout=timeout_clock.get_remaining_time(),
            )
