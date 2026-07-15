
import os
import json
import click
import asyncio
import aiohttp
import logging
import re

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers = []

class RateLimitError(Exception):
    def __init__(self, message):
        super().__init__(message)

class ContentFormatError(Exception):
    def __init__(self, message):
        super().__init__(message)

def extract_model_json_text(raw_content, markers=("RESULT #:", "STRICT JSON FORMAT #:")):
    content = str(raw_content or "").replace("\n", "").replace("\\_", "_").strip()
    for marker in markers:
        start_pos = content.find(marker)
        if start_pos != -1:
            content = content[start_pos + len(marker):]
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end < start:
        return content
    return content[start:end + 1]

def repair_invalid_json_escapes(json_text):
    # JSON allows escapes like \" and \\ but not \'. Some models emit English
    # apostrophes as \' inside JSON strings; remove only invalid escape slashes.
    return re.sub(r'\\([^"\\/bfnrtu])', r'\1', json_text)

def loads_model_json_content(raw_content, markers=("RESULT #:", "STRICT JSON FORMAT #:")):
    json_text = extract_model_json_text(raw_content, markers=markers)
    try:
        content = json.loads(json_text)
    except json.JSONDecodeError:
        content = json.loads(repair_invalid_json_escapes(json_text))
    if isinstance(content, list) and len(content):
        merge_content = {}
        for item in content:
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                merge_content[key].extend(value) if key in merge_content else merge_content.update({key: value})
        return merge_content
    return content

def load_demo_field(data, primary_key, fallback_key=None):
    value = data.get(primary_key)
    if value is None and fallback_key:
        value = data.get(fallback_key)
    if value is None:
        keys = primary_key if not fallback_key else f"{primary_key}/{fallback_key}"
        raise KeyError(f"Demo {data.get('id')} is missing {keys}")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value

def apply_non_streaming_model_options(payload):
    model = str(payload.get("model") or "").strip().lower()
    if model.startswith("qwen3") and payload.get("stream") is False:
        payload["enable_thinking"] = False
    else:
        payload.pop("enable_thinking", None)
    return payload


