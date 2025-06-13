import os
import re
import csv
import json
import pandas as pd
import xml.etree.ElementTree as ET
from typing import Literal, Tuple, Union, List

import warnings

warnings.filterwarnings("ignore")


class CoverageProcessor:
    def __init__(
        self,
        report_path: str,
        src_file_path: str,
        src_relative_filepath: str,
        report_type: Literal["cobertura", "lcov", "jacoco", "go_cover"],
        logger=None,
    ):
        self.report_path = report_path
        self.src_file_path = src_file_path
        self.src_relative_filepath = src_relative_filepath
        self.coverage_type = report_type
        self.logger = logger

    def process_report(self, time_of_test_command: int) -> Tuple[list, list, float]:
        """
        Verifies the coverage report's existence and update time, and then
        parses the report based on its type to extract coverage data.
        """
        self.verify_report_update(time_of_test_command)
        return self.parse_coverage_report()

    def verify_report_update(self, time_of_test_command: int):
        assert os.path.exists(
            self.report_path
        ), f'Fatal: Coverage report "{self.report_path}" was not found!'

        # Convert file modification time to milliseconds for comparison
        file_mod_time_ms = int(round(os.path.getmtime(self.report_path) * 1000))

        assert (
            file_mod_time_ms > time_of_test_command
        ), f"Fatal: The coverage report file was not updated after the test command. file_mod_time_ms: {file_mod_time_ms}, time_of_test_command: {time_of_test_command}. {file_mod_time_ms > time_of_test_command}"

    def parse_coverage_report(self) -> Tuple[list, list, float]:
        if self.coverage_type == "cobertura":
            # Default behavior is to parse out a single file from the report
            lines_covered, lines_missed, coverage_percentage = self._parse_cobertura()
        elif self.coverage_type == "go_cover":
            lines_covered, lines_missed, coverage_percentage = self._parse_go_cover()
        elif self.coverage_type == "lcov":
            lines_covered, lines_missed, coverage_percentage = self._parse_lcov()
        elif self.coverage_type == "jacoco":
            lines_covered, lines_missed, coverage_percentage = self._parse_jacoco()
        else:
            raise ValueError(f"Unsupported coverage report type: {self.coverage_type}")
        return lines_covered, lines_missed, round(coverage_percentage, 2)

    def _parse_cobertura(self) -> Union[Tuple[list, list, float], dict]:
        """
        Parses a Cobertura XML code coverage report to extract covered and missed line numbers for a specific file
        or all files, and calculates the coverage percentage.
        """
        tree = ET.parse(self.report_path)
        root = tree.getroot()
        lines_covered = []
        lines_missed = []

        for cls in root.findall(".//class"):
            name_attr = cls.get("filename")
            cls_name = cls.get("name")

            if (
                name_attr
                and name_attr.endswith(self.src_relative_filepath)
                or self.src_relative_filepath.endswith(name_attr)
            ):
                _covered, _missed, _ = self._parse_coverage_data_for_class(cls)
                lines_covered.extend(_covered)
                lines_missed.extend(_missed)
        total_lines = len(lines_covered) + len(lines_missed)
        coverage_percentage = (
            (len(lines_covered) / total_lines) * 100 if total_lines > 0 else 0
        )
        return lines_covered, lines_missed, coverage_percentage

    def _parse_go_cover(self):
        df = pd.read_csv(
            self.report_path,
            sep=" ",
            skiprows=1,
            header=None,
            names=["location", "num_statements", "hit"],
        )
        assert len(df.columns) == 3, f"Wrong n columns {len(df.columns)}"
        df[["file", "range"]] = df["location"].str.split(":", n=1, expand=True)
        df = df[df["file"].apply(lambda x: x.endswith(self.src_relative_filepath))]

        def extract_lines(range_str):
            start, end = range_str.split(",")
            start_line = int(start.split(".")[0])
            end_line = int(end.split(".")[0])
            return list(range(start_line, end_line + 1))

        lines_covered, lines_missed = [], []
        for _, row in df.iterrows():
            lines = extract_lines(row["range"])
            if row["hit"] == 1:
                lines_covered.extend(lines)
            else:
                lines_missed.extend(lines)
        lines_covered = sorted(set(lines_covered))
        lines_missed = sorted(set(lines_missed))
        coverage_percentage = (
            df[df["hit"] == 1]["num_statements"].sum()
            / df["num_statements"].sum()
            * 100
        )
        return lines_covered, lines_missed, coverage_percentage

    def _parse_coverage_data_for_class(self, cls) -> Tuple[list, list, float]:
        """
        Parses coverage data for a single class.
        """
        lines_covered, lines_missed = [], []

        for line in cls.findall(".//line"):
            line_number = int(line.get("number"))
            hits = int(line.get("hits"))
            if hits > 0:
                lines_covered.append(line_number)
            else:
                lines_missed.append(line_number)

        total_lines = len(lines_covered) + len(lines_missed)
        coverage_percentage = (
            (len(lines_covered) / total_lines) * 100 if total_lines > 0 else 0
        )
        return lines_covered, lines_missed, coverage_percentage

    def _parse_lcov(self):

        lines_covered, lines_missed = [], []
        filename = os.path.basename(self.src_file_path)
        try:
            with open(self.report_path, "r") as file:
                for line in file:
                    line = line.strip()
                    if line.startswith("SF:"):
                        if line.endswith(filename):
                            for line in file:
                                line = line.strip()
                                if line.startswith("DA:"):
                                    line_number = line.replace("DA:", "").split(",")[0]
                                    hits = line.replace("DA:", "").split(",")[1]
                                    if int(hits) > 0:
                                        lines_covered.append(int(line_number))
                                    else:
                                        lines_missed.append(int(line_number))
                                elif line.startswith("end_of_record"):
                                    break

        except (FileNotFoundError, IOError) as e:
            self.logger.error(f"Error reading file {self.report_path}: {e}")
            raise

        total_lines = len(lines_covered) + len(lines_missed)
        coverage_percentage = (
            (len(lines_covered) / total_lines) if total_lines > 0 else 0
        )
        coverage_percentage = round(coverage_percentage * 100, 2)
        return lines_covered, lines_missed, coverage_percentage

    def _parse_jacoco(self) -> Tuple[list, list, float]:
        """
        Parses a JaCoCo XML code coverage report to extract covered and missed line numbers for a specific file,
        and calculates the coverage percentage.
        """
        lines_covered, lines_missed = [], []
        source_file_extension = self.get_file_extension(self.src_file_path)

        package_name, class_name = "", ""
        if source_file_extension == "java":
            package_name, class_name = self._extract_package_and_class_java()
        else:
            raise ValueError(
                f"Unsupported Language: {source_file_extension}. Using default Java logic."
            )

        file_extension = self.get_file_extension(self.report_path)

        missed, covered = 0, 0
        if file_extension == "xml":
            missed, covered, lines_missed, lines_covered = (
                self._parse_missed_covered_lines_jacoco_xml(class_name)
            )
        elif file_extension == "csv":
            missed, covered = self._parse_missed_covered_lines_jacoco_csv(
                package_name, class_name
            )
        else:
            raise ValueError(
                f"Unsupported JaCoCo code coverage report format: {file_extension}"
            )

        total_lines = missed + covered
        coverage_percentage = (float(covered) / total_lines) if total_lines > 0 else 0
        coverage_percentage = round(coverage_percentage * 100, 2)
        return lines_covered, lines_missed, coverage_percentage

    def _parse_missed_covered_lines_jacoco_xml(
        self, class_name: str
    ) -> tuple[int, int]:
        """Parses a JaCoCo XML code coverage report to extract covered and missed line numbers for a specific file."""
        tree = ET.parse(self.report_path)
        root = tree.getroot()
        sourcefile = root.find(
            f".//sourcefile[@name='{class_name}.java']"
        ) or root.find(f".//sourcefile[@name='{class_name}.kt']")

        if sourcefile is None:
            return 0, 0, 0, 0

        missed_count, covered_count = 0, 0
        for counter in sourcefile.findall("counter"):
            if counter.attrib.get("type") == "LINE":
                missed_count += int(counter.attrib.get("missed", 0))
                covered_count += int(counter.attrib.get("covered", 0))
                break

        # define (un)covered line numbers
        uncovered_lines = []
        covered_lines = []
        for line in sourcefile.findall("line"):
            if int(line.get("mi", 0)) > 0:
                uncovered_lines.append(int(line.get("nr")))
            covered_lines.append(int(line.get("nr")))

        return missed_count, covered_count, uncovered_lines, covered_lines

    def _parse_missed_covered_lines_jacoco_csv(
        self, package_name: str, class_name: str
    ) -> tuple[int, int]:
        with open(self.report_path, "r") as file:
            reader = csv.DictReader(file)
            missed, covered = 0, 0
            for row in reader:
                if row["PACKAGE"] == package_name and row["CLASS"] == class_name:
                    try:
                        missed = int(row["LINE_MISSED"])
                        covered = int(row["LINE_COVERED"])
                        break
                    except KeyError as e:
                        self.logger.error(f"Missing expected column in CSV: {str(e)}")
                        raise

        return missed, covered

    def _extract_package_and_class_java(self):
        package_pattern = re.compile(r"^\s*package\s+([\w\.]+)\s*;.*$")
        class_pattern = re.compile(r"^\s*public\s+class\s+(\w+).*")

        package_name = ""
        class_name = ""
        try:
            with open(self.src_file_path, "r") as file:
                for line in file:
                    if not package_name:  # Only match package if not already found
                        package_match = package_pattern.match(line)
                        if package_match:
                            package_name = package_match.group(1)

                    if not class_name:  # Only match class if not already found
                        class_match = class_pattern.match(line)
                        if class_match:
                            class_name = class_match.group(1)

                    if package_name and class_name:  # Exit loop if both are found
                        break
        except (FileNotFoundError, IOError) as e:
            self.logger.error(f"Error reading file {self.src_file_path}: {e}")
            raise

        return package_name, class_name

    def get_file_extension(self, filename: str) -> str | None:
        """Get the file extension from a given filename."""
        return os.path.splitext(filename)[1].lstrip(".")
