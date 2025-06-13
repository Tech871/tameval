import re
import json
from jinja2 import Template
from typing import Literal


from utils.ai_caller import LitellmCaller


class LLMTestEnhancer:
    SEARCH_REPLACE_REGEX = re.compile(
        r"<<{5,}\s*SEARCH\s*\n(?P<search>.*?)(?:\n)?={5,}\s*\n(?P<replace>.*?)(?:\n)?>{5,}\s*REPLACE",
        re.DOTALL,
    )

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str = "dummy_key",
        edit_format: Literal["edit", "whole"] = "whole",
    ):
        self.model_name = model_name
        self.ai_caller = LitellmCaller(
            model=model_name, base_url=base_url, api_key=api_key
        )
        self.edit_format = edit_format
        if edit_format == "whole":
            self.prompt_template_path = "generate/prompts/whole_prompt.j2"
        elif edit_format == "search_replace":
            self.prompt_template_path = "generate/prompts/search_replace_prompt.j2"
        else:
            raise NotImplementedError(f"Unknown edit format: {edit_format}")

        with open(self.prompt_template_path, "r") as f:
            self.prompt_template = Template(f.read())

    def generate(
        self, source_file_path: str, test_file_path: str, language: str, repo_root: str
    ):
        message = self.get_input_message(
            source_file_path=source_file_path,
            test_file_path=test_file_path,
            language=language,
            repo_root=repo_root,
        )
        print(message[-1]["content"])
        response = self.ai_caller.call_model(messages=message, stream=True)

        return response["content"]

    def generate_batch(
        self,
        samples: dict,
        qwen3_disable_thinking: bool = True,
        max_new_test_cases_per_request: int = 5,
    ):
        messages = []
        for sample in samples:
            request_msg = self.get_input_message(
                source_file_path=sample["source_file_path"],
                test_file_path=sample["test_file_path"],
                language=sample["language"],
                repo_root=sample["repo_root"],
                fail_feedback=sample["fail_feedback"],
                qwen3_disable_thinking=qwen3_disable_thinking,
                max_new_test_cases_per_request=max_new_test_cases_per_request,
            )
            with open(sample["result_save_path"], "r") as f:
                result_sample = json.load(f)
                result_sample["prompts"] = result_sample.get("prompts", []) + [
                    request_msg[-1]["content"]
                ]

            with open(sample["result_save_path"], "w") as f:
                json.dump(result_sample, f, indent=4)

            data = {"msg": request_msg, "result_save_path": sample["result_save_path"]}
            messages.append(data)

        return self.ai_caller.call_batch(messages)

    def get_input_message(
        self,
        source_file_path: str,
        test_file_path: str,
        language: str,
        repo_root: str,
        fail_feedback: str = None,
        qwen3_disable_thinking: bool = True,
        max_new_test_cases_per_request: int = 5,
    ):
        with open(repo_root / source_file_path, "r") as f:
            source_file = f.read()
        with open(repo_root / test_file_path, "r") as f:
            test_file = f.read()

        if self.edit_format in ["edit", "udiff"]:
            lines = test_file.splitlines()
            num_indent = len(str(len(lines)))
            test_file = "\n".join(
                [f"{i + 1:>{num_indent}}| {line}" for i, line in enumerate(lines)]
            )

        prompt = self.prompt_template.render(
            {
                "language": language,
                "source_file": source_file,
                "test_file": test_file,
                "source_file_name": source_file_path,
                "test_file_name": test_file_path,
                "fail_feedback": fail_feedback,
                "max_new_test_cases": max_new_test_cases_per_request,
            }
        )
        if qwen3_disable_thinking and "qwen3" in self.model_name:
            prompt = "/no_think\n" + prompt

        return [{"role": "user", "content": prompt}]

    def apply_changes(
        self,
        test_file_path: str,
        changes: str,
        result_save_path: str = None,
        inplace: bool = True,
        output_path: str = None,
    ):
        if inplace:
            output_path = test_file_path

        assert output_path is not None, ValueError("Output path is not specified")

        if self.edit_format == "whole":
            changed_test_file = self._extract_whole_response(changes)
            with open(output_path, "w") as f:
                f.write(changed_test_file)

        elif self.edit_format == "search_replace":
            with open(test_file_path, "r") as f:
                test_file = f.read()
            changed_test_file = self.apply_search_replace_changes(test_file, changes)
            with open(output_path, "w") as f:
                f.write(changed_test_file)

        else:
            raise NotImplementedError(f"Unknown edit format: {self.edit_format}")

        if result_save_path is None:
            return

        with open(result_save_path, "r") as f:
            result = json.load(f)
            result["test_content_to_eval"] = changed_test_file
            result["pipeline_status"] = "generated"

        with open(result_save_path, "w") as f:
            json.dump(result, f, indent=4)
        return

    def _extract_whole_response(self, response: str):
        if len(response) < 5:
            raise ValueError(f"Response is empty: '{response}'")
        code_block_pattern = r"```(?:\w*\n)?(.*?)```"
        response_text = response.strip().strip("\n")
        if not response_text.startswith("```"):
            response_text = f"```{response_text}"
        if not response_text.endswith("```"):
            response_text = f"{response_text}\n```"
        match = re.search(code_block_pattern, response_text, re.DOTALL)

        if not match:
            print("No code block found. Returning as-is.")
            return response_text
        matched_text = match.group(1)
        return matched_text.strip("`")

    def apply_search_replace_changes(self, file_content: str, changes: str):
        sr_blocks = self.extract_search_replace_blocks(changes)
        for i, block in enumerate(sr_blocks):
            if block["search"] == "" or block["search"] == " ":
                file_content = file_content + "\n\n" + block["replace"]
            else:
                file_content = file_content.replace(block["search"], block["replace"])
        return file_content

    def extract_search_replace_blocks(self, text: str):

        blocks = []
        for match in self.SEARCH_REPLACE_REGEX.finditer(text):
            search_block = match.group(1).strip()
            replace_block = match.group(2).strip()
            blocks.append({"search": search_block, "replace": replace_block})

        return blocks
