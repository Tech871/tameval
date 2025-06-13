import textwrap
import traceback
import tree_sitter
from tree_sitter import Language, Parser
from tree_sitter_languages import get_parser
import tree_sitter_java

from typing import Literal, List, Dict

import warnings

warnings.simplefilter("ignore")

# JAVA_LANGUAGE = Language(tree_sitter_java.language())
# ts_parser = Parser(JAVA_LANGUAGE)
ts_parser = get_parser("java")


class ParsedFile:
    def __init__(self, content: str):
        self.content = content
        self.bytes_content = bytes(content, "utf8")
        self.tree = ts_parser.parse(self.bytes_content)

    def get_file_struct(self) -> Dict:
        """Main function to parse the sample content and return structured information."""
        file_content_no_comments = self._remove_comments(
            self.bytes_content, self.tree.root_node
        )
        num_of_lines = self.content.count("\n")
        num_of_code_lines = file_content_no_comments.count("\n")
        if num_of_code_lines < 3:
            return {
                "num_of_lines": num_of_lines,
                "num_of_code_lines": num_of_code_lines,
                "classes": [],
            }

        funcs = self.get_functions()
        self.file_struct = {
            "num_of_lines": num_of_lines,
            "num_of_code_lines": num_of_code_lines,
            "functions": [func.text.decode("utf8") for func in funcs],
        }
        return self.file_struct

    def get_test_functions(self):
        funcs = self.get_functions()
        test_funcs = []
        for func in funcs:
            if "@Test" in func.text.decode("utf8"):
                test_funcs.append(func)
        return test_funcs

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

    def remove_method_by_name(
        self, class_name: str, method_name: str | List[str]
    ) -> str:
        """Remove the method with the given name from the code."""
        root_node = self.tree.root_node
        class_node = self._find_node_by_type_and_name(
            root_node, class_name, node_type="class_declaration"
        )[0]

        if isinstance(method_name, str):
            method_name = [method_name]

        # Find the method node
        method_nodes = self._find_node_by_type_and_name(
            class_node, method_name, node_type="method_declaration"
        )
        if not method_nodes:
            raise Exception(f"Method '{method_name}' not found.")

        result = []
        current_index = 0
        file_lines = self.content.splitlines()
        for node in method_nodes:

            start_line = node.start_point[0]
            end_line = node.end_point[0] + 1
            result.extend(file_lines[current_index:start_line])
            current_index = end_line

        result.extend(file_lines[current_index:])
        return "\n".join(result)

    def add_method_to_class(self, class_name: str, method_content: str) -> str:
        root_node = self.tree.root_node
        file_code = root_node.text.decode()
        class_node = self._find_node_by_type_and_name(
            root_node, class_name, node_type="class_declaration"
        )[0]
        # Locate the end of the class declaration
        end_byte = class_node.end_byte - 1  # Before the closing brace

        # Insert the new method before the last closing brace
        modified_code = (
            file_code[:end_byte] + "\n" + method_content + "\n" + file_code[end_byte:]
        )
        return modified_code

    def find_method_nodes_by_row_numbers(self, row_numbers: List[int]) -> List[Dict]:
        # TODO: переписать на работу со списком отрезков
        target_nodes = []

        def traverse(node):
            if node.type == "method_declaration":
                # Check if method name matches the target
                for row_num in row_numbers:
                    if (
                        row_num >= node.start_point.row
                        and row_num <= node.end_point.row
                    ):
                        parent_class_node = self._get_parent_class_node(node)
                        target_nodes.append(
                            dict(
                                method_node=node,
                                method_name=node.child_by_field_name(
                                    "name"
                                ).text.decode(),
                                class_node=parent_class_node,
                                class_name=parent_class_node.child_by_field_name(
                                    "name"
                                ).text.decode(),
                            )
                        )
                        break
            # Traverse child nodes
            for child in node.children:
                result = traverse(child)
                if result:
                    return result
            return None

        traverse(self.tree.root_node)
        return target_nodes

    @staticmethod
    def _get_parent_class_node(method_node: tree_sitter.Node) -> tree_sitter.Node:
        """Returns the parent class node of a given method node."""
        current_node = method_node
        while current_node:
            if current_node.type == "class_declaration":
                return current_node
            current_node = current_node.parent
        return None

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
            if node.type not in ("line_comment", "block_comment"):
                non_comment_parts.append(
                    byte_content[node.start_byte : node.end_byte].decode()
                )

        return "".join(non_comment_parts)

    def _get_class_info(self) -> List[Dict]:
        """Extract class-level information, including class name and modifiers."""
        root_node = self.tree.root_node
        class_structs = []
        for child_node in root_node.children:
            if child_node.type == "class_declaration":
                class_struct = {
                    "name": child_node.child_by_field_name("name").text.decode(),
                    "class_content": child_node.text.decode(),
                    "modifiers": self._get_modifiers(child_node),
                    "class_body_node": child_node.child_by_field_name("body"),
                }
                class_structs.append(class_struct)
        return class_structs

    def _get_methods_info(self, class_body_node: tree_sitter.Node):
        """Extract method-level information, including method name, modifiers, and parameters."""
        methods = []
        for child_node in class_body_node.children:
            if child_node.type != "method_declaration":
                continue
            method_body_node = child_node.child_by_field_name("body")
            method_info = {
                "name": child_node.child_by_field_name("name").text.decode(),
                "content": child_node.text.decode(),
                "start_point": tuple(child_node.start_point),
                "end_point": tuple(child_node.end_point),
                "start_byte": child_node.start_byte,
                "end_byte": child_node.end_byte,
                "modifiers": self._get_modifiers(child_node),
                "params": self._get_method_parameters(child_node),
            }
            method_info["num_of_chars"] = len(method_body_node.text.decode())
            method_info["num_of_decorators"] = method_info["content"].count("@")
            method_info["num_of_lines"] = method_body_node.text.decode().count("\n")
            methods.append(method_info)
        return methods

    def _get_fields_info(self, class_body_node: tree_sitter.Node):
        """Extract field-level information, including field name and type."""
        fields = []
        for child_node in class_body_node.children:
            if child_node.type != "field_declaration":
                continue
            field_name_node = child_node.children_by_field_name("declarator")[0]
            field_type_node = child_node.children_by_field_name("type")[0]

            field_info = {
                "name": field_name_node.text.decode(),
                "type": field_type_node.text.decode(),
            }
            fields.append(field_info)
        return fields

    def _get_modifiers(self, node: tree_sitter.Node):
        """Extract modifiers (e.g., public, private, static) from a node."""
        _map = {
            "private": "access_modifier",
            "public": "access_modifier",
            "static": "access_modifier",
        }
        output = {
            _map.get(grand_child.type, grand_child.type): grand_child.text.decode()
            for child in node.named_children
            if child.type == "modifiers"
            for grand_child in child.children
        }
        if "access_modifier" not in output:
            output["access_modifier"] = "public"
        for child in node.named_children:
            if child.type == "modifiers":
                output["content"] = child.text.decode()
        return output

    def _get_method_parameters(self, method_body_node):
        """Extract parameters for a method."""
        params = [
            {k.type: k.text.decode() for k in param.children}
            for params_node in method_body_node.children_by_field_name("parameters")
            for param in params_node.children
        ]
        return params
