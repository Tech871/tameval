import re
from datetime import datetime
from typing import Mapping, Dict
from dataclasses import dataclass, asdict as dataclass_asdict

from teval.utils import coverage, mutations
from utils.syntax_checker import SyntaxChecker
from utils import aux, runner
from teval.parser.java import parser as java_parser
from teval.parser.go import parser as go_parser
from teval.parser.python import parser as py_parser

from typing import Literal, List, Dict


@dataclass
class Metrics:
    status: Literal["PASS", "FAIL"]
    fail_reason: Literal[
        "empty_file",
        "no_changes",
        "syntax_error",
        "execution_error",
        "runtime_error",
        "test_failed",
        "timeout",
        "mutations_failed",
    ] = None

    test_cov: float = None
    mut_cov: float = None
    test_case_num: int = None
    lines_covered: List[int] = None
    lines_missed: List[int] = None
    killed_mutants_num: int = None
    total_mutants_num: int = None

    meta: Mapping = None
    task: Literal["test_creation", "test_repair", "test_update"] = None
    lang: Literal["java", "go", "python"] = None
    scenario: str = None

    initial_test_cov: float = None
    initial_mut_cov: float = None
    initial_test_case_num: int = None

    delta_test_cov: float = None
    delta_mut_cov: float = None
    delta_test_case_num: int = None

    error_logs: str = None
    fail_feedback: str = None

    init_test_file_content: str = None
    test_file_content: str = None

    # responses: Dict[int, str] = None
    date: str = None

    def __post_init__(self):
        self.date = datetime.now().strftime("%Y-%m-%d %H:%M")
        print(f"\n\tStatus: {self.status}, fail_reason: {self.fail_reason}")
        if self.status == "PASS":
            print(f"test_cov: {self.test_cov}, mut_cov: {self.mut_cov}")
            print(
                f"delta_test_cov: {self.delta_test_cov}, delta_mut_cov: {self.delta_mut_cov}"
            )
        print()

    def set_bench_instance_meta(self, meta: Mapping) -> None:
        self.meta = meta
        self.task = meta["meta"]["task"]
        self.scenario = meta["meta"]["scenario"]
        self.lang = meta["lang_info"]["lang"].lower()

        self._set_fail_feedback()
        self._set_AST_stats()
        self._set_delta_metrics()

    def to_dict(self) -> Dict:
        output = dataclass_asdict(self)
        del output["meta"]
        return output

    def _set_delta_metrics(self) -> None:
        if self.meta is None:
            raise ValueError(
                "Benchmark sample meta is not set yet. Use .set_bench_instance_meta"
            )

        self.initial_test_cov = self.meta["coverage"]["coverage"]
        self.initial_mut_cov = self.meta["coverage"].get("mutation_kill_rate", None)

        if self.fail_reason == "no_changes":
            self.test_cov = self.initial_test_cov
            self.mut_cov = self.initial_mut_cov

        if self.status == "FAIL":
            self.delta_test_cov = 0.0
            self.delta_mut_cov = 0.0
            self.delta_test_case_num = 0
            self.test_cov = self.initial_test_cov
            self.mut_cov = self.initial_mut_cov
            self.test_case_num = self.initial_test_case_num
        else:
            if self.initial_test_cov is None:
                raise ValueError("Initial test coverage is not set!")
            self.delta_test_cov = round(self.test_cov - self.initial_test_cov, 2)

            if self.mut_cov is None or self.initial_mut_cov is None:
                self.delta_mut_cov = None
            else:
                self.delta_mut_cov = round(self.mut_cov - self.initial_mut_cov, 2)
            self.delta_test_case_num = self.test_case_num - self.initial_test_case_num

    def _set_fail_feedback(self):
        if self.fail_reason == "no_changes":
            self.fail_feedback = "You have to enhance this test file!"
        if self.fail_reason == "empty_file":
            self.fail_feedback = "Test file is empty! You have to provide new test suite or change existing"
        elif self.fail_reason == "timeout":
            self.fail_feedback = "Test file execution timed out!"
            self.fail_feedback += (
                "\nAnalyze test cases that may cause extra long execution and fix them!"
            )
        elif self.fail_reason == "syntax_error":
            self.fail_feedback = self.error_logs
        elif self.fail_reason == "execution_error":
            self.fail_feedback = self.parse_logs(self.error_logs)[-2500:]

    def _set_AST_stats(self) -> str:
        if self.lang == "java":
            parser_obj = java_parser.ParsedFile
        elif self.lang == "go":
            parser_obj = go_parser.ParsedFile
        elif self.lang == "python":
            parser_obj = py_parser.ParsedFile
        else:
            raise ValueError(f"Unsupported language: {self.lang}")

        init_parsed_file = parser_obj(self.init_test_file_content)
        out_parsed_file = parser_obj(self.test_file_content)

        self.initial_test_case_num = len(init_parsed_file.get_test_functions())

        if self.fail_reason == "PASS":
            self.test_case_num = len(out_parsed_file.get_test_functions())
        else:
            self.test_case_num = self.initial_test_case_num
        return

    def parse_logs(self, logs: str) -> str:
        if self.lang.lower() == "java":
            return self._parse_java_logs(logs)
        elif self.lang.lower() == "go":
            return self._parse_go_logs(logs)
        elif self.lang.lower() == "python":
            return self._parse_python_logs(logs)
        else:
            raise ValueError(f"parse_logs: Unsupported language: {self.lang}")

    def _parse_go_logs(self, logs: str) -> str:
        log_lines = logs.splitlines()
        filtered_logs = []
        for line in log_lines:
            if "[GIN" in line:
                continue
            if "- using env:" in line or "- using code:" in line:
                continue
            if len(line) < 5:
                continue
            filtered_logs.append(line)
        return "...\n" + "\n".join(filtered_logs)

    def _parse_java_logs(self, logs: str) -> str:
        MAVEN_STOPWORDS = [
            "For more information",
            "Help 1",
            "Re-run Maven",
            "To see the full stack",
            "Please refer to ",
            "container with uuid",
            "Failed to execute goal",
            # "FAILURE!",
        ]
        FULL_FILEPATH_REGEX = re.compile(r"[\w/.-]+/([^/]+)")
        log_type = [
            x[:6] for x in logs.splitlines() if "[ERROR] " in x or "[INFO] " in x
        ]
        log_message = re.split(r"\[ERROR\] |\[INFO\] ", logs)
        # filter out non error messages
        error_logs = map(
            lambda x: x[1],
            filter(
                lambda x: True if "[ERROR" in x[0] else False,
                zip(log_type, log_message[1:]),
            ),
        )
        error_logs = "".join(error_logs).splitlines()
        error_logs = [
            line
            for line in error_logs
            if all(stop not in line for stop in MAVEN_STOPWORDS)
        ]
        result_message = ""
        log_set = set([])
        for _log in error_logs:
            if _log not in log_set:
                log_set.add(_log)
                result_message += _log + "\n"
        return FULL_FILEPATH_REGEX.sub(r"/\1", result_message)

    def _parse_python_logs(self, logs: str) -> str:
        result = ""
        stoplist = [
            "Warning",
            "short test summary info",
            "generated xml file",
            "_ _ _ _ _ _ _",
            "warnings summary",
        ]

        for line in logs.splitlines():
            if any(x in line for x in stoplist):
                continue
            if len(line) < 4:
                continue
            line = re.sub(
                r"_HOME_/.local/lib/python\d+(\.\d+)?/site-packages/",
                "site-packages/",
                line,
            )
            line = line.replace("/usr/local/lib/python", "python")
            result += line + "\n"
        return "..." + result


