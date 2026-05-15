import traceback

from datetime import datetime
import numpy as np
from scipy.optimize import linear_sum_assignment
import json
import click
try:
    from datasets import load_metric
except Exception:
    import importlib
    import sys
    from pathlib import Path

    def load_metric(metric_name):
        # Avoid circular import with local file name `evaluate.py`.
        this_dir = str(Path(__file__).resolve().parent)
        removed: list[str] = []
        for p in ("", this_dir):
            while p in sys.path:
                sys.path.remove(p)
                removed.append(p)
        try:
            hf_evaluate = importlib.import_module("evaluate")
            return hf_evaluate.load(metric_name)
        finally:
            for p in reversed(removed):
                sys.path.insert(0, p)
import Levenshtein
from sklearn.metrics import precision_recall_fscore_support as prfs
import warnings
import logging
import os
import itertools
import re

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers = []

timestamp = datetime.now().strftime("%Y%m%d")

def sim(name_1, name_2):
    if name_1 == "<PAD>" or name_2 == "<PAD>":
        return 0
    return 1 if name_1 == name_2 else 0

def create_cost_matrix(graph_1, graph_2):
    nodes_1 = graph_1["nodes"]
    nodes_2 = graph_2["nodes"]

    num_nodes_1 = len(nodes_1)
    num_nodes_2 = len(nodes_2)

    nodes_similarity_matrix = np.zeros((num_nodes_1, num_nodes_2))
    
    for i, node_1 in enumerate(graph_1["nodes"]):
        for j, node_2 in enumerate(graph_2["nodes"]):
            nodes_similarity_matrix[i, j] = sim(node_1, node_2)  # 

    links_similarity_matrix = np.zeros((num_nodes_1, num_nodes_2))
    for link_1 in graph_1["links"]:
        for link_2 in graph_2["links"]:
            if link_1["source"] == link_2["source"] and link_1["target"] == link_2["target"]:
                try:
                    i_index_1 = nodes_1.index(link_1["source"])
                    i_index_2 = nodes_2.index(link_2["source"])
                    j_index_1 = nodes_1.index(link_1["target"])
                    j_index_2 = nodes_2.index(link_2["target"])
                except ValueError:
                    continue
                links_similarity_matrix[i_index_1, i_index_2] += 1
                links_similarity_matrix[j_index_1, j_index_2] += 1
    
    cost_matrix = 2 - nodes_similarity_matrix - 0.5 * links_similarity_matrix
    return cost_matrix

def compute_assignment_matrix(graph_1, graph_2):
    cost_matrix = create_cost_matrix(graph_1, graph_2)
    row_ind, col_ind = linear_sum_assignment(cost_matrix) 
    return row_ind, col_ind, cost_matrix[row_ind, col_ind].sum()

def matching(graph_1, graph_2):
    indices_1, indices_2, total_cost = compute_assignment_matrix(graph_1, graph_2)
    return indices_1, indices_2

def ratio_levenshtein(x, y):
    assert len(x) == len(y)
    n = len(x)
    total = 0
    for i in range(n):
        total += Levenshtein.ratio(x[i], y[i])
    return total / n


def flatten(gt, pred, types = None):
    assert len(gt) == len(pred)

    gt_flat = []
    pred_flat = []

    for (sample_gt, sample_pred) in zip(gt, pred):
        union = set()

        union.update(sample_gt)
        union.update(sample_pred)

        for s in union:
            if types: 
                if s in types:
                    if s in sample_gt:
                        gt_flat.append(types.index(s)+1)
                    else:
                        gt_flat.append(0)

                    if s in sample_pred:
                        pred_flat.append(types.index(s)+1)
                    else:
                        pred_flat.append(0)
                else:
                    gt_flat.append(0)
                    pred_flat.append(0)
            else:
                if s in sample_gt:
                    gt_flat.append(1)
                else:
                    gt_flat.append(0)

                if s in sample_pred:
                    pred_flat.append(1)
                else:
                    pred_flat.append(0)
    return gt_flat, pred_flat

def print_results(per_type, micro, macro, types, result_dict = None):
    columns = ('type', 'precision', 'recall', 'f1-score', 'support')

    row_fmt = "%30s" + (" %12s" * (len(columns) - 1))
    logger.info(row_fmt % columns)

    metrics_per_type = []
    for i, t in enumerate(types):
        metrics = []
        for j in range(len(per_type)):
            metrics.append(per_type[j][i])
        metrics_per_type.append(metrics)

    for m, t in zip(metrics_per_type, types):
        logger.info(row_fmt % get_row(m, t))
        if result_dict is not None:
            result_dict[t] = {}
            result_dict[t]["precision"] = m[0]
            result_dict[t]["recall"] = m[1]
            result_dict[t]["f1-score"] = m[2]
            result_dict[t]["support"] = int(m[3])

    logger.info('')

    # micro
    logger.info(row_fmt % get_row(micro, 'micro'))

    # macro
    logger.info(row_fmt % get_row(macro, 'macro'))

def get_row(data, label):
    row = [label]
    for i in range(len(data) - 1):
        row.append("%.2f" % (data[i] * 100))
    row.append(data[3])
    return tuple(row)

def get_content_type(content):
    content = content.strip('\'')
    assert isinstance(content, str), content
    # image
    for ext in ["jpg", "png", "jpeg", "gif", "bmp", "tiff", "svg", "ico"]:
        if "."+ext in content:
            return "image"
    # audio
    for ext in ["mp3", "wav", "wma", "ogg", "aac", "flac", "aiff", "au"]:
        if "."+ext in content:
            return "audio"
    # video
    for ext in ["mp4", "avi", "mov", "flv", "wmv", "mkv", "webm", "m4v", "mpg", "mpeg"]:
        if "."+ext in content:
            return "video"
    return "text"


