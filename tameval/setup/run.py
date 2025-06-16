import os
import sys
import json
import tomli
import shutil
import random
import subprocess
import argparse
from git import Repo, RemoteProgress, GitCommandError
from glob import glob
from tqdm import tqdm
from pathlib import Path

from collections import Counter
from typing import List, Dict, Iterable

# from utils import runner

from setup.java_repo import JavaRepo
from setup.go_repo import GoRepo
from setup.python_repo import PythonRepo


def get_repo_object(lang: str):
    if lang.lower() == "java":
        return JavaRepo
    elif lang.lower() == "go":
        return GoRepo
    elif lang.lower() == "python":
        return PythonRepo
    else:
        raise ValueError(f"Unknown language: {lang}")


random.seed(42)


def get_arg_parser_obj(parents=None):
    """
    Parse command line arguments.
    """

    parser = argparse.ArgumentParser(
        description=f"Generate enhanced test file samples for evaluation on TAM-eval.",
        parents=parents,
    )
    parser.add_argument(
        "--benchmark-version",
        type=str,
        required=True,
        help="Version of the benchmark to eval.",
        default="v1",
    )
    parser.add_argument(
        "--repo-folder",
        type=Path,
        default="/download/repo",
        help="Path to where downloaded repos located.",
    )
    parser.add_argument(
        "--only-refresh-files",
        type=int,
        default=0,
        required=False,
        help="Only recopy files from repo-folder, do not build projects.",
    )
    return parser


def copy_files_with_extension(src_dir, dst_dir):
    src_dir = os.path.abspath(src_dir)
    dst_dir = os.path.abspath(dst_dir)

    for root, _, files in os.walk(src_dir):
        for file in files:
            src_path = os.path.join(root, file)
            rel_path = os.path.relpath(src_path, src_dir)
            if rel_path.startswith("__HOME__") or rel_path.startswith(".git"):
                continue
            if rel_path.split(".")[-1] not in ["java", "py", "go", "kt"]:
                continue
            dst_path = os.path.join(dst_dir, rel_path)
            # print(rel_path, "->", dst_dir)
            # os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            try:
                shutil.copy2(src_path, dst_path)
            except Exception as e:
                # print(e, rel_path)
                continue

    return


def json_path_hook(data):
    for key, value in data.items():
        if isinstance(value, str) and "_path" in key:
            data[key] = Path(value)
    return data


class CloneProgress(RemoteProgress):
    def __init__(self):
        super().__init__()
        self.pbar = None

    def update(self, op_code, cur_count, max_count=None, message=""):
        if self.pbar is None and max_count:
            self.pbar = tqdm(total=max_count, unit="objects")
        if self.pbar:
            self.pbar.n = cur_count
            self.pbar.set_description(message.strip() or "Cloning")
            self.pbar.refresh()


def setup_repo(
    args,
    sample: Dict,
    repo_folder: Path,
    base_url_template: str = "https://github.com/{repo}.git",
):

    repo_name = sample["repo_info"]["repository"]
    sha = sample["repo_info"]["sha"]
    clone_dir = repo_folder / repo_name
    working_dir = Path("tmp") / repo_name
    print(f"Setup repo: {repo_name}...")

    print("Recopy files from --dowload-repo...")
    if os.path.exists(clone_dir):
        copy_files_with_extension(clone_dir, working_dir)

    if args.only_refresh_files and os.path.exists(clone_dir):
        return

    ### 0. Download docker image
    download_image_command = f"docker pull {sample['run_info']['docker_image']}"
    print(f"Run {download_image_command}...")
    try:
        result = subprocess.run(download_image_command, shell=True)
        if result.returncode == 0:
            print(f"{repo_name} image downloaded!")
        else:
            print(f"{repo_name} image download error {result.returncode}!")
    except Exception as e:
        print(f"An error occurred for {repo}: {e}")

    repo_obj = get_repo_object(lang=sample["lang_info"]["lang"])(
        origin=base_url_template.format(repo=repo_name),
        repo_dir=clone_dir,
        working_dir=working_dir,
        commit=sample["repo_info"]["sha"],
    )
    repo_obj.prepare()


def main(args: argparse.Namespace):

    # check benchmark samples
    benchmark_dir_path = f"benchmark/{args.benchmark_version}/*"
    benchmark_sample_paths = sorted(glob(benchmark_dir_path))
    num_of_samples = len(benchmark_sample_paths)

    print(f"Found {num_of_samples} benchmark examples in {benchmark_dir_path}")
    if num_of_samples == 0:
        raise ValueError("No benchmark samples found")

    # get unique repos
    unique_repos = set([])
    unique_samples = []
    for sample_path in tqdm(benchmark_sample_paths):
        with open(sample_path, "rb") as f:
            sample = tomli.load(f)
        if sample["repo_info"]["repository"] not in unique_repos:
            unique_repos.add(sample["repo_info"]["repository"])
            unique_samples.append(sample)

    print(f"Need to setup {len(unique_samples)} repos.")

    # setup repos
    for sample in tqdm(
        unique_samples[::-1], desc="Setuping repos...", total=len(unique_samples)
    ):
        setup_repo(args, sample, args.repo_folder)

    return


if __name__ == "__main__":
    args = get_arg_parser_obj().parse_args()
    main(args)