class TestFileEvaluator:

    def __init__(
        self,
        lang: Literal["java", "go", "python"],
        repo_root_path: str,
        focal_file_path: str,
        test_file_path: str,
        test_run_command: str,
        mut_run_command: str,
        test_file_content: str,
        initial_test_file_content: str,
        cov_report_type: Literal["cobertura", "jacoco", "go_cover"],
        mut_report_type: Literal["pitest", "mutpy", "go-mutesting"],
        cov_report_path: str,
        mut_report_path: str,
        command_run_dir: str = ".",
        timeout: int = 300,
        check_no_changes: bool = True,
        mut_run_fallback_command: str = None,
    ):
        self.lang = lang
        self.repo_root_path = repo_root_path
        self.focal_file_path = focal_file_path
        self.test_file_path = test_file_path
        self.test_run_command = test_run_command
        self.mut_run_command = mut_run_command
        self.mut_run_fallback_command = mut_run_fallback_command
        self.test_file_content = test_file_content
        self.initial_test_file_content = initial_test_file_content
        self.command_run_dir = command_run_dir
        self.cov_report_type = cov_report_type
        self.cov_report_path = cov_report_path
        self.mut_report_type = mut_report_type
        self.mut_report_path = mut_report_path
        self.timeout = timeout
        self.check_no_changes = check_no_changes

        self.syntax_checker = SyntaxChecker(lang=lang.lower())

    def eval(self) -> Metrics:
        """
        Runs eval pipe for the given answer. Saves metrics to json
        """

        new_content = self.test_file_content

        if not new_content or len(new_content) == 0:
            print("Empty test file!")
            return Metrics(
                status="FAIL",
                fail_reason="empty_file",
                init_test_file_content=self.initial_test_file_content,
                test_file_content=new_content,
            )

        if self.check_no_changes:
            if self.initial_test_file_content == new_content:
                print("No changes in test file!")
                return Metrics(
                    status="FAIL",
                    fail_reason="no_changes",
                    init_test_file_content=self.initial_test_file_content,
                    test_file_content=new_content,
                )

        # rewrite a test file
        print("Rewriting test file...")
        aux.write_to_file(self.repo_root_path / self.test_file_path, new_content)

        syntax_check_result = self.syntax_checker.check(new_content)
        if not syntax_check_result["correct"]:
            return Metrics(
                status="FAIL",
                fail_reason="syntax_error",
                error_logs=self.syntax_checker.get_error_message(
                    new_content,
                    str(self.test_file_path),
                    syntax_check_result,
                    limit_message_n_chars=1200,
                ),
                init_test_file_content=self.initial_test_file_content,
                test_file_content=new_content,
            )

        # run test command
        print(f"Running {self.test_run_command}...")
        stdout, stderr, exit_code, time_of_test_command = runner.Runner.run_command(
            command=self.test_run_command,
            cwd=self.command_run_dir,
            timeout=self.timeout,
        )
        if exit_code == "timeout":
            print(f"Error test run. Exit code: {exit_code}")
            return Metrics(
                status="FAIL",
                fail_reason="timeout",
                error_logs=stdout + "\n" + stderr,
                init_test_file_content=self.initial_test_file_content,
                test_file_content=new_content,
            )
        if exit_code != 0:
            print(f"Error test run. Exit code: {exit_code}")
            logs = stdout + "\n" + stderr
            print(*logs.splitlines()[-10:], sep="\n")
            return Metrics(
                status="FAIL",
                fail_reason="execution_error",
                error_logs=logs,
                init_test_file_content=self.initial_test_file_content,
                test_file_content=new_content,
            )

        cov_processor = coverage.CoverageProcessor(
            report_path=self.cov_report_path,
            src_file_path=self.repo_root_path / self.focal_file_path,
            src_relative_filepath=self.focal_file_path,
            report_type=self.cov_report_type,
        )
        lines_covered, lines_missed, test_coverage_percent = (
            cov_processor.process_report(time_of_test_command=time_of_test_command)
        )

        # run mutations command
        print(f"Running {self.mut_run_command}...")
        if self.lang == "go":
            stdout, stderr, exit_code, time_of_mut_command = (
                runner.Runner.run_and_save_output(
                    command=self.mut_run_command,
                    file_path=self.mut_report_path,
                    cwd=self.command_run_dir,
                    timeout=self.timeout,
                )
            )
        else:
            stdout, stderr, exit_code, time_of_test_command = runner.Runner.run_command(
                command=self.mut_run_command,
                cwd=self.command_run_dir,
                timeout=self.timeout,
            )
        if exit_code != 0 and exit_code != 124:  # 124 is timeout code
            print(f"Mut run exit code: {exit_code}")
            logs = (stdout if stdout else "") + "\n" + (stderr if stderr else "")
            print(*logs.splitlines()[-10:], sep="\n")
            return Metrics(
                status="PASS",
                fail_reason="mutations_failed",
                test_cov=test_coverage_percent,
                lines_covered=lines_covered,
                lines_missed=lines_missed,
                error_logs=logs,
                init_test_file_content=self.initial_test_file_content,
                test_file_content=new_content,
            )

        mut_processor = mutations.MutationsProcessor(
            report_path=self.mut_report_path,
            src_file_path=self.repo_root_path / self.focal_file_path,
            src_relative_filepath=self.focal_file_path,
            report_type=self.mut_report_type,
        )
        try:
            killed_mutants_num, total_mutants_num, mut_cov_percent = (
                mut_processor.process_report(time_of_test_command=time_of_test_command)
            )
        except Exception as e:
            print(f"Error while processing mut report: {e}")
            return Metrics(
                status="PASS",
                fail_reason="mutations_failed",
                test_cov=test_coverage_percent,
                lines_covered=lines_covered,
                lines_missed=lines_missed,
                error_logs="mutation report processing failed!",
                init_test_file_content=self.initial_test_file_content,
                test_file_content=new_content,
            )
        if killed_mutants_num == 0 and self.mut_run_fallback_command:
            print("No mutants killed! Fallback...")
            stdout, stderr, exit_code, time_of_test_command = runner.Runner.run_command(
                command=self.mut_run_fallback_command,
                cwd=self.command_run_dir,
                timeout=self.timeout,
            )
            if exit_code == 0:
                try:
                    killed_mutants_num, total_mutants_num, mut_cov_percent = (
                        mut_processor.process_report(
                            time_of_test_command=time_of_test_command
                        )
                    )
                except Exception as e:
                    print(
                        f"Error while processing mut report: {e}. Use previous report."
                    )
                    pass
        return Metrics(
            status="PASS",
            test_cov=test_coverage_percent,
            lines_covered=lines_covered,
            lines_missed=lines_missed,
            mut_cov=mut_cov_percent,
            killed_mutants_num=killed_mutants_num,
            total_mutants_num=total_mutants_num,
            init_test_file_content=self.initial_test_file_content,
            test_file_content=new_content,
        )
