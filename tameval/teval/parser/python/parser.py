import re
import tree_sitter
from tree_sitter_languages import get_parser
from typing import Dict, List
import warnings

warnings.simplefilter("ignore")


ts_parser = get_parser("python")


class ParsedFile:
    def __init__(self, content: str):
        self.content = content
        self.bytes_content = content.encode("utf8")
        self.tree = ts_parser.parse(self.bytes_content)
        self.file_content_no_comments = self._remove_comments(
            self.bytes_content, self.tree.root_node
        )

    def get_file_struct(self) -> Dict:
        num_of_lines = len([x for x in self.content.splitlines() if x.strip()])
        num_of_code_lines = len(
            [x for x in self.file_content_no_comments.splitlines() if x.strip()]
        )

        if num_of_code_lines < 3:
            return {
                "num_of_lines": num_of_lines,
                "num_of_code_lines": num_of_code_lines,
                "content_no_comment": self.file_content_no_comments,
                "tree": [],
            }

        funcs = self.get_functions()
        tests = self.get_test_functions()

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
        }
        self.file_struct["test_num"] = len(self.file_struct["tests"])
        self.file_struct["func_num"] = len(self.file_struct["functions"])
        self.file_struct["import_num"] = len(self.file_struct["imports"])
        return self.file_struct

    def get_functions(self):
        return self._find_node_by_type(
            self.tree.root_node, "function_definition"
        ) + self._find_node_by_type(self.tree.root_node, "method_definition")

    def get_test_functions(self):
        functions = self.get_functions()
        return [
            func for func in functions if self.is_unit_test(func.text.decode("utf8"))
        ]

    def get_imports(self):
        imports = []
        for stm in self._find_node_by_type(self.tree.root_node, "import_statement"):
            text = stm.text.decode("utf8")
            match = re.match(r"^(?:import|from) ([\w\.]+)", text)
            if match:
                imports.append(match.group(1).split(".")[0])
        return imports

    @staticmethod
    def is_unit_test(py_function: str) -> bool:
        return re.match(r"^def test_\w+\(", py_function) is not None

    def remove_func_by_name(
        self, name: str | List[str], remove_unused_imports: bool = False
    ) -> str:
        if isinstance(name, str):
            name = [name]

        nodes_to_remove = self._find_nodes_to_remove(self.tree.root_node, name)

        if not nodes_to_remove:
            raise Exception(f"Function(s) '{name}' not found.")

        new_content = self._delete_nodes(nodes_to_remove, self.content)

        new_tree = ts_parser.parse(new_content.encode("utf8"))
        new_root_node = new_tree.root_node

        new_content = self._remove_empty_classes(new_root_node, new_content)

        new_content = self._remove_unused_fixtures(new_content)

        if remove_unused_imports:
            imports = self.get_imports()
            updated_root = ts_parser.parse(new_content.encode("utf8")).root_node
            unused_imports = set(imports) - set(
                self.get_imports_from_tree(updated_root)
            )
            new_content = self._remove_unused_imports(new_content, unused_imports)

        return new_content + "\n" if not new_content.endswith("\n") else new_content

    def _remove_empty_classes(self, root_node, content: str) -> str:
        lines = content.splitlines()
        nodes_to_remove = []

        for node in root_node.children:
            if node.type == "decorated_definition":
                class_def = next(
                    (c for c in node.children if c.type == "class_definition"), None
                )
                if class_def and self._is_class_empty(class_def):
                    nodes_to_remove.append(node)
            elif node.type == "class_definition" and self._is_class_empty(node):
                nodes_to_remove.append(node)

        return self._delete_nodes(nodes_to_remove, content)

    def _is_class_empty(self, class_node: tree_sitter.Node) -> bool:
        body = next((c for c in class_node.children if c.type == "block"), None)
        if not body:
            return True
        return all(
            child.type not in {"function_definition", "decorated_definition"}
            for child in body.children
        )

    def _remove_unused_fixtures(self, content: str) -> str:

        fixture_defs = re.findall(
            r"@pytest\.fixture.*?\ndef (\w+)\(", content, re.DOTALL
        )
        used_names = set(re.findall(r"\b(\w+)\b", content))

        unused_fixtures = [name for name in fixture_defs if name not in used_names]

        if not unused_fixtures:
            return content

        new_tree = ts_parser.parse(content.encode("utf8"))
        root_node = new_tree.root_node
        nodes_to_remove = []

        for node in root_node.children:
            if node.type == "decorated_definition":
                func_def = next(
                    (c for c in node.children if c.type == "function_definition"), None
                )
                if func_def:
                    name_node = next(
                        (c for c in func_def.children if c.type == "identifier"), None
                    )
                    if name_node and name_node.text.decode("utf8") in unused_fixtures:
                        nodes_to_remove.append(node)

        return self._delete_nodes(nodes_to_remove, content)

    def _find_nodes_to_remove(
        self, root_node, names: List[str]
    ) -> List[tree_sitter.Node]:
        nodes_to_remove = []

        for name in names:
            function_nodes = self._find_node_by_name(
                root_node, name, "function_definition"
            )
            if not function_nodes:
                continue

            node_to_remove = max(function_nodes, key=lambda n: n.start_point)
            nodes_to_remove.append(node_to_remove)

            decorator_nodes = self._find_decorators(node_to_remove)
            nodes_to_remove.extend(decorator_nodes)

        return nodes_to_remove

    def _find_decorators(
        self, function_node: tree_sitter.Node
    ) -> List[tree_sitter.Node]:
        decorators = []
        for child in function_node.parent.children:
            if (
                child.type == "decorator"
                and child.end_point[0] < function_node.start_point[0]
            ):
                decorators.append(child)
        return decorators

    def _delete_nodes(self, nodes: List[tree_sitter.Node], content: str) -> str:
        lines = content.splitlines()
        result_lines = []
        current_index = 0

        for node in sorted(nodes, key=lambda n: n.start_point):
            start_line = node.start_point[0]
            end_line = node.end_point[0] + 1
            result_lines.extend(lines[current_index:start_line])
            current_index = end_line

        result_lines.extend(lines[current_index:])
        return "\n".join(result_lines)

    def get_imports_from_tree(self, tree: tree_sitter.Tree) -> List[str]:
        return [
            stm.text.decode("utf8")
            for stm in self._find_node_by_type(tree.root_node, "import_statement")
        ]

    def _remove_unused_imports(self, content: str, unused_imports: set) -> str:
        return "\n".join(
            line
            for line in content.splitlines()
            if not any(ui in line for ui in unused_imports)
        )

    def _find_node_by_type(
        self, root_node: tree_sitter.Node, node_type: str
    ) -> List[tree_sitter.Node]:
        nodes = []

        def traverse(node):
            if node.type == node_type:
                nodes.append(node)
            for child in node.children:
                traverse(child)

        traverse(root_node)
        return nodes

    def _find_node_by_name(
        self, root_node: tree_sitter.Node, names: List[str], node_type: str
    ) -> List[tree_sitter.Node]:
        nodes = []

        def traverse(node):
            if node.type == node_type:
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text.decode("utf8") in names:
                    nodes.append(node)
            for child in node.children:
                traverse(child)

        traverse(root_node)
        return nodes

    def _remove_comments(self, byte_content: bytes, root_node: tree_sitter.Node) -> str:
        non_comment_parts = []

        for node in root_node.children:
            if node.type not in ("comment", "line_comment", "block_comment"):
                non_comment_parts.append(
                    byte_content[node.start_byte : node.end_byte].decode("utf8")
                )

        return "".join(non_comment_parts)

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

            # Traverse child nodes
            for child in node.children:
                result = traverse(child)
                if result:
                    return result

        traverse(root_node)
        return nodes_to_return
