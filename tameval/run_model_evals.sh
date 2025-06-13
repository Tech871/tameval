models=("qwen/qwen-2.5-coder-32b-instruct" "google/gemma-3-27b-it"  "mistralai/devstral-small" "qwen/qwen3-235b-a22b" "openai/o4-mini-high" "deepseek/deepseek-r1")

for model in "${models[@]}"
do
    time python run.py \
    --benchmark-version="v1" \
    --model-to-eval=$model \
    --edit-format="whole" \
    --llm-api-base-url="https://openrouter.ai/api/v1" \
    --batch-generation=1 \
    --repo-folder=download/repo \
    --work-folder=tmp \
    --env-file=".env" \
    --clearml-task-name="attempt@3" \
    --exp-tag="final" \
    --only-refresh-files=1 \
    --max-attempts-num=3 \
    --qwen3-disable-thinking=1
done