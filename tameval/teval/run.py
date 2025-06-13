import os
import json
import time
import tomli
import random
import argparse
import subprocess
from glob import glob
from tqdm import tqdm
from pathlib import Path

from typing import List, Dict, Iterable

from utils import aux
from teval.evaluator import TestFileEvaluator
from teval.utils.tracking import ClearMLTracker


random.seed(42)


def get_arg_parser_obj(parents=None):
    """
    Parse command line arguments.
    """

    parser = argparse.ArgumentParser(
        description=f"TAM-eval: Evaluation Framework for Automated Unit Test Maintenance",
        parents=parents,
    )
    parser.add_argument(
        "--model-to-eval",
        type=str,
        required=True,
        help="Model to eval inside the outputs folder. Glob regex.",
        default="gpt4",
    )

    parser.add_argument(
        "--edit-format",
        type=str,
        required=True,
        help="Edit format that was used for generation.",
        default="whole",
    )
    parser.add_argument(
        "--benchmark-version",
        type=str,
        required=True,
        help="Version of the benchmark to eval.",
        default="v1",
    )
    parser.add_argument(
        "--work-folder", type=Path, default="tmp", help="Path to the work folder."
    )
    parser.add_argument(
        "--repo-folder",
        type=Path,
        default="../download/repo",
        help="Path to where downloaded repos located.",
    )
    parser.add_argument(
        "--result-folder",
        type=Path,
        default="results",
        help="Path to the result folder.",
    )

    parser.add_argument(
        "--force-clean-folders",
        default=1,
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
        "--env-file",
        default="teval/.env",
        type=str,
        required=False,
        help=".env file",
    )
    parser.add_argument(
        "--force-eval-recompute",
        default=0,
        type=int,
        required=False,
        help="Force recompute already computed samples",
    )
    parser.add_argument(
        "--langs-to-eval",
        default="java,go,kotlin,python",
        type=str,
        required=False,
        help="Languages to eval",
    )
    parser.add_argument(
        "--tasks-to-eval",
        default="create,update,repair",
        type=str,
        required=False,
        help="Tasks to eval",
    )
    parser.add_argument(
        "--timeout",
        default=150,
        type=int,
        required=False,
        help="Timeout for command runner",
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
        "--exp-tag",
        default="exp1",
        type=str,
        required=False,
        help="Experiment tag",
    )
    return parser


def json_path_hook(data):
    for key, value in data.items():
        if isinstance(value, str) and "_path" in key:
            data[key] = Path(value)
    return data