@click.command()
@click.option("--data_dir", default="data_huggingface", help="The directory of the data.")
@click.option("--temperature", type=float, default=0.2)
@click.option("--top_p", type=float, default=0.1)
@click.option("--api_addr", type=str, default="localhost")
@click.option("--api_port", type=int, default=4000)
@click.option("--base_url", type=str, default=None, help="OpenAI-compatible API base URL. Overrides api_addr/api_port when set.")
@click.option("--api_key", type=str, default="your api key")
@click.option("--multiworker", type=int, default=1)
@click.option("--llm", type=str, default="gpt-4")
@click.option("--use_demos", type=int, default=2)
@click.option("--reformat", type=bool, default=False)
@click.option("--reformat_by", type=str, default="self")
@click.option("--tag", type=bool, default=False)
@click.option("--dependency_type", type=str, default="resource")
@click.option("--log_first_detail", type=bool, default=False)
@click.option("--max_tokens", type=int, default=2000, help="Maximum completion tokens requested from the model.")
def main(data_dir, temperature, top_p, api_addr, api_key, api_port, base_url, multiworker, llm, use_demos, reformat, reformat_by, tag, dependency_type, log_first_detail, max_tokens):
    assert dependency_type in ["resource", "temporal"], "Dependency type not supported"
    if dependency_type == "resource":
        assert data_dir != "data_dailylifeapis", "Resource dependency type only support data_huggingface and data_multimedia"

    arguments = locals()
    if base_url:
        base_url = base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            url = base_url
        else:
            url = f"{base_url}/chat/completions"
    else:
        url = f"http://{api_addr}:{api_port}/v1/chat/completions"
    header = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prediction_dir = f"{data_dir}/predictions{f'_use_demos_{use_demos}' if use_demos and tag else ''}{f'_reformat_by_{ reformat_by}' if reformat and tag else ''}"
    wf_name = f"{prediction_dir}/{llm}.json"
    
    if not os.path.exists(prediction_dir):
        os.makedirs(prediction_dir, exist_ok=True)

    has_inferenced = []
    if os.path.exists(wf_name):
        rf = open(wf_name, "r", encoding="utf-8-sig")
        for line in rf:
            data = json.loads(line)
            has_inferenced.append(data["id"])
        rf.close()

    rf_ur = open(f"{data_dir}/user_requests.json", "r", encoding="utf-8-sig")
    inputs = []
    for line in rf_ur:
        input = json.loads(line)
        if input["id"] not in has_inferenced:
            inputs.append(input)
    rf_ur.close()

    wf = open(wf_name, "a", encoding="utf-8")
    
    tool_list = json.load(open(f"{data_dir}/tool_desc.json", "r", encoding="utf-8-sig"))["nodes"]
    if "input-type" not in tool_list[0]:
        assert dependency_type == "temporal", "Tool type is not ignored, but the tool list does not contain input-type and output-type"
    if dependency_type == "temporal":
        for tool in tool_list:
            parameter_list = []
            for parameter in tool["parameters"]:
                parameter_list.append(parameter["name"])
            tool["parameters"] = parameter_list

    # log llm name in format
    formatter = logging.Formatter(f"%(asctime)s - [ {llm} ] - %(levelname)s - %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(f"{prediction_dir}/{llm}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    # logging all args
    logger.info(f"Arguments: {arguments}")

    demos = []
    if use_demos:
        if dependency_type == "temporal":
            demos_id = [ "38563456", "27267145", "91005535"]
        else:
            if "huggingface" in data_dir: 
                demos_id = [ "10523150", "14611002", "22067492"]
            elif "multimedia" in data_dir:
                demos_id = [ "30934207", "20566230", "19003517"]
        demos_id = demos_id[:use_demos]
        logger.info(f"Use {len(demos_id)} demos: {demos_id}")
        demos_rf = open(f"{data_dir}/data.json", "r", encoding="utf-8-sig")
        for line in demos_rf:
            data = json.loads(line)
            if data["id"] in demos_id:
                user_request = load_demo_field(data, "user_request", "instruction")
                task_steps = load_demo_field(data, "task_steps", "tool_steps")
                task_nodes = load_demo_field(data, "task_nodes", "tool_nodes")
                if dependency_type == "temporal":
                    task_links = load_demo_field(data, "task_links", "tool_links")
                    demo = {
                        "user_request": user_request,
                        "result":{
                            "task_steps": task_steps,
                            "task_nodes": task_nodes,
                            "task_links": task_links
                        }
                    }
                else:
                    demo = {
                        "user_request": user_request,
                        "result":{
                            "task_steps": task_steps,
                            "task_nodes": task_nodes
                        }
                    }
                demos.append(demo)
        demos_rf.close()

    tool_string = "# TASK LIST #:\n"
    for k, tool in enumerate(tool_list):
        tool_string += json.dumps(tool) + "\n"
    
    sem = asyncio.Semaphore(multiworker)

    async def inference_wrapper(input, url, header, temperature, top_p, tool_string, wf, llm, demos, reformat, reformat_by, dependency_type, max_tokens, log_detail = False):
        async with sem:
            await inference(input, url, header, temperature, top_p, tool_string, wf, llm, demos, reformat, reformat_by, dependency_type, max_tokens, log_detail)

    if len(inputs) == 0:
        logger.info("All Completed!")
        return
    else:
        logger.info(f"Detected {len(has_inferenced)} has been inferenced,")
        logger.info(f"Start inferencing {len(inputs)} tasks...")
    
    loop = asyncio.get_event_loop()

    if log_first_detail:
        tasks = [inference_wrapper(inputs[0], url, header, temperature, top_p, tool_string, wf, llm, demos, reformat, reformat_by, dependency_type, max_tokens, log_detail=True)]
        results = loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        inputs = inputs[1:]

    tasks = []
    for input in inputs:
        tasks.append(inference_wrapper(input, url, header, temperature, top_p, tool_string, wf, llm, demos, reformat, reformat_by, dependency_type, max_tokens))

    results += loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    failed = []
    done = []
    for result in results:
        if isinstance(result, Exception):
            failed.append(result)
        else:
            done.append(result)
    logger.info(f"Completed: {len(done)}")
    logger.info(f"Failed: {len(failed)}")
    loop.close()

async def inference(input, url, header, temperature, top_p, tool_string, wf, llm, demos, reformat, reformat_by, dependency_type, max_tokens, log_detail = False):
    user_request = input["user_request"]
    if dependency_type == "resource":
        prompt = """\n# GOAL #: Based on the above tools, I want you generate task steps and task nodes to solve the # USER REQUEST #. The format must in a strict JSON format, like: {"task_steps": [ step description of one or more steps ], "task_nodes": [{"task": "tool name must be from # TOOL LIST #", "arguments": [ a concise list of arguments for the tool. Either original text, or user-mentioned filename, or tag '<node-j>' (start from 0) to refer to the output of the j-th node. ]}]} """
        prompt += """\n\n# REQUIREMENTS #: \n1. the generated task steps and task nodes can resolve the given user request # USER REQUEST # perfectly. Task name must be selected from # TASK LIST #; \n2. the task steps should strictly aligned with the task nodes, and the number of task steps should be same with the task nodes; \n3. the dependencies among task steps should align with the argument dependencies of the task nodes; \n4. the tool arguments should be align with the input-type field of # TASK LIST #;"""
    else:
        prompt = """\n# GOAL #:\nBased on the above tools, I want you generate task steps and task nodes to solve the # USER REQUEST #. The format must in a strict JSON format, like: {"task_steps": [ "concrete steps, format as Step x: Call xxx tool with xxx: 'xxx' and xxx: 'xxx'" ], "task_nodes": [{"task": "task name must be from # TASK LIST #", "arguments": [ {"name": "parameter name", "value": "parameter value, either user-specified text or the specific name of the tool whose result is required by this node"} ]}], "task_links": [{"source": "task name i", "target": "task name j"}]}"""
        prompt += """\n\n# REQUIREMENTS #: \n1. the generated task steps and task nodes can resolve the given user request # USER REQUEST # perfectly. Task name must be selected from # TASK LIST #; \n2. the task steps should strictly aligned with the task nodes, and the number of task steps should be same with the task nodes; \n3. The task links (task_links) should reflect the temporal dependencies among task nodes, i.e. the order in which the APIs are invoked;"""

    if len(demos) > 0:
        prompt += "\n"
        for demo in demos:
            prompt += f"""\n# EXAMPLE #:\n# USER REQUEST #: {demo["user_request"]}\n# RESULT #: {json.dumps(demo["result"])}"""
    
    prompt += """\n\n# USER REQUEST #: {{user_request}}\nnow please generate your result in a strict JSON format:\n# RESULT #:"""

    final_prompt = tool_string + prompt.replace("{{user_request}}", user_request)
    payload_dict = {
        "model": f"{llm}",
        "messages": [
            {
            "role": "user",
            "content":  final_prompt
            }
        ],
        "temperature": temperature,
        "top_p": top_p,
        "frequency_penalty": 0,
        "presence_penalty": 1.05,
        "max_tokens": max_tokens,
        "stream": False,
        "stop": None
    }
    payload = json.dumps(apply_non_streaming_model_options(payload_dict))
    try:
        result = await get_response(url, header, payload, input['id'], reformat, reformat_by, dependency_type, log_detail)
    except Exception as e:
        logger.info(f"Failed #id {input['id']}: {type(e)} {e}")
        raise e
    logger.info(f"Success #id {input['id']}")
    input["result"] = result
    wf.write(json.dumps(input) + "\n")
    wf.flush()

async def post_json_with_context_retry(url, header, payload, timeout, id, phase):
    current_payload = payload
    last_status = None
    last_resp = None
    for _attempt in range(8):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=header, data=current_payload, timeout=timeout) as response:
                last_status = response.status
                try:
                    last_resp = await response.json()
                except Exception:
                    last_resp = {"raw_response": await response.text()}
        if last_status == 400:
            reduced = reduce_max_tokens_for_context_error(current_payload, last_resp)
            if reduced is not None:
                current_payload, old_max_tokens, new_max_tokens = reduced
                logger.info(
                    f"[context_retry] #id {id} {phase}: max_tokens {old_max_tokens} -> {new_max_tokens}"
                )
                continue
        return last_status, last_resp, current_payload
    return last_status, last_resp, current_payload


