import json
from glob import glob
import pandas as pd
from abc import abstractmethod, ABC
from clearml import Task, TaskTypes
from dotenv import load_dotenv
from pathlib import Path


class Tracker(ABC):

    @abstractmethod
    def upload_eval_instance_result(self):
        raise NotImplementedError()

    @abstractmethod
    def upload_metrics(self):
        raise NotImplementedError()

    def calc_aggregated_metrics(self, results_folder_path: str):
        result_paths = glob(results_folder_path)
        results = []
        for result_path in result_paths:
            with open(result_path, "r") as f:
                json_result = json.load(f)
            results.append(json_result)
        df = pd.DataFrame(results)
        df = df[~df.status.isna()]

        def _round(num: float):
            try:
                return num.round(2)
            except Exception as e:
                # print(e)
                return 0.0

        def get_metrics_for_subset(
            _df: pd.DataFrame, subset: str = "", calc_table: bool = False
        ) -> dict:
            if len(_df) == 0:
                return {}

            if subset != "":
                subset = f"{subset.upper()}/"

            metrics = {
                f"{subset}count": len(_df),
                f"{subset}num_samples": len(
                    _df.drop_duplicates(subset=["benchmark_sample_path", "scenario"])
                ),
                f"{subset}mean_delta_test_cov": _round(_df["delta_test_cov"].mean()),
                f"{subset}mean_delta_mut_cov": _round(_df["delta_mut_cov"].mean()),
                f"{subset}median_delta_test_cov": _round(
                    _df["delta_test_cov"].median()
                ),
                f"{subset}median_delta_mut_cov": _round(_df["delta_mut_cov"].median()),
                f"{subset}mean_test_cov": _round(_df["test_cov"].mean()),
                f"{subset}mean_mut_cov": _round(_df["mut_cov"].mean()),
                f"{subset}mean_init_test_cov": _round(_df["initial_test_cov"].mean()),
                f"{subset}mean_init_mut_cov": _round(_df["initial_mut_cov"].mean()),
                f"{subset}mean_delta_test_case_num": _round(
                    _df["delta_test_case_num"].mean()
                ),
                f"{subset}pass_rate": round(
                    len(_df[_df["status"] == "PASS"]) / len(_df), 2
                ),
            }
            if calc_table:
                reason_df = (
                    _df[["fail_reason"]]
                    .fillna("pass")
                    .value_counts(dropna=False)
                    .reset_index()
                )
                reason_df["%"] = (reason_df["count"] / reason_df["count"].sum()) * 100
                reason_dct = reason_df.round(1).to_dict(orient="records")
                # print(pd.DataFrame.from_records(reason_dct))
                # print()
                metrics[f"{subset}fail_reason_table"] = reason_dct
            return metrics

        metrics = get_metrics_for_subset(df, calc_table=True)
        for task in df.task.unique():
            metrics.update(
                get_metrics_for_subset(
                    df[df.task == task], f"task_{task}", calc_table=True
                )
            )
        for scenario in df.scenario.unique():
            metrics.update(
                get_metrics_for_subset(
                    df[(df.scenario == scenario) & (df.task == "create")],
                    f"subtask_{scenario}",
                    calc_table=True,
                )
            )
        for lang in df.lang.unique():
            _df_lang = df[df.lang == lang]
            metrics.update(get_metrics_for_subset(_df_lang, f"overall_{lang}"))

        return metrics


class NaiveTracker(Tracker):
    def upload_eval_instance_result(self):
        pass

    def upload_metrics(self):
        pass


class ClearMLTracker(Tracker):
    def __init__(
        self, task_name: str, project_name: str = "tameval", env_path: str = ".env"
    ):
        load_dotenv(env_path)
        Task.force_store_standalone_script(True)
        self.client = Task.init(
            task_name=task_name,
            project_name=project_name,
            task_type=TaskTypes.inference,
            auto_resource_monitoring=False,
            auto_connect_streams=True,
        )
        self.client.set_script(repository="", commit="", diff="")

    def upload_eval_instance_result(self, eval_result: dict, name: str):
        self.client.connect(eval_result, name=name)

    def upload_metrics(self, metrics: dict):
        for key, value in metrics.items():
            subset = key.split("/")[0]
            if not key.endswith("_table"):
                self.client.get_logger().report_single_value(name=key, value=value)
            else:
                _iter_key = key.replace("fail_reason_table", "count")
                _df = pd.DataFrame.from_records(value)
                self.client.get_logger().report_table(
                    title=key.split("/")[-1],
                    series=key,
                    table_plot=_df,
                    iteration=metrics[_iter_key],
                )

        self.client.get_logger().report_histogram(
            title="Delta Test Coverage (mean)",
            series="delta_test_cov",
            values=[v for k, v in metrics.items() if "mean_delta_test_cov" in k],
            xlabels=[
                k.split("/")[0].replace("mean_delta_test_cov", "overall")
                for k, v in metrics.items()
                if "mean_delta_test_cov" in k
            ],
        )
        self.client.get_logger().report_histogram(
            title="Delta Mut Coverage (mean)",
            series="delta_mut_cov",
            values=[v for k, v in metrics.items() if "mean_delta_mut_cov" in k],
            xlabels=[
                k.split("/")[0].replace("mean_delta_mut_cov", "overall")
                for k, v in metrics.items()
                if "mean_delta_mut_cov" in k
            ],
        )
        self.client.get_logger().report_histogram(
            title="Test File Pass Rate",
            series="pass_rate",
            values=[v for k, v in metrics.items() if "pass_rate" in k],
            xlabels=[
                k.split("/")[0].replace("pass_rate", "overall")
                for k, v in metrics.items()
                if "pass_rate" in k
            ],
        )
