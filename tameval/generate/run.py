import os
import sys
import json
import tomli
import random
import argparse
from glob import glob
from tqdm import tqdm
from pathlib import Path

from collections import Counter
from typing import List, Dict, Iterable
from dotenv import dotenv_values

from utils import aux
from generate.llm_enhancer import LLMTestEnhancer


sys.path.append("..")


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
        "--outputs-dir-path",
        type=Path,
        required=False,
        help="Path to save outputs for later evaluation.",
        default=Path("outputs"),
    )
    parser.add_argument(
        "--work-folder", type=Path, default="tmp", help="Path to the work folder."
    )
    parser.add_argument(
        "--repo-folder",
        type=Path,
        default="/download/repo",
        help="Path to where downloaded repos located.",
    )
    parser.add_argument("--model-to-eval", required=True, help="model-to-eval")
    parser.add_argument(
        "--llm-api-base-url", required=True, help="OpenAI compatible API /v1 base url"
    )
    parser.add_argument(
        "--min-tests-per-request",
        default=4,
        type=int,
        required=False,
        help="Min tests value of test generated in prompt",
    )
    parser.add_argument(
        "--edit-format",
        default="whole",
        type=str,
        required=False,
        help="Edit format: whole, edit or udiff",
    )

    parser.add_argument(
        "--max-tests-per-request",
        default=4,
        type=int,
        required=False,
        help="Max tests value of test generated in prompt",
    )
    parser.add_argument(
        "--force-clean-folders",
        default=0,
        type=int,
        help="Force clean folders (0=false, 1=true)",
    )
    parser.add_argument(
        "--prefix", default=None, type=str, help="Prefix for all report names"
    )
    parser.add_argument(
        "--clearml-task-name",
        default=None,
        type=str,
        required=False,
        help="ClearML task name",
    )
    parser.add_argument(
        "--force-gen-recompute",
        default=0,
        type=int,
        required=False,
        help="Force recompute already computed samples",
    )
    parser.add_argument(
        "--prompt-version",
        default="v1",
        type=str,
        required=False,
        help="Test generation prompt version to use",
    )
    parser.add_argument(
        "--test-gen-temperature",
        default=0.2,
        type=float,
        required=False,
        help="Test generation temperature",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        type=str,
        required=False,
        help="Path to.env file",
    )
    parser.add_argument(
        "--batch-generation",
        default=0,
        type=int,
        required=False,
        help="Activate batch generation",
    )
    parser.add_argument(
        "--attempt",
        default=1,
        type=int,
        required=False,
    )
    parser.add_argument(
        "--max-attempts-num",
        default=1,
        type=int,
        required=False,
    )
    parser.add_argument(
        "--tasks-to-eval",
        default="create,update,repair",
        type=str,
        required=False,
        help="Tasks to eval",
    )
    parser.add_argument(
        "--qwen3-disable-thinking",
        default=1,
        type=int,
        required=False,
    )
    parser.add_argument(
        "--exp-tag",
        default="exp1",
        type=str,
        required=False,
        help="Experiment tag",
    )
    parser.add_argument(
        "--max-new-test-cases-per-request",
        default=5,
        type=int,
        required=False,
    )

    return parser


def json_path_hook(data):
    for key, value in data.items():
        if isinstance(value, str) and "_path" in key:
            data[key] = Path(value)
    return data


