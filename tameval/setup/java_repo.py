import re
import copy
import typing
from typing import Any, ClassVar, Literal, override
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET


from setup.repo import Repo, Timer, RepoFailureError


@dataclass(kw_only=True)
class JavaRepo(Repo):
    lang: ClassVar[str | None] = "Java"
    java_version: str | None = None
    java_builder: Literal["Maven", "Gradle"] | None = None
    is_multi_module: bool = False

    # TODO we need to unify 2 different ways to check if the project is multi-module:
    # 1) parse pom.xml in self.is_multimodule_maven_project()
    # 2) check if module's pom.xml exist in .run_single_test()

    @override
    def get_lang_info(self) -> dict[str, Any]:
        """
        Should be extended in subclasses to include other fields
        """
        return {
            **super().get_lang_info(),
            "java_version": self.java_version,
            "java_builder": self.java_builder,
            "is_multi_module": self.is_multi_module,
        }

    @property
    @override
    def extension(self) -> str:
        return ".java"

    @override
    def resolve_lang_version(self):
        if (self.repo_dir / "pom.xml").is_file():
            self.java_builder = "Maven"
            self.java_version = self.find_java_version_in_pom()  # returns str | None
            self.is_multi_module = self.is_multimodule_maven_project()
        elif (self.repo_dir / "build.gradle").is_file():
            self.java_builder = "Gradle"

    @override
    def is_unit_test_file(self, file: Path) -> bool:
        if "integration" in str(file).lower():
            return False
        if "intTest" in str(file.parent):
            return False
        return file.name.endswith("Test.java") or file.name.endswith("Tests.java")

    @override
    def test_path_to_focal_name(self, test_path: Path) -> str:
        return (
            test_path.name.replace("Test.java", "").replace("Tests.java", "") + ".java"
        )

    @override
    def get_default_docker_image(self) -> str:
        image_from_dockerfile = self.get_docker_image_from_dockerfile()
        if image_from_dockerfile is not None and not "jre" in image_from_dockerfile:
            return image_from_dockerfile
        elif self.java_builder is None:
            raise RepoFailureError(
                "get_default_docker_image: java_builder is None",
                error_stage="get_default_docker_image",
            )
        elif self.java_builder == "Maven":
            mapping = {
                "7": "maven:3-jdk-7-onbuild-alpine",
                "1.7": "maven:3-jdk-7-onbuild-alpine",
                "8": "maven:3.8.6-jdk-8",
                "1.8": "maven:3.8.6-jdk-8",
                "11": "maven:3.8.4-openjdk-11",
                "17": "maven:3.8.3-openjdk-17",
                "21": "maven:3-amazoncorretto-21",
                "22": "maven:3.9.8-eclipse-temurin-22",
            }
            if (image := mapping.get(self.java_version, None)) is not None:
                return image
            return "maven:3.8.3-openjdk-17"
        elif self.java_builder == "Gradle":
            gradle_build_path = Path(self.working_dir / "build.gradle")
            with open(gradle_build_path, "r") as f:
                content = f.read()
            gradle_worker = GradleWorker(content)

            java_version = gradle_worker.get_java_version()

            if java_version:
                return f"gradle:jdk{java_version}"
            else:
                return "gradle:jdk17"  # maybe there is more default version
        else:
            raise RepoFailureError(
                "get_default_docker_image: Unknown java builder",
                error_stage="get_default_docker_image",
            )

    @override
    def _build_project(self, timeout: float, n_threads: int | str = "4"):
        with Timer(timeout=timeout) as timeout_clock:
            if self.java_builder == "Maven":
                try:
                    self._setup_jacoco_and_surefire(self.working_dir / "pom.xml")
                except (SyntaxError, FileNotFoundError) as e:
                    print(f"Cannot setup Jacoco: {e}")
                # except RuntimeError as e:  # no surefire plugin
                #     raise RepoFailureError(f'Error in _setup_jacoco_and_surefire: {e}') from e
                output = self.run_docker_command(
                    cmd=f"mvn -Dmaven.repo.local=/.m2 -T {n_threads!s} clean install",
                    timeout=timeout_clock.get_remaining_time(),
                    raise_on_error="on_timeout",
                )
                if (code := output.exit_code) != 0:
                    print(f"Exit code {code} for mvn install, trying mvn mackage")
                    self.run_docker_command(
                        cmd=f"mvn -Dmaven.repo.local=/.m2 -T {n_threads!s} clean package",
                        timeout=timeout_clock.get_remaining_time(),
                    )

            elif self.java_builder == "Gradle":

                gradle_build_path = Path(self.working_dir / "build.gradle")
                with open(gradle_build_path, "r") as f:
                    content = f.read()
                gradle_worker = GradleWorker(content)

                if not gradle_worker.has_jacoco():
                    updated_build_content = gradle_worker.append_jacoco()
                    gradle_build_path = Path(self.working_dir / "build.gradle")
                    with open(gradle_build_path, "w") as f:
                        f.write(updated_build_content)

                has_gradlew = (self.working_dir / "gradlew").exists()
                build_cmd = "./gradlew" if has_gradlew else "gradle"

                # TODO gradle caching
                # TODO: join commands with ||
                tasks = [
                    "build",  # Standard build process
                    "assemble",  # Fallback if 'build' is not available
                    "jar",  # Used for library projects
                    "shadowJar",  # For fat-jar projects (self-contained JARs with dependencies)
                    "war",  # If the project generates a .war file (for web applications)
                ]

                if has_gradlew:
                    self.run_docker_command(
                        cmd="chmod +x gradlew",
                        timeout=timeout_clock.get_remaining_time(),
                    )
                for task in tasks:
                    output = self.run_docker_command(
                        cmd=f"{build_cmd} {task}",
                        timeout=timeout_clock.get_remaining_time(),
                        raise_on_error="on_timeout",
                    )
                    if output.exit_code == 0:
                        break
                else:
                    raise CommandFailureError(output=output)
            else:
                raise RepoFailureError("Can build only Maven and Gradle projects")

    def _get_module_full_root_path_and_name(
        self, test_path: Path
    ) -> tuple[Path, str | None]:
        if (
            self.is_multi_module
            and len(test_path.parts) > 1
            and test_path.parts[1] == "src"
            and (self.repo_dir / test_path.parts[0] / "pom.xml").is_file()
        ):
            return self.working_dir / test_path.parts[0], test_path.parts[0]
        else:
            return self.working_dir, None

    def _extract_class_name(self, java_file_path: Path) -> str:
        """
        Extracts fully qualified class name from a Java file.

        Args:
            java_file_path: Path to the Java file

        Returns:
            Fully qualified class name (package.ClassName)
        Raises:
            OSError
                On various os errors when cannot read file
            UnicodeDecodeError
                If cannot decode file
        """
        full_path = self.working_dir / java_file_path

        file_content = full_path.read_text()
        package_match = re.search(r"package\s+([a-zA-Z0-9_.]+);", file_content)
        package = package_match.group(1) if package_match else ""

        if package:
            return f"{package}.{java_file_path.stem}"
        return java_file_path.stem

    def _setup_jacoco_and_surefire(self, pom_xml_path: str | Path):
        """
        Accepts pom.xml path. Sets `<phase>test</phase>` for execution with `<goal>report</goal>` for
        `org.jacoco` plugin, so that jacoco.xml coverage report is created on `mvn test` phase. If jacoco
        plugin is not found in pom.xml, tries to insert it. Writes the updated file to the same path.

        (disabled): Also sets failIfNoTests=True, failIfNoSpecifiedTests=True in surefire
        (we cannot override pom.xml values from command line, need to ensure they are right)

        Raises:
            SyntaxError
                On XML parsing errors
            OSError (including FileNotFoundError)
                On OS errors
        """
        # TODO where to seach for Jacoco: in <plugins/>, <pluginManagement/> or both?

        ET.register_namespace(
            "", "http://maven.apache.org/POM/4.0.0"
        )  # warning: this is a global operation
        tree = ET.parse(pom_xml_path)
        ns = {"": "http://maven.apache.org/POM/4.0.0"}

        self.misc["jacoco_setup_report"] = report = []

        # get or create project/build/plugins node
        build_node, was_created = get_or_create_xml_node(
            tree.getroot(), "build", namespaces=ns
        )
        if was_created:
            report.append("project/build node created")

        plugins_node, was_created = get_or_create_xml_node(
            build_node, "plugins", namespaces=ns
        )
        if was_created:
            report.append("project/build/plugins node created")

        # get jacoco node if exists
        jacoco_node: ET.Element | None = plugins_node.find(
            'plugin[artifactId="jacoco-maven-plugin"]', namespaces=ns
        )

        # determine jacoco version
        if jacoco_node is not None and (
            version_node := jacoco_node.find(".//version", namespaces=ns)
        ):
            jacoco_version = version_node.text
            report.append(f"jacoco version found: {jacoco_version}")
        else:
            jacoco_version = "0.8.10"
            report.append(f"jacoco version set to default: {jacoco_version}")

        # define function to add jacoco node
        jacoco_template_node = ET.fromstring(
            f"""
        <plugin>
            <groupId>org.jacoco</groupId>
            <artifactId>jacoco-maven-plugin</artifactId>
            <version>{jacoco_version}</version>
            <executions>
                <execution>
                    <id>jacoco-initialize</id>
                    <goals><goal>prepare-agent</goal></goals>
                </execution>
                <execution>
                    <id>report</id>
                    <phase>test</phase>
                    <goals><goal>report</goal></goals>
                </execution>
            </executions>
        </plugin>
        """
        )

        if jacoco_node is not None:
            report.append("jacoco node found")

            executions_node, was_created = get_or_create_xml_node(
                jacoco_node, "executions", namespaces=ns
            )
            if was_created:
                report.append("executions node created")

            report_node = executions_node.find(
                './/goals[goal="report"]...', namespaces=ns
            )
            if report_node is not None:
                report.append(
                    'execution node for goal="report" exists, setting phase="test"'
                )
                phase_node, was_created = get_or_create_xml_node(
                    report_node, "phase", namespaces=ns
                )
                if was_created:
                    report.append("phase node created")
                phase_node.text = "test"
            else:
                report.append(
                    'execution node for goal="report" does not exist, inserting from template'
                )
                executions_node.append(
                    jacoco_template_node.find('.//goals[goal="report"]...')
                )

        else:
            report.append("jacoco node does not exist, inserting from template")
            plugins_node.append(jacoco_node := jacoco_template_node)

        node_to_report = copy.deepcopy(jacoco_node)
        ET.indent(node_to_report, space="\t", level=0)
        self.misc["jacoco_xml"] = ET.tostring(node_to_report, encoding="unicode")

        tree.write(pom_xml_path)

    def _setup_pitest(
        self,
        pom_xml_path: str | Path,
        target_classes: list[str],
        target_tests: list[str],
    ) -> None:
        """
        Sets up PIT (Pitest) mutation testing framework for Maven projects.

        Args:
            pom_xml_path: Path to the pom.xml file
            target_classes: List of classes to mutate (e.g., ["com.example.Calculator"])
            target_tests: List of test classes to run (e.g., ["com.example.CalculatorTest"])

        Raises:
            SyntaxError: On XML parsing errors
            OSError: On OS errors including FileNotFoundError
            UnicodeDecodeError: on file reading
        """
        ET.register_namespace("", "http://maven.apache.org/POM/4.0.0")
        tree = ET.parse(pom_xml_path)
        ns = {"": "http://maven.apache.org/POM/4.0.0"}

        build_node, was_created = get_or_create_xml_node(
            tree.getroot(), "build", namespaces=ns
        )
        plugins_node, was_created = get_or_create_xml_node(
            build_node, "plugins", namespaces=ns
        )
        pitest_node = plugins_node.find(
            'plugin[artifactId="pitest-maven"]', namespaces=ns
        )
        if pitest_node is None:
            # Create PIT plugin configuration
            pitest_template = f"""  
            <plugin>  
                <groupId>org.pitest</groupId>  
                <artifactId>pitest-maven</artifactId>  
                <version>1.9.11</version>  
                <configuration>  
                    <targetClasses>  
                        <param>{'</param><param>'.join(target_classes)}</param>  
                    </targetClasses>  
                    <targetTests>  
                        <param>{'</param><param>'.join(target_tests)}</param>  
                    </targetTests>  
                    <outputFormats>  
                        <param>XML</param>  
                    </outputFormats>  
                    <timestampedReports>false</timestampedReports>  
                    <mutators>  
                        <mutator>DEFAULTS</mutator>  
                    </mutators>  
                </configuration>  
            </plugin>  
            """
            pitest_node = ET.fromstring(pitest_template)
            plugins_node.append(pitest_node)
        else:
            # Update existing PIT configuration
            config_node, was_created = get_or_create_xml_node(
                pitest_node, "configuration", namespaces=ns
            )

            # Update targetClasses
            target_classes_node, was_created = get_or_create_xml_node(
                config_node, "targetClasses", namespaces=ns
            )
            target_classes_node.clear()
            for tc in target_classes:
                param = ET.SubElement(target_classes_node, "param")
                param.text = tc

            # Update targetTests
            target_tests_node, was_created = get_or_create_xml_node(
                config_node, "targetTests", namespaces=ns
            )
            target_tests_node.clear()
            for tt in target_tests:
                param = ET.SubElement(target_tests_node, "param")
                param.text = tt

            # Ensure XML output format
            output_formats_node, was_created = get_or_create_xml_node(
                config_node, "outputFormats", namespaces=ns
            )

            if output_formats_node.find('param[.="XML"]', namespaces=ns) is None:
                param = ET.SubElement(output_formats_node, "param")
                param.text = "XML"

            # Disable timestamped reports for easier finding of reports
            timestamped_node, was_created = get_or_create_xml_node(
                config_node, "timestampedReports", namespaces=ns
            )
            timestamped_node.text = "false"

        tree.write(pom_xml_path)

    def find_java_version_in_pom(self) -> str | None:
        try:
            tree = ET.parse(self.repo_dir / "pom.xml")
            root = tree.getroot()
            namespaces = {"maven": "http://maven.apache.org/POM/4.0.0"}
            properties = root.find(".//maven:properties", namespaces)

            def get_from_properties(name: str = "java.version") -> str | None:
                nonlocal properties, namespaces
                if properties is not None:
                    if (
                        value := properties.find(f"maven:{name}", namespaces)
                    ) is not None:
                        return value.text

            java_version_from_mvn_plugin = None
            # Search for maven-compiler-plugin version in the pom.xml
            for plugin in root.findall(
                ".//maven:build/maven:plugins/maven:plugin", namespaces
            ):
                artifact_id = plugin.find("maven:artifactId", namespaces).text
                if artifact_id == "maven-compiler-plugin":
                    configuration = plugin.find("maven:configuration", namespaces)
                    if configuration is not None:
                        target_version = configuration.find("maven:target", namespaces)
                        if target_version is not None:
                            java_version_from_mvn_plugin = target_version.text

            if java_version_from_mvn_plugin is not None:
                if (
                    match := re.fullmatch(r"1\.(\d)", java_version_from_mvn_plugin)
                ) is not None:
                    # old notation 1.7, 1.8
                    return match.group(1)
                elif java_version_from_mvn_plugin.isnumeric():
                    # new notation 9, 10, 11, ...,  21, ...
                    return java_version_from_mvn_plugin
                elif (
                    match := re.fullmatch(r"\$\{(.*)\}", java_version_from_mvn_plugin)
                ) is not None:
                    # link such as ${maven.compiler.target}
                    if (value := get_from_properties(match.group(1))) is not None:
                        return value

            # trying to find java.version as a last resort
            if (version := get_from_properties("java.version")) is not None:
                if (match := re.fullmatch(r"1\.(\d)", version)) is not None:
                    return match.group(1)
                return version
        except (
            ET.ParseError,
            FileNotFoundError,
            IsADirectoryError,
            UnicodeDecodeError,
        ) as e:
            return None

    def is_multimodule_maven_project(self):
        assert (pom_path := self.repo_dir / "pom.xml").is_file()
        try:
            root = ET.parse(pom_path).getroot()
            namespaces = {"": "http://maven.apache.org/POM/4.0.0"}
            if (modules_element := root.find("modules", namespaces)) is not None:
                if len(modules_element.findall("module", namespaces)) > 0:
                    return True
        except (ET.ParseError, UnicodeDecodeError):
            pass
        return False
