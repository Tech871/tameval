import re
import tree_sitter
from tree_sitter_languages import get_parser

from typing import Dict, List

import warnings

warnings.simplefilter("ignore")


ts_parser = get_parser("go")


class ParsedFile:
    def __init__(self, content: str):
        self.content = content

        self.bytes_content = bytes(content, "utf8")
        self.tree = ts_parser.parse(self.bytes_content)
        self.file_content_no_comments = self._remove_comments(
            self.bytes_content, self.tree.root_node
        )

    def get_file_struct(self) -> Dict:
        """Main function to parse the sample content and return structured information."""

        num_of_lines = len([x for x in self.content.splitlines() if len(x) > 0])
        num_of_code_lines = len(
            [x for x in self.file_content_no_comments.splitlines() if len(x) > 0]
        )
        if num_of_code_lines < 3:
            return {
                "num_of_lines": num_of_lines,
                "num_of_code_lines": num_of_code_lines,
                "content_no_comment": self.file_content_no_comments,
                "tree": [],
            }
        funcs = self.get_functions()
        tests = self.get_test_funtions()

        self.file_struct = {
            # "tree": self.tree,
            "num_of_lines": num_of_lines,
            "num_of_code_lines": num_of_code_lines,
            "content_no_comment": self.file_content_no_comments,
            "functions": [func.text.decode("utf8") for func in funcs],
            "func_names": [
                func.child_by_field_name("name").text.decode() for func in funcs
            ],
            "tests": [func.text.decode("utf8") for func in tests],
            "test_names": [
                func.child_by_field_name("name").text.decode() for func in tests
            ],
            "imports": self.get_imports(),
            "package": self.get_package(),
        }
        self.file_struct["test_num"] = len(self.file_struct["tests"])
        self.file_struct["func_num"] = len(self.file_struct["functions"])
        self.file_struct["import_num"] = len(self.file_struct["imports"])
        return self.file_struct

    def get_functions(self):
        return self._find_node_by_type_and_name(
            self.tree.root_node,
            node_type="function_declaration",
            node_name=None,
        ) + self._find_node_by_type_and_name(
            self.tree.root_node,
            node_type="method_declaration",
            node_name=None,
        )

    def get_test_functions(self):
        functions = self._find_node_by_type_and_name(
            self.tree.root_node,
            node_type="function_declaration",
            node_name=None,
        )
        return [
            func for func in functions if self.is_unit_test(func.text.decode("utf8"))
        ]

    def get_imports(self):
        return [
            stm.text.decode("utf8")
            for stm in self._find_node_by_type_and_name(
                self.tree.root_node,
                node_type="import_spec",
                node_name=None,
            )
        ]

    @staticmethod
    def is_unit_test(go_function: str) -> bool:
        pattern = r"^func Test\w+\(t \*testing\.T\)"

        match = re.match(pattern, go_function)
        if match:
            function_name = match.group(0)
            if "integration(" not in function_name.lower():
                return True
        return False

    def get_package(self):
        return [
            stm.text.decode("utf8")
            for stm in self._find_node_by_type_and_name(
                self.tree.root_node,
                node_type="package_clause",
                node_name=None,
            )
        ]

    def remove_func_by_name(
        self, name: str | List[str], remove_unused_imports: bool = False
    ) -> str:
        """Remove the method with the given name from the code."""
        root_node = self.tree.root_node

        if isinstance(name, str):
            name = [name]

        # Find the method node
        _nodes_to_remove = self._find_node_by_type_and_name(
            root_node, name, node_type="function_declaration"
        )

        if not _nodes_to_remove:
            raise Exception(f"Method(s) '{name}' not found.")

        new_content = self._delete_nodes(_nodes_to_remove, self.content)

        if remove_unused_imports:
            imports = self.get_imports()
            new_root_node = ts_parser.parse(bytes(new_content, "utf8")).root_node
            unused_import_names = self.get_used_definitions(
                self.tree.root_node, imports
            ) - self.get_used_definitions(new_root_node, imports)
            unused_import_nodes = [
                node
                for node in self._find_node_by_type_and_name(
                    new_root_node, None, node_type="import_spec"
                )
                if node.text.decode() in unused_import_names
            ]
            new_content = self._delete_nodes(unused_import_nodes, new_content)
        if new_content[-1] != "\n":
            new_content += "\n"
        return new_content

    def _delete_nodes(self, nodes: List[tree_sitter.Node], content: str) -> str:
        result = []
        current_index = 0
        file_lines = content.splitlines()
        for node in nodes:

            start_line = node.start_point[0]
            end_line = node.end_point[0] + 1
            result.extend(file_lines[current_index:start_line])
            current_index = end_line

        result.extend(file_lines[current_index:])

        new_content = "\n".join(result)
        return new_content

    @staticmethod
    def get_used_definitions(node, dependencies):
        root_node = node

        def get_pack_alias(pack):
            pack = pack.split()[0]
            return pack.split("/")[-1].strip('"').split(".")[0]

        usage_counts = {dep: 1 for dep in dependencies}
        packs2dep = {get_pack_alias(dep): dep for dep in dependencies}
        # print(packs2dep)

        # usage_counts = {}
        def traverse(node):
            """Обход AST для подсчета использования зависимостей."""
            if node.type in {
                "identifier",
                "call_expression",
                "package_identifier",
            } and not node.parent.type in {"package_clause", "import_spec"}:
                name = node.text.decode("utf8").strip('"')
                if name in packs2dep:
                    usage_counts[packs2dep[name]] = (
                        usage_counts.get(packs2dep[name], 0) + 1
                    )

            for child in node.children:
                traverse(child)

        traverse(root_node)
        # print(usage_counts)
        return {name for name, count in usage_counts.items() if count > 1}

    def _find_node_by_type_and_name(
        self,
        root_node: tree_sitter.Node,
        node_name: str | List[str],
        node_type: str,
    ) -> List[tree_sitter.Node]:
        """Find the method node with the given name."""
        nodes_to_return = []

        if isinstance(node_name, str):
            node_name = [node_name]

        def traverse(node):
            if node.type == node_type:
                # Check if method name matches the target
                _name_node = node.child_by_field_name("name")

                if node_name is None:
                    nodes_to_return.append(node)
                elif _name_node and _name_node.text.decode("utf8") in node_name:
                    nodes_to_return.append(node)

                # if len(nodes_to_return) == len(node_name):
                #     return nodes_to_return
            # Traverse child nodes
            for child in node.children:
                result = traverse(child)
                if result:
                    return result

        traverse(root_node)
        return nodes_to_return

    def _remove_comments(self, byte_content: bytes, root_node: tree_sitter.Node):
        # Store all non-comment parts of the code
        non_comment_parts = []

        # Iterate through all nodes, filtering out comment nodes
        for node in root_node.children:
            if node.type not in ("line_comment", "block_comment", "comment"):
                non_comment_parts.append(
                    byte_content[node.start_byte : node.end_byte].decode()
                )

        return "".join(non_comment_parts)