def main(args: argparse.Namespace):
    logger = aux.setup_logger(log_file_path="generate.log")

    # check benchmark samples
    benchmark_sample_paths = sorted(glob(f"benchmark/{args.benchmark_version}/*"))
    num_of_samples = len(benchmark_sample_paths)

    logger.info(
        f"Found {num_of_samples} benchmark examples in benchmark/{args.benchmark_version}"
    )
    if num_of_samples == 0:
        raise ValueError("No benchmark samples found")

    output_dir = (
        args.outputs_dir_path
        / args.benchmark_version
        / args.edit_format
        / Path(args.model_to_eval)
    )
    print("Outputs will be stored in", output_dir)
    os.makedirs(Path(output_dir), exist_ok=True)

    random.shuffle(benchmark_sample_paths)

    # prepare repos for generation
    unique_repos = set([])
    benchmark_samples = []
    input_set = set([])
    print(f"Need to enhance: {len(benchmark_sample_paths)} test files samples.")
    env_values = dotenv_values(args.env_file)

    test_enhancer = LLMTestEnhancer(
        model_name=args.model_to_eval,
        base_url=args.llm_api_base_url,
        api_key=env_values["LLM_API_KEY"],
        edit_format=args.edit_format,
    )
    tasks_to_eval = args.tasks_to_eval.split(",")
    for sample_path in tqdm(benchmark_sample_paths, total=len(benchmark_sample_paths)):
        with open(sample_path, "rb") as f:
            sample = tomli.load(f)

        if sample["meta"]["task"] not in tasks_to_eval:
            print(f"Skipping because task is not in {tasks_to_eval}!")
            continue

        repo_name = sample["repo_info"]["repository"]
        tmp_path = args.work_folder / repo_name
        test_file_path = sample["input_info"]["test_file_path"]
        focal_file_path = sample["input_info"]["focal_file_path"]
        ext = test_file_path.split(".")[-1]

        output_path = output_dir / sample_path.split("/")[-1].replace(
            ".toml", f".{ext}"
        )

        if not args.force_gen_recompute:
            if output_path.is_file():
                print(f"Skipping because output already exists ({output_path}).")
                continue

        # rewrite a test file if needed
        test_file_content = sample["input_info"]["test_file_content"]
        try:
            if test_file_content is not None:
                print("Rewriting with sample content: ", end="\t")
                aux.write_to_file(tmp_path / test_file_path, test_file_content)

            changes = test_enhancer.generate(
                source_file_path=focal_file_path,
                test_file_path=test_file_path,
                language=sample["lang_info"]["lang"],
                repo_root=tmp_path,
            )

            test_enhancer.apply_changes(
                test_file_path=tmp_path / test_file_path,
                changes=changes,
                inplace=False,
                output_path=output_path,
            )
        except Exception as e:
            print("Exception: rewriting test file with its original content.")
            aux.copy_file(
                src=args.repo_folder / repo_name / test_file_path,
                dst=tmp_path / test_file_path,
            )
            raise e

        print("Rewrite test file with its original content.")
        aux.copy_file(
            src=args.repo_folder / repo_name / test_file_path,
            dst=tmp_path / test_file_path,
        )


