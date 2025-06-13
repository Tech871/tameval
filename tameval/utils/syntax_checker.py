from tree_sitter_languages import get_parser
from grep_ast import TreeContext, filename_to_lang

from typing import Literal, Dict, List

import warnings

warnings.simplefilter("ignore")


class SyntaxChecker:

    def __init__(
        self, lang: Literal["java", "python", "go", "kotlin", "swift"] = "java"
    ):
        self.ts_parser = get_parser(lang)

    def check(self, code: str) -> Dict[str, str]:

        tree = self.ts_parser.parse(bytes(code, "utf8"))
        if not tree.root_node.has_error:
            return {"correct": True, "errors": [], "error_lines": []}

        def traverse_tree_iterative(root_node, stack_limit=10000):
            errors = []
            stack = [root_node]

            itr = 0
            while stack:
                if itr > stack_limit:
                    errors.append(
                        {
                            "error": "Maximum recursion depth exceeded when check syntax",
                            "range": ((0, 0), tuple(node.end_point)),
                        }
                    )
                    break

                node = stack.pop()

                if node.is_error:
                    errors.append(
                        {
                            "error": f"{node.text.decode(errors='replace')}",
                            "range": (tuple(node.start_point), tuple(node.end_point)),
                        }
                    )

                if node.is_missing:
                    errors.append(
                        {
                            "error": f"Missing: {node.type}",
                            "range": (tuple(node.start_point), tuple(node.end_point)),
                        }
                    )
                stack.extend(reversed(node.children))
                itr += 1

            return errors

        errors = traverse_tree_iterative(tree.root_node)
        return {
            "correct": False,
            "errors": errors,
            "error_lines": self._get_error_lines(errors),
        }

    def get_error_message(
        self,
        code: str,
        filename: str,
        errors: Dict,
        add_error_context: bool = True,
        limit_message_n_chars: int = 1500,
    ) -> str:
        error_message = "## See syntax errors below:\n\n"
        for error in errors["errors"]:
            start_p = error["range"][0]
            end_p = error["range"][1]
            error_message += f"\nError in line {start_p[0]+1} byte {start_p[1]} till line {end_p[0]+1} byte {end_p[1]}!"
            error_message += f" Details:\n{error['error'][:150]}..."
        if add_error_context:
            try:
                error_message += "\n\n" + self.get_error_context(
                    code, filename, errors["error_lines"]
                )
            except Exception as e:
                print(e)
        if len(error_message) > limit_message_n_chars:
            return error_message[:limit_message_n_chars] + "..."
        return error_message

    def get_error_context(
        self, code: str, filename: str, error_line_numbers: List[int]
    ) -> TreeContext:
        context = TreeContext(
            filename,
            code,
            color=False,
            line_number=True,
            child_context=False,
            last_line=False,
            margin=0,
            mark_lois=True,
            loi_pad=3,
            show_top_of_file_parent_scope=False,
        )
        line_nums = set(error_line_numbers)
        context.add_lines_of_interest(error_line_numbers)
        context.add_context()
        s = "s" if len(line_nums) > 1 else ""
        output = f"## See relevant line{s} below marked with █.\n\n"
        output += filename + ":\n"
        output += context.format()[-500:]
        return output

    @staticmethod
    def _get_error_lines(errors: List) -> List[int]:
        error_lines = []
        for error in errors:
            start_point, end_point = error["range"]
            if start_point[0] == end_point[0]:
                error_lines.append(start_point[0])
                continue
            error_lines += list(range(start_point[0], end_point[0] + 1))
        return error_lines
