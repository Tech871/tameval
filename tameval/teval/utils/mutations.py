import os
import re
import json
import ruamel
from pathlib import Path
from ruamel.yaml import YAML
import xml.etree.ElementTree as ET
from typing import Literal, Tuple, Union, List

import warnings

warnings.filterwarnings("ignore")


class MutationsProcessor:
    def __init__(
        self,
        report_path: str,
        src_file_path: str,
        src_relative_filepath: str,
        report_type: Literal["pitest", "mutpy", "go-mutesting"],
        logger=None,
    ):
        self.report_path = report_path
        self.src_file_path = src_file_path
        self.src_relative_filepath = src_relative_filepath
        self.report_type = report_type
        self.logger = logger

    def process_report(self, time_of_test_command: int) -> Tuple[list, list, float]:
        self.verify_report_update(time_of_test_command)
        return self.parse_report()

    def verify_report_update(self, time_of_test_command: int):
        assert os.path.exists(
            self.report_path
        ), f'Fatal: Coverage report "{self.report_path}" was not found!'

        # Convert file modification time to milliseconds for comparison
        file_mod_time_ms = int(round(os.path.getmtime(self.report_path) * 1000))

        assert (
            file_mod_time_ms > time_of_test_command
        ), f"Fatal: The coverage report file was not updated after the test command. file_mod_time_ms: {file_mod_time_ms}, time_of_test_command: {time_of_test_command}. {file_mod_time_ms > time_of_test_command}"

    def parse_report(self):
        if self.report_type == "pitest":
            killed_mutants, total_mutants = self.parse_pitest_report()
        elif self.report_type == "mutpy":
            killed_mutants, total_mutants = self.parse_mutpy_report()
        elif self.report_type == "go-mutesting":
            killed_mutants, total_mutants = self.parse_go_mutesting_report()
        else:
            raise ValueError(f"Unknown report type: {self.report_type}")
        if total_mutants == 0:
            mut_cov = 0.0
        else:
            mut_cov = round((killed_mutants / total_mutants) * 100, 2)
        return killed_mutants, total_mutants, mut_cov

    def parse_pitest_report(self):
        tree = ET.parse(self.report_path)
        root = tree.getroot()

        mutations = root.findall(".//mutation")

        if not mutations:
            mutations = root.findall("mutation")

        killed_mutants = 0
        total_mutants = 0
        for mutation in mutations:
            source_file = mutation.find("sourceFile").text
            if not self.src_relative_filepath.endswith(source_file):
                continue

            status = mutation.get("status")

            # If status is not in attributes, try to find it as a child element
            if status is None:
                status_node = mutation.find("status")
                if status_node is not None:
                    status = status_node.text

            if status == "KILLED":
                killed_mutants += 1
                total_mutants += 1
            elif status == "SURVIVED":
                total_mutants += 1

        return killed_mutants, total_mutants

    def parse_mutpy_report(self):
        _path = Path(self.report_path)
        data = ruamel.yaml.YAML().load(_path.read_text())

        killed_mutants = 0
        total_mutants = 0
        for mutation in data["mutations"]:
            if mutation["status"] in ["incompetent", "timeout"]:
                continue

            if mutation["status"] == "killed":
                killed_mutants += 1

            total_mutants += 1

        return killed_mutants, total_mutants

    def parse_go_mutesting_report(self):
        with open(self.report_path, "r", encoding="utf-8") as f:
            output = f.read()
        output = re.sub(r"(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]", "", output)

        killed_mutants = 0
        total_mutants = 0
        pattern = re.compile(r'(PASS|FAIL|SKIP)\s+"[^"]+\.(\d+)" with checksum')
        for match in pattern.finditer(output):
            status, idx = match.group(1), match.group(2)
            if status == "PASS":
                killed_mutants += 1
                total_mutants += 1
            elif status == "FAIL":
                total_mutants += 1
        return killed_mutants, total_mutants