def main(args: argparse.Namespace):
    logger = aux.setup_logger(log_file_path="eval.log")

    # prepare: check outputs to eval
    result_folder_glob = f"results/{args.benchmark_version}/{args.edit_format}/{args.model_to_eval}/{args.exp_tag}/*"
    all_samples = glob(result_folder_glob)
    if len(all_samples) == 0:
        print(f"No all_results found in {result_folder_glob}!")
        return

    samples_to_eval = []
    for result_path in all_samples:
        with open(result_path, "r") as f:
            result = json.load(f)
        if result["pipeline_status"] == "generated":
            result["_result_path"] = result_path
            samples_to_eval.append(result)

    if len(samples_to_eval) == 0:
        print("No results to eval (with pipeline_status='generated')!")
        return

    num_of_samples_to_eval = len(samples_to_eval)
    random.shuffle(samples_to_eval)

    # prepare: init experiment tracker
    tracker = None
    if args.clearml_task_name is not None and args.clearml_task_name:
        clearml_task_name = f"{args.model_to_eval} | {args.edit_format} | {args.exp_tag}: {args.clearml_task_name}"
        tracker = ClearMLTracker(task_name=clearml_task_name, env_path=args.env_file)

    ### **evaluation loop** ###
    tasks_to_eval = args.tasks_to_eval.split(",")

    for itr, sample_to_eval in tqdm(
        enumerate(samples_to_eval), total=len(samples_to_eval)
    ):
        benchmark_sample_path = sample_to_eval["benchmark_sample"]
        print(f"\n\nStarted processing {benchmark_sample_path}...")
        with open(benchmark_sample_path, "rb") as f:
            sample = tomli.load(f)

        lang = sample["lang_info"]["lang"].lower()
        repo_name = sample["repo_info"]["repository"]
        tmp_path = args.work_folder / repo_name
        test_file_path = sample["input_info"]["test_file_path"]
        focal_file_path = sample["input_info"]["focal_file_path"]

        if sample["meta"]["task"] not in tasks_to_eval:
            print(f"Skipping because task is not in {tasks_to_eval}!")
            continue

        if not args.force_eval_recompute:
            if not (sample_to_eval.get("attempt", 0) < args.max_attempts_num):
                print(
                    f"Skipping because number of attempts exceeded (>={args.max_attempts_num})"
                )
                continue

        # run info
        image = sample["run_info"]["docker_image"]
        docker_wrap = sample["run_info"]["docker_wrap"]
        test_run_command = sample["run_info"]["test_run_command"]
        mut_run_command = sample["run_info"]["mutation_run_command"]
        docker_test_run_command = docker_wrap.format(
            proj_path="$(pwd)/" + str(tmp_path),
            host="/",
            img=image,
            cmd=test_run_command,
        )
        docker_mut_run_command = docker_wrap.format(
            proj_path="$(pwd)/" + str(tmp_path),
            host="/",
            img=image,
            cmd=mut_run_command,
        )
        mut_run_fallback_command = None
        if sample["run_info"].get("mut_run_fallback_command", None) is not None:
            mut_run_fallback_command = docker_wrap.format(
                proj_path="$(pwd)/" + str(tmp_path),
                host="/",
                img=image,
                cmd=sample["run_info"]["mut_run_fallback_command"],
            )

        # init evaluator object
        evaluator = TestFileEvaluator(
            lang=lang,
            initial_test_file_content=sample["input_info"]["test_file_content"],
            test_file_content=sample_to_eval["test_content_to_eval"],
            repo_root_path=tmp_path,
            focal_file_path=focal_file_path,
            test_file_path=test_file_path,
            cov_report_type=sample["run_info"]["coverage_report_type"],
            cov_report_path=tmp_path / sample["run_info"]["coverage_report_path"],
            mut_report_type=sample["run_info"]["mutation_report_type"],
            mut_report_path=tmp_path / sample["run_info"]["mutation_report_path"],
            test_run_command=docker_test_run_command,
            mut_run_command=docker_mut_run_command,
            mut_run_fallback_command=mut_run_fallback_command,
            timeout=args.timeout,
            check_no_changes=args.model_to_eval != "dry-run",
        )

        try:
            start_time = time.time()
            eval_result = evaluator.eval()
            elapsed_time = time.time() - start_time
            eval_result.set_bench_instance_meta(sample)
            eval_result = eval_result.to_dict()

            # add some info
            eval_result["eval_time"] = round(elapsed_time, 2)
            eval_result["benchmark_sample_path"] = benchmark_sample_path
            eval_result["edit_format"] = args.edit_format
            eval_result["model"] = args.model_to_eval
            eval_result["lang"] = lang
            eval_result["repo"] = repo_name
            eval_result["attempt"] = sample_to_eval.get("attempt", 0) + 1

            # if tracker is not None:
            #     tracker.upload_eval_instance_result(
            #         eval_result, name=sample_to_eval["_result_path"].split("/")[-1]
            #     )
        except Exception as e:
            print("Exception: rewrite test file with its original content.")
            raise e
        finally:
            print("Rewrite test file with its original content.")
            aux.copy_file(
                src=args.repo_folder / repo_name / test_file_path,
                dst=tmp_path / test_file_path,
            )

        sample_to_eval.update(eval_result)
        sample_to_eval["attempts"] = sample_to_eval.get("attempts", []) + [eval_result]
        sample_to_eval["pipeline_status"] = "executed"

        # save result
        with open(sample_to_eval["_result_path"], "w") as f:
            json.dump(sample_to_eval, f, indent=4)

        if tracker is not None and itr % 15 == 0:
            tmp_metrics = tracker.calc_aggregated_metrics(
                results_folder_path=result_folder_glob
            )
            tracker.upload_metrics(tmp_metrics)

    print(f"Finish...")
    if tracker is not None:
        tmp_metrics = tracker.calc_aggregated_metrics(
            results_folder_path=result_folder_glob
        )
        tracker.upload_metrics(tmp_metrics)
        print(f"Metrics uploaded...")
        tracker.client.close()
    return


if __name__ == "__main__":
    args = get_arg_parser_obj().parse_args()
    main(args)