def main_batch(args: argparse.Namespace):
    logger = aux.setup_logger(log_file_path="generate.log")

    # check benchmark samples
    benchmark_sample_paths = sorted(glob(f"benchmark/{args.benchmark_version}/*"))
    num_of_samples = len(benchmark_sample_paths)

    logger.info(
        f"Found {num_of_samples} benchmark examples in benchmark/{args.benchmark_version}"
    )
    if num_of_samples == 0:
        raise ValueError("No benchmark samples found")

    output_dir = (
        args.outputs_dir_path
        / args.benchmark_version
        / args.edit_format
        / Path(args.model_to_eval)
        / args.exp_tag
    )
    results_dir = (
        Path("results")
        / args.benchmark_version
        / args.edit_format
        / Path(args.model_to_eval)
        / args.exp_tag
    )
    print("Outputs will be stored in", output_dir)
    os.makedirs(Path(output_dir), exist_ok=True)
    print("Results will be stored in", results_dir)
    os.makedirs(Path(results_dir), exist_ok=True)

    # random.shuffle(benchmark_sample_paths)
    random.Random(73).shuffle(benchmark_sample_paths)

    # prepare repos for generation
    unique_repos = set([])
    benchmark_samples = []
    input_set = set([])
    print(f"Need to enhance: {len(benchmark_sample_paths)} test files samples.")
    paths_to_compute = []
    samples_to_compute = []

    env_values = dotenv_values(args.env_file)
    test_enhancer = LLMTestEnhancer(
        model_name=args.model_to_eval,
        base_url=args.llm_api_base_url,
        api_key=env_values["LLM_API_KEY"],
        edit_format=args.edit_format,
    )

    tasks_to_eval = args.tasks_to_eval.split(",")

    # 1. prepare samples
    for sample_path in tqdm(benchmark_sample_paths, total=len(benchmark_sample_paths)):

        with open(sample_path, "rb") as f:
            sample = tomli.load(f)

        if sample["meta"]["task"] not in tasks_to_eval:
            print(f"Skipping because task is not in {tasks_to_eval}!")
            continue

        repo_name = sample["repo_info"]["repository"]
        tmp_path = args.work_folder / repo_name
        test_file_path = sample["input_info"]["test_file_path"]
        ext = test_file_path.split(".")[-1]
        sample_name = sample_path.split("/")[-1].replace(".toml", "")
        output_path = output_dir / Path(sample_name + f".{ext}")
        result_save_path = results_dir / Path(sample_name + ".json")

        sample["_output_path"] = output_path
        sample["result_save_path"] = result_save_path

        prev_attempt = 0
        prev_result = {}
        if Path(result_save_path).is_file():
            try:
                with open(result_save_path, "r") as f:
                    prev_result = json.load(f)
            except Exception as e:
                print(f"Error loading {result_save_path}!")
                raise e
            prev_attempt = prev_result.get("attempt", 0)

        if prev_result.get("pipeline_status") == "generated":
            print("Skipping because pipe status is 'generated' (should be executed)!")
            continue

        if not args.force_gen_recompute and not (prev_attempt < args.max_attempts_num):
            print(
                f"Skipping because number of attempts exceeded ({prev_attempt}>={args.max_attempts_num})"
            )
            continue

        if prev_result.get("status") == "PASS":
            print(f"Skipping because previous result status=PASS.")
            continue

        test_file_content = None
        if (
            isinstance(prev_result.get("test_file_content"), str)
            or len(str(prev_result.get("test_file_content"))) > 10
        ):
            test_file_content = prev_result["test_file_content"]
            sample["fail_feedback"] = prev_result["fail_feedback"]
            sample["input_info"]["test_file_content"] = test_file_content

        if test_file_content is None:
            sample["fail_feedback"] = None
            test_file_content = sample["input_info"]["test_file_content"]

        samples_to_compute.append(sample)

        try:
            aux.write_to_file(tmp_path / test_file_path, test_file_content)
        except Exception as e:
            print("Exception: rewriting test file with its original content.")
            aux.copy_file(
                src=args.repo_folder / repo_name / test_file_path,
                dst=tmp_path / test_file_path,
            )
            raise e

        with open(result_save_path, "w") as f:
            prev_result["pipeline_status"] = "pending"
            prev_result["benchmark_sample"] = sample_path
            json.dump(prev_result, f, indent=4)

    if len(samples_to_compute) == 0:
        print("No samples to compute. Exiting.")
        return

    # 2. generate batch
    changes = test_enhancer.generate_batch(
        samples=[
            {
                "source_file_path": s["input_info"]["focal_file_path"],
                "test_file_path": s["input_info"]["test_file_path"],
                "language": s["lang_info"]["lang"],
                "repo_root": args.work_folder / s["repo_info"]["repository"],
                "result_save_path": s["result_save_path"],
                "fail_feedback": (
                    s.get("fail_feedback")
                    if isinstance(s.get("fail_feedback"), str)
                    else None
                ),
            }
            for s in samples_to_compute
        ],
        qwen3_disable_thinking=bool(args.qwen3_disable_thinking),
        max_new_test_cases_per_request=args.max_new_test_cases_per_request,
    )

    # 3. apply changes
    for change, sample in zip(changes, samples_to_compute):
        repo_name = sample["repo_info"]["repository"]
        test_file_path = sample["input_info"]["test_file_path"]
        tmp_path = args.work_folder / repo_name
        try:
            test_enhancer.apply_changes(
                test_file_path=tmp_path / test_file_path,
                changes=change,  # change["content"],
                inplace=False,
                output_path=sample["_output_path"],
                result_save_path=sample["result_save_path"],
            )
        except Exception as e:
            print(f"Exception: ({e})")
        finally:
            aux.copy_file(
                src=args.repo_folder / repo_name / test_file_path,
                dst=tmp_path / test_file_path,
            )


def dry_run(args: argparse.Namespace):
    benchmark_sample_paths = sorted(glob(f"benchmark/{args.benchmark_version}/*"))
    output_dir = (
        args.outputs_dir_path
        / args.benchmark_version
        / args.edit_format
        / Path(args.model_to_eval)
        / args.exp_tag
    )
    os.makedirs(Path(output_dir), exist_ok=True)

    for sample_path in tqdm(
        benchmark_sample_paths, total=len(benchmark_sample_paths), desc="Dry run:"
    ):
        with open(sample_path, "rb") as f:
            sample = tomli.load(f)
        repo_name = sample["repo_info"]["repository"]
        tmp_path = args.work_folder / repo_name
        test_file_path = sample["input_info"]["test_file_path"]
        ext = test_file_path.split(".")[-1]
        output_path = output_dir / sample_path.split("/")[-1].replace(
            ".toml", f".{ext}"
        )
        if sample["meta"]["scenario"] != "from_scratch":
            output_content = sample["input_info"]["test_file_content"]
        else:
            with open(tmp_path / test_file_path, "r", encoding="utf-8") as f:
                output_content = f.read()

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output_content)

    return


if __name__ == "__main__":
    args = get_arg_parser_obj().parse_args()
    if args.model_to_eval == "dry-run":
        print("Dry run...")
        dry_run(args)
    elif args.batch_generation:
        print("Batch generation mode activated!")
        main_batch(args)
    else:
        main(args)