def _safe_json_loads(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _normalize_label_sample(data):
    sample = dict(data)
    task_nodes = sample.get("task_nodes")
    task_links = sample.get("task_links")
    task_steps = sample.get("task_steps")

    if task_nodes is None:
        task_nodes = sample.get("tool_nodes", [])
    if task_links is None:
        task_links = sample.get("tool_links", [])
    if task_steps is None:
        task_steps = sample.get("tool_steps", [])

    task_nodes = _safe_json_loads(task_nodes)
    task_links = _safe_json_loads(task_links)
    task_steps = _safe_json_loads(task_steps)

    if not isinstance(task_nodes, list):
        task_nodes = []
    if not isinstance(task_links, list):
        task_links = []
    if not isinstance(task_steps, list):
        task_steps = []

    sample["task_nodes"] = task_nodes
    sample["task_links"] = task_links
    sample["task_steps"] = task_steps
    sample["type"] = sample.get("type", sample.get("method", "overall"))
    sample["n_tools"] = sample.get("n_tools", len(task_nodes))
    sample["id"] = str(sample.get("id"))
    return sample

def _normalize_task_name(name):
    return str(name).replace("_", " ")


def _looks_like_node_reference(argument):
    if not isinstance(argument, str):
        return False
    if re.search(r"<node-\d+>", argument):
        return True
    return re.search(r"(?:output\s+of\s+step|step)\s*\d+", argument, flags=re.IGNORECASE) is not None


def _build_incoming_source_map(task_links):
    incoming_sources = {}
    if not isinstance(task_links, list):
        return incoming_sources
    for link in task_links:
        if not isinstance(link, dict):
            continue
        source = _normalize_task_name(link.get("source", ""))
        target = _normalize_task_name(link.get("target", ""))
        if not source or not target:
            continue
        incoming_sources.setdefault(target, set()).add(source)
    return incoming_sources


def _resolve_node_reference(argument, current_index, node_names, current_task=None, incoming_source_map=None):
    if not isinstance(argument, str):
        return None

    node_match = re.search(r"<node-(\d+)>", argument)
    if node_match:
        raw_index = int(node_match.group(1))
        candidates = []
        for candidate in (raw_index, raw_index - 1):
            if 0 <= candidate < len(node_names) and candidate < current_index and candidate not in candidates:
                candidates.append(candidate)
        if not candidates:
            return None

        expected_sources = set()
        if current_task is not None and incoming_source_map is not None:
            expected_sources = incoming_source_map.get(_normalize_task_name(current_task), set())

        if expected_sources:
            matched_candidates = [
                candidate
                for candidate in candidates
                if _normalize_task_name(node_names[candidate]) in expected_sources
            ]
            if len(matched_candidates) == 1:
                return matched_candidates[0]
            if matched_candidates:
                candidates = matched_candidates

        if raw_index == current_index and (raw_index - 1) in candidates:
            return raw_index - 1
        if raw_index in candidates:
            return raw_index
        return max(candidates)

    step_match = re.search(r"(?:output\s+of\s+step|step)\s*(\d+)", argument, flags=re.IGNORECASE)
    if step_match:
        reference_index = int(step_match.group(1)) - 1
        if 0 <= reference_index < len(node_names) and reference_index < current_index:
            return reference_index
    return None


def _materialize_resource_graph(nodes, task_links, tool_output_type_map):
    normalized_nodes = list(nodes) if isinstance(nodes, list) else []
    node_names = [_normalize_task_name(node.get("task", "")) for node in normalized_nodes]
    incoming_source_map = _build_incoming_source_map(task_links)
    links = []
    node_arguments = []

    for current_index, node in enumerate(normalized_nodes):
        current_task = node_names[current_index]
        new_arguments = []
        for argument in node.get("arguments", []):
            try:
                raw_argument = argument
                if isinstance(raw_argument, dict):
                    raw_argument = list(raw_argument.values())[0]
                if isinstance(raw_argument, list):
                    raw_argument = " ".join(str(item) for item in raw_argument)
                if raw_argument is None:
                    continue
                if not isinstance(raw_argument, str):
                    raw_argument = str(raw_argument)

                reference_node_index = _resolve_node_reference(
                    raw_argument,
                    current_index,
                    node_names,
                    current_task=current_task,
                    incoming_source_map=incoming_source_map,
                )
                if reference_node_index is not None:
                    source_task = node_names[reference_node_index]
                    links.append({"source": source_task, "target": current_task})
                    new_arguments.append(
                        {"name": tool_output_type_map.get(source_task, "other"), "value": source_task}
                    )
                    continue
                if _looks_like_node_reference(raw_argument):
                    continue
                new_arguments.append({"name": get_content_type(raw_argument), "value": raw_argument})
            except Exception:
                continue
        node_arguments.append(new_arguments)

    return node_names, links, node_arguments

@click.command()
@click.option("--data_dir", default="data_multimedia", help="The directory of the data.")
@click.option("--prediction_dir", default="predictions_pipeline_agent", help="The directory of the data.")
@click.option("--save_dir", default=None, help="The directory to save the evaluation results")
@click.option("--alignment", default=None)
@click.option("--splits", "-s", multiple=True, default=["all"])
@click.option("--n_tools", "-n", multiple=True, default=["all"])
@click.option("--mode", default="add")
@click.option("--metric", "-m", multiple=True, default=["f1", "ed", "link", "argument"])
@click.option("--llm", default="pipeline_orchestrator_agent")
@click.option("--dependency_type", type=str, default="resource")
@click.option("--prompting", default="cot")
def main(data_dir, prediction_dir, save_dir, splits, n_tools, mode, metric, llm, dependency_type, alignment, prompting):
    assert dependency_type in ["resource", "temporal"], "Dependency type not supported"
    args = locals()
    
    if save_dir is None:
        save_dir = prediction_dir.replace("predictions", "metrics") 
        save_dir = save_dir + f"_alignment_{alignment}" if alignment is not None else save_dir

    formatter = logging.Formatter(f'%(asctime)s - [ {llm} ] - %(levelname)s - %(message)s')
    if not os.path.exists(f'{data_dir}/{save_dir}'):
        os.makedirs(f'{data_dir}/{save_dir}')

    #timestamp = datetime.now().strftime("%Y%m%d")
    metric_file = f'{data_dir}/{save_dir}/{llm}_{timestamp}.json'
    if os.path.exists(metric_file):
        all_metric_dict = json.load(open(metric_file, "r"))
    else:
        all_metric_dict = {}
    
    file_handler = logging.FileHandler(f'{data_dir}/{save_dir}/{llm}_{timestamp}.log')
    stream_handler = logging.StreamHandler()
    
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    if "all" in metric:
        metric = ["f1", "ed", "link", "argument", "rouge", "bertscore"]
        if prompting != "cot":
            metric = ["f1", "ed", "link", "argument"]

    logger.info(f"Starts with: {args}")

    tool_desc = json.load(open(f"{data_dir}/tool_desc.json", "r"))
    tool_map = {tool["id"]: i+1 for i, tool in enumerate(tool_desc["nodes"])}
    tool_map_reverse = {i+1: tool["id"] for i, tool in enumerate(tool_desc["nodes"])}
    tool_map_reverse[0] = "NEGATIVE"
    tool_map["<PAD>"] = -1

    tool_output_type_map = None
    if dependency_type == "resource":
        tool_output_type_map = {
            _normalize_task_name(tool["id"]): tool["output-type"][0] if len(tool["output-type"]) else "none"
            for tool in tool_desc["nodes"]
        }

    splits = list(splits)
    n_tools = list(n_tools)

    if "all" in splits:
        splits = ["overall", "single", "chain", "dag", ]
    if "all" in n_tools:
        n_tools = ["overall"] + [str(i) for i in range(1, 11)]
    
    group = []

    if mode == "mul":
        for s in splits:
            for n in n_tools:
                if (s, n) not in group:
                    group.append((s, n))
    elif mode == "add":
        for s in splits:
            if (s, "overall") not in group:
                group.append((s, "overall"))
        for n in n_tools:
            if ("overall", n) not in group:
                group.append(("overall", n))
    else:
        assert False, "mode should be mul or add"

    for s, n in group:
        logger.info("-"*15)
        logger.info(f"Tools Number: {n}, Task Split: {s}")
        evaluate(data_dir, prediction_dir, llm, s, n, metric, tool_desc, tool_map, tool_output_type_map, tool_map_reverse, all_metric_dict, dependency_type=dependency_type, alignment=alignment)

    metric_json = open(metric_file, "w")
    metric_json.write(json.dumps(all_metric_dict, indent=2))

def evaluate(data_dir, prediction_dir, llm, split, n_tool, metric, tool_desc, tool_map, tool_output_type_map, tool_map_reverse, all_metric_dict, dependency_type, alignment = None):
    if f"{split}_{n_tool}" in all_metric_dict:
        metric_dict = all_metric_dict[f"{split}_{n_tool}"]
    else:
        metric_dict = {}
        all_metric_dict[f"{split}_{n_tool}"] = metric_dict

    label_rf = open(f"{data_dir}/data.json", "r")
    
    alignment_ids = None
    if alignment is not None:
        if alignment == "human":
            label_rf = open(f"{data_dir}/data.json", "r")
            logger.info(f"Alignment Mode: {alignment} ({len(label_rf.readlines())})")
        else:
            alignment_file = open(f"{data_dir}/alignment_ids.json", "r")
            alignment_ids = json.load(alignment_file)
            alignment_ids = list(itertools.chain(*alignment_ids[f"{alignment}_alignment_id"].values()))
            logger.info(f"Alignment Mode: {alignment} ({len(alignment_ids)})")
        
    predcition_rf = open(f"{data_dir}/{prediction_dir}/{llm}_{timestamp}.json", "r")

    predcitions = {}
    labels = {}
    label_rf = open(f"{data_dir}/data.json", "r")
    for line in label_rf:
        data = _normalize_label_sample(json.loads(line))
        real_tool_num = int(data.get("n_tools", len(data["task_nodes"])))
        if alignment_ids is None or data["id"] in alignment_ids:
            if split == "overall" or data["type"] == split:
                if n_tool == "overall" or str(real_tool_num) == n_tool:
                    id = data["id"]
                    labels[id] = data

    for line in predcition_rf:
        try:
            data = json.loads(line)
        except Exception as e:
            print(e)
            print(line)
            exit()
        id = data["id"]
        predcitions[id] = data

    ids = set(labels.keys()).intersection(set(predcitions.keys()))
    labels = {id: labels[id] for id in ids}
    predcitions = {id: predcitions[id] for id in ids}

    predcition_task_steps = []
    label_task_steps = []
    predcition_names = []
    label_names = []
    label_graphs = []
    predcition_graphs = []
    label_links = []
    predcition_links = []
    label_task_arg_names = []
    predcition_task_arg_names = []
    label_task_arg_name_values = []
    predcition_task_arg_name_values = []

    for id in ids:
        try:
            label = labels[id]
            predcition = predcitions[id]

            if "rouge" in metric or "bertscore" in metric:
                predcition_task_step = predcition["result"]["task_steps"]
                label_task_step = label["task_steps"]
                
                try:
                    if isinstance(predcition_task_step[0], str):
                        predcition_task_steps.append("\n".join(predcition_task_step))
                    else:
                        if "task" in predcition_task_step[0]:
                            predcition_task_steps.append("\n".join([step["task"] for step in predcition_task_step]))
                        elif "step" in predcition_task_step[0]:
                            predcition_task_steps.append("\n".join([step["step"] for step in predcition_task_step]))
                        elif "id" in predcition_task_step[0]:
                            predcition_task_steps.append("\n".join([step["id"] for step in predcition_task_step]))
                        elif "step_name" in predcition_task_step[0]:
                            predcition_task_steps.append("\n".join([step["step_name"] for step in predcition_task_step]))
                        else:
                            predcition_task_steps.append("\n".join([step["description"] for step in predcition_task_step]))
                except Exception as e:
                    predcition_task_steps.append(str(predcition_task_step))

                label_task_steps.append("\n".join(label_task_step))

            label_nodes = label["task_nodes"]
            predcition_nodes = predcition["result"]["task_nodes"] 

            label_node_name = [node["task"] for node in label_nodes]
            predcition_node_name = [node["task"] for node in predcition_nodes]

            label_task_arg_name = []
            predcition_task_arg_name = []

            label_task_arg_name_value = []
            predcition_task_arg_name_value = []
                
            if dependency_type == "resource":
                label_node_name, label_link, label_node_argument = _materialize_resource_graph(
                    label_nodes,
                    label.get("task_links", []),
                    tool_output_type_map,
                )
                predcition_node_name, predcition_link, predcition_node_argument = _materialize_resource_graph(
                    predcition_nodes,
                    predcition["result"].get("task_links", []),
                    tool_output_type_map,
                )
            else:
                predcition_link = predcition["result"]["task_links"]
                label_link = label["task_links"]
                predcition_node_argument = [node.get("arguments", []) for node in predcition_nodes]
                label_node_argument = [node["arguments"] for node in label_nodes]

            for task, arguments in zip (predcition_node_name, predcition_node_argument):
                for argument in arguments:
                    predcition_task_arg_name.append(f"{task}-{argument['name']}")
                    predcition_task_arg_name_value.append(f"{task}-{argument['name']}-{argument['value']}")
            
            for task, arguments in zip (label_node_name, label_node_argument):
                for argument in arguments:
                    label_task_arg_name.append(f"{task}-{argument['name']}")
                    label_task_arg_name_value.append(f"{task}-{argument['name']}-{argument['value']}")

            label_graph = {
                "nodes": label_node_name,
                "links": label_link,
                "arguments": label_node_argument
            }
            predcition_graph = {
                "nodes": predcition_node_name,
                "links": predcition_link,
                "arguments": predcition_node_argument
            }

            label_graphs.append(label_graph)
            predcition_graphs.append(predcition_graph)

            for node_name in predcition_node_name:
                assert isinstance(node_name, str), node_name

            predcition_names.append(predcition_node_name)
            label_names.append(label_node_name)

            predcition_task_arg_names.append(predcition_task_arg_name)
            label_task_arg_names.append(label_task_arg_name)
        
            predcition_task_arg_name_values.append(predcition_task_arg_name_value)
            label_task_arg_name_values.append(label_task_arg_name_value)

            label_links.append(label_link)
            predcition_links.append(predcition_link)

        except Exception as e:
            logger.info(f"Parsing Error: {e}, Ignore #id {id}")
            logger.info(traceback.format_exc())
            
    logger.info(f"Step Supports: {len(label_task_steps)} / {len(ids)}")
    logger.info(f"Node Support: {len(label_names)} / {len(ids)}")
    logger.info(f"Link Support: {len(label_links)} / {len(ids)}")
    logger.info(f"Argument Support: {len(label_graphs)} / {len(ids)}")

    metric_dict["all_samples"] = len(ids)
    metric_dict["step_supports"] = len(label_task_steps)
    metric_dict["node_supports"] = len(label_names)
    metric_dict["link_supports"] = len(label_links)
    metric_dict["argument_supports"] = len(label_graphs)

    if len(label_graphs) == 0 or len(label_names) == 0 or len(label_links) == 0:
        logger.info("No supports, skip")
        return

    if "rouge" in metric:
        rouge = load_metric("rouge")
        rouge_scores = rouge.compute(predictions=predcition_task_steps, references=label_task_steps, use_aggregator=True)
        for key in rouge_scores:
            logger.info(f"Step {key}: {rouge_scores[key].mid.fmeasure}")
            metric_dict[f"step_{key}"] = rouge_scores[key].mid.fmeasure

    if "bertscore" in metric:
        bertscore = load_metric("bertscore")
        bertscore_scores = bertscore.compute(predictions=predcition_task_steps, references=label_task_steps, model_type="roberta-large")
        for key in bertscore_scores:
            if key in ["precision", "recall", "f1"]:
                bertscore_scores[key] = np.mean(bertscore_scores[key])
                logger.info(f"Step BERTScore {key}: {bertscore_scores[key]}")
                metric_dict[f"step_bertscore_{key}"] = bertscore_scores[key]
    
    if "f1" in metric or "argument" in metric:
        types = list(range(1, len(tool_desc["nodes"])+1))
        types_name = [tool_map_reverse[i] for i in types]
        gt_flat, pred_flat = flatten(label_names, predcition_names, types = types_name)

        per_type = prfs(gt_flat, pred_flat, labels=types, average=None)
        micro = prfs(gt_flat, pred_flat, labels=types, average='micro')[:-1]
        macro = prfs(gt_flat, pred_flat, labels=types, average='macro')[:-1]
        total_support = sum(per_type[-1])

        logger.info(f"Node Micro Precision [ No Matching ]: {micro[0]}")
        logger.info(f"Node Micro Recall [ No Matching ]: {micro[1]}")
        logger.info(f"Node Micro F1 [ No Matching ]: {micro[2]}")
        logger.info(f"Node Macro Precision [ No Matching ]: {macro[0]}")
        logger.info(f"Node Macro Recall [ No Matching ]: {macro[1]}")
        logger.info(f"Node Macro F1 [ No Matching ]: {macro[2]}")
        logger.info("Node Detailed Report [ No Matching ]: ")
        metric_dict["node_micro_precision_no_matching"] = micro[0]
        metric_dict["node_micro_recall_no_matching"] = micro[1]
        metric_dict["node_micro_f1_no_matching"] = micro[2]
        metric_dict["node_macro_precision_no_matching"] = macro[0]
        metric_dict["node_macro_recall_no_matching"] = macro[1]
        metric_dict["node_macro_f1_no_matching"] = macro[2]

        per_type_metric = {}
        metric_dict["node_per_type_no_matchcing"] = per_type_metric
        print_results(per_type, list(micro) + [total_support], list(macro) + [total_support], types_name, result_dict = per_type_metric)


        gt_flat, pred_flat = flatten(label_task_arg_names, predcition_task_arg_names)
        if len(gt_flat) == 0:
            logger.info("Argument Task-ArgName Binary F1: [ No Matching ]: skipped (empty support)")
            metric_dict["argument_task_argname_binary_f1_no_matching"] = None
        else:
            micro = prfs(gt_flat, pred_flat, average="binary")[:-1]
            logger.info(f"Argument Task-ArgName Binary F1: [ No Matching ]: {micro[-1]}")
            metric_dict["argument_task_argname_binary_f1_no_matching"] = micro[-1]

        gt_flat, pred_flat = flatten(label_task_arg_name_values, predcition_task_arg_name_values)
        if len(gt_flat) == 0:
            logger.info("Argument Task-ArgName-Value Binary F1 [ No Matching ]: skipped (empty support)")
            metric_dict["argument_task_argname_value_binary_f1_no_matching"] = None
        else:
            micro = prfs(gt_flat, pred_flat, average="binary")[:-1]
            logger.info(f"Argument Task-ArgName-Value Binary F1 [ No Matching ]: {micro[-1]}")
            metric_dict["argument_task_argname_value_binary_f1_no_matching"] = micro[-1]

    if "ed" in metric:
        labels = []
        predcitions = []
        for label_name, predcition_name in zip(label_names, predcition_names):
            labels.append([tool_map.get(name, 0) for name in label_name])
            predcitions.append([tool_map.get(name, 0) for name in predcition_name])
        ed = ratio_levenshtein(predcitions, labels)
        logger.info(f"Edit Distance: {1-ed}")
        metric_dict["edit_distance"] = 1-ed
    
    if "link" in metric:
        tuple_label_links = []
        tuple_predcition_links = []
        for label_link, predcition_link in zip(label_links, predcition_links):
            tuple_label_links.append([(link["source"], link["target"]) for link in label_link])
            tuple_predcition_links.append([(link["source"], link["target"]) for link in predcition_link])
        
        gt_flat, pred_flat = flatten(tuple_label_links, tuple_predcition_links)
        if len(gt_flat) == 0:
            logger.info("Link Binary F1: skipped (empty support)")
            metric_dict["link_binary_f1"] = None
        else:
            micro = prfs(gt_flat, pred_flat, average="binary")[:-1]
            logger.info(f"Link Binary F1: {micro[-1]}")
            metric_dict["link_binary_f1"] = micro[-1]

if __name__ == "__main__":
    main()