def reduce_max_tokens_for_context_error(payload, resp):
    message = str(resp.get("error", {}).get("message", "") if isinstance(resp, dict) else "")
    if "maximum context length" not in message or "input tokens" not in message:
        return None

    limit_match = re.search(r"maximum context length is\s+(\d+)\s+tokens", message)
    input_match = re.search(r"prompt contains at least\s+(\d+)\s+input tokens", message)
    if input_match is None:
        input_match = re.search(r"value=(\d+)", message)
    if limit_match is None or input_match is None:
        return None

    try:
        payload_dict = json.loads(payload)
        old_max_tokens = int(payload_dict.get("max_tokens") or 0)
        context_limit = int(limit_match.group(1))
        input_tokens = int(input_match.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    available_tokens = context_limit - input_tokens - 96
    if available_tokens < 128:
        return None
    new_max_tokens = available_tokens
    if new_max_tokens <= 0 or new_max_tokens >= old_max_tokens:
        return None
    payload_dict["max_tokens"] = new_max_tokens
    return json.dumps(payload_dict), old_max_tokens, new_max_tokens


async def get_response(url, header, payload, id, reformat, reformat_by, dependency_type, log_detail=False):
    status, resp, payload = await post_json_with_context_retry(
        url=url,
        header=header,
        payload=payload,
        timeout=300,
        id=id,
        phase="initial",
    )

    if status == 429:
        raise RateLimitError(f"{resp}")
    if status != 200:
        raise Exception(f"{resp}")
    
    if log_detail:
        logger.info(json.loads(payload)["messages"][0]["content"])
        logger.info(resp["choices"][0]["message"]["content"])

    oring_content = resp["choices"][0]["message"]["content"]
    content = extract_model_json_text(oring_content, markers=("RESULT #:",))
    try:
        return loads_model_json_content(oring_content, markers=("RESULT #:",))
    except json.JSONDecodeError as e:
        if reformat:
            if dependency_type == "resource":
                prompt = """Please format the result # RESULT # to a strict JSON format # STRICT JSON FORMAT #. \nRequirements:\n1. Do not change the meaning of task steps and task nodes;\n2. Don't tolerate any possible irregular formatting to ensure that the generated content can be converted by json.loads();\n3. You must output the result in this schema: {"task_steps": [ step description of one or more steps ], "task_nodes": [{"task": "tool name must be from # TOOL LIST #", "arguments": [ a concise list of arguments for the tool. Either original text, or user-mentioned filename, or tag '<node-j>' (start from 0) to refer to the output of the j-th node. ]}]}\n# RESULT #:{{illegal_result}}\n# STRICT JSON FORMAT #:"""
            else:
                prompt = """Please format the result # RESULT # to a strict JSON format # STRICT JSON FORMAT #. \nRequirements:\n1. Do not change the meaning of task steps, task nodes and task links;\n2. Don't tolerate any possible irregular formatting to ensure that the generated content can be converted by json.loads();\n3. Pay attention to the matching of brackets. Write in a compact format and avoid using too many space formatting controls;\n4. You must output the result in this schema: {"task_steps": [ "concrete steps, format as Step x: Call xxx tool with xxx: 'xxx' and xxx: 'xxx'" ], "task_nodes": [{"task": "task name must be from # TASK LIST #", "arguments": [ {"name": "parameter name", "value": "parameter value, either user-specified text or the specific name of the tool whose result is required by this node"} ]}], "task_links": [{"source": "task name i", "target": "task name j"}]}\n# RESULT #:{{illegal_result}}\n# STRICT JSON FORMAT #:"""
            prompt = prompt.replace("{{illegal_result}}", oring_content)
            payload = json.loads(payload)
            if reformat_by != "self":
                payload["model"] = reformat_by

            if log_detail:
                logger.info(f"[warning] #id {id} Illegal JSON format: {content}")
                logger.info(f"[reformat] #id {id} Detected illegal JSON format, try to reformat by {payload['model']}...")

            payload["messages"][0]["content"] = prompt
            payload = json.dumps(apply_non_streaming_model_options(payload))
            
            status, resp, payload = await post_json_with_context_retry(
                url=url,
                header=header,
                payload=payload,
                timeout=120,
                id=id,
                phase="reformat",
            )

            if status == 429:
                raise RateLimitError(f"{resp}")
            if status != 200:
                raise Exception(f"{resp}")
            
            if log_detail:
                logger.info(json.loads(payload)["messages"][0]["content"])
                logger.info(resp["choices"][0]["message"]["content"])

            content = resp["choices"][0]["message"]["content"]
            try:
                return loads_model_json_content(content, markers=("STRICT JSON FORMAT #:",))
            except json.JSONDecodeError as e:
                raise ContentFormatError(f"{extract_model_json_text(content, markers=('STRICT JSON FORMAT #:',))}")
        else:
            raise ContentFormatError(f"{content}")

if __name__ == "__main__":
    main()
