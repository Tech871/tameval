import os
import json
import tenacity
import litellm, httpx
from rich.console import Console
from typing import List, Dict
import asyncio
from tqdm.asyncio import tqdm_asyncio

from openai import AsyncOpenAI

# allow no certificate verification
litellm.client_session = httpx.Client(verify=False)

console = Console(markup=False)

RANDOM_STATE = 73


class LitellmCaller:
    def __init__(
        self,
        model: str,
        base_url: str = "https://some-proxy/public/v1",
        api_key: str = "dummy_key",
        custom_llm_provider: str = "openai",
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.custom_llm_provider = custom_llm_provider

    def call_model(
        self,
        messages: List[Dict],
        max_completion_tokens: int = 7000,
        temperature: float = 0.25,
        stream: bool = True,
        n: int = 1,
        **completion_params,
    ) -> Dict[str, str]:
        console.log("LLM call...\n")

        response = litellm.completion(
            model=self.model,
            messages=messages,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            stream=stream,
            custom_llm_provider=self.custom_llm_provider,
            api_key=self.api_key,
            base_url=self.base_url,
            **completion_params,
        )

        if not stream:
            assert n == 1, NotImplementedError(
                "Multi choice generation (n>1) w/o steaming"
            )
            content = response.choices[0].message.content
            usage = response.usage
            prompt_tokens = int(usage.prompt_tokens)
            completion_tokens = int(usage.completion_tokens)
            console.log(f"Prompt tokens used: {prompt_tokens}")
            console.log(f"Completion tokens used: {completion_tokens}")
            return {
                "content": content,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        chunks = []
        chunks_per_completion = {i: [] for i in range(n)}
        with console.screen():
            console.log(messages[-1]["content"])
            with open("request.log", "w") as f:
                f.write(messages[-1]["content"])
            console.log("Streaming results from LLM model...\n")  # Initial output
            for i, chunk in enumerate(response):
                for choice in chunk.choices:
                    idx = choice.index
                    chunks_per_completion[idx].append(chunk)
                    if idx == n - 1:
                        content = choice.delta.get("content", "")
                        print(content or "", end="", flush=True)

        # Build the final response from the streamed chunks
        response_contents = []
        prompt_tokens = 0
        completion_tokens = 0
        for i, chunks in enumerate(chunks_per_completion.values()):
            model_response = litellm.stream_chunk_builder(chunks, messages=messages)
            prompt_tokens += int(model_response["usage"]["prompt_tokens"])
            completion_tokens += int(model_response["usage"]["completion_tokens"])
            response_contents.append(model_response["choices"][0]["message"]["content"])

        console.log(f"Prompt tokens used: {prompt_tokens}")
        console.log(f"Completion tokens used: {completion_tokens}")
        return {
            "content": (
                response_contents[0]
                if len(response_contents) == 1
                else response_contents
            ),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }

    def call_batch(
        self,
        message_batch: List[List[Dict]],
        max_active_requests: int = 50,
        max_completion_tokens: int = 7000,
        temperature: float = 0.25,
        n: int = 1,
        retry: int = 3,
        **completion_params,
    ):
        return asyncio.run(
            self._call_batch(
                message_batch,
                max_active_requests=max_active_requests,
                max_completion_tokens=max_completion_tokens,
                temperature=temperature,
                retry=retry,
            )
        )

    async def _call_batch(
        self,
        message_batch: List[List[Dict]],
        max_active_requests: int = 50,
        max_completion_tokens: int = 5000,
        temperature: float = 0.25,
        n: int = 1,
        retry: int = 2,
        **completion_params,
    ):
        http_client = httpx.AsyncClient(verify=False)

        client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            http_client=http_client,
        )

        semaphore = asyncio.Semaphore(min(len(message_batch), max_active_requests))

        def acompletion_w_retry(msg):
            return client.chat.completions.create(
                model=self.model,
                messages=msg,
                max_completion_tokens=max_completion_tokens,
                temperature=temperature,
                seed=RANDOM_STATE,
                timeout=240,
            )

        def log_before_sleep(retry_state):
            print(
                f"Retry ({retry_state.attempt_number}) after exception: {retry_state.outcome.exception()}"
            )

        @tenacity.retry(
            stop=tenacity.stop_after_attempt(retry),
            wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
            before_sleep=log_before_sleep,
        )
        async def bounded_process(data):
            async with semaphore:
                try:
                    response = await acompletion_w_retry(data["msg"])
                    # content = {
                    #     "content": response.choices[0].message.content,
                    #     "prompt_tokens": response.usage.prompt_tokens,
                    #     "completion_tokens": response.usage.completion_tokens,
                    # }
                    content = response.choices[0].message.content
                    if not content:
                        raise Exception("Empty response content")

                    if not "result_save_path" in data:
                        return content

                    with open(data["result_save_path"], "r") as f:
                        result_sample = json.load(f)
                        result_sample["responses"] = result_sample.get(
                            "responses", []
                        ) + [content]

                    with open(data["result_save_path"], "w") as f:
                        json.dump(result_sample, f, indent=4)

                    return content
                except asyncio.TimeoutError:
                    print(f"Batch gen sample timeout!")
                    raise e
                except Exception as e:
                    print(f"Batch gen sample error: {e}")
                    raise e

        tasks = [bounded_process(msg) for msg in message_batch]
        responses = await tqdm_asyncio.gather(*tasks, desc=f"Calling {self.model}")
        await http_client.aclose()
        return responses


if __name__ == "__main__":
    client = LitellmCaller(
        model="deepseek/deepseek-chat",
        base_url="https://openrouter.ai/api/v1",
        api_key="<KEY>",
    )

    message_batch = [
        {"msg": [{"role": "user", "content": "prompt1: hello hello"}]},
        {"msg": [{"role": "user", "content": "prompt2: hello world"}]},
    ]
    answers = client.call_batch(message_batch, max_completion_tokens=100)
    print(answers)
