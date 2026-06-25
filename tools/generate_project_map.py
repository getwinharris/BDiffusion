#!/usr/bin/env python3
"""
generate_project_map.py

Systematically scans the BDiffusion repository and generates
project_map.mmd -- a NotebookLM-style source graph for coding agents.

Usage:
    python3 tools/generate_project_map.py          # generate project_map.mmd
    python3 tools/generate_project_map.py --check   # verify existing map is up-to-date
"""

import ast
import re
import sys
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "project_map.mmd"

EXCLUDE_DIRS = {".git", "__pycache__"}
IMPORT_EXCLUDE_PREFIXES = {"torch", "transformers", "numpy", "deepspeed",
                           "peft", "wandb", "configargparse", "omegaconf",
                           "datasets", "json", "tempfile", "tqdm", "copy",
                           "time", "math", "gc", "abc", "typing", "collections",
                           "logging", "os", "random", "argparse", "human_eval",
                           "bigcode_eval"}

STALE_KEYWORDS = ["raise NotImplementedError", "# TODO", "# FIXME"]


def read_file(path):
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def rel_path(path):
    return path.relative_to(REPO_ROOT)


def safe_id(name):
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_")


def node(path):
    return "n_" + safe_id(str(rel_path(path)))


def collect(root, pattern):
    files = []
    for p in root.rglob(pattern):
        r = rel_path(p)
        if any(part.startswith(".") or part in EXCLUDE_DIRS for part in r.parts):
            continue
        files.append(p)
    return sorted(files)


def parse_ast(path):
    src = read_file(path)
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def get_imports(path):
    tree = parse_ast(path)
    if tree is None:
        return set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return imports


def get_classes(path):
    tree = parse_ast(path)
    if tree is None:
        return []
    out = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            bases = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
            methods = [n.name for n in ast.iter_child_nodes(node)
                       if isinstance(n, ast.FunctionDef)]
            out.append({"name": node.name, "bases": bases, "methods": methods,
                        "lineno": node.lineno})
    return out


def get_functions(path):
    tree = parse_ast(path)
    if tree is None:
        return []
    out = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef):
            out.append({"name": node.name, "lineno": node.lineno})
    return out


def get_decorators(path):
    tree = parse_ast(path)
    if tree is None:
        return []
    decs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.decorator_list:
            names = []
            for d in node.decorator_list:
                if isinstance(d, ast.Name):
                    names.append(d.id)
                elif isinstance(d, ast.Attribute):
                    names.append(d.attr)
                elif isinstance(d, ast.Call):
                    if isinstance(d.func, ast.Name):
                        names.append(d.func.id)
                    elif isinstance(d.func, ast.Attribute):
                        names.append(d.func.attr)
            if names:
                decs.append((node.name, names, node.lineno))
    return decs


def check_stale(path):
    if path.name == "generate_project_map.py":
        return []
    src = read_file(path)
    results = []
    for kw in STALE_KEYWORDS:
        for i, line in enumerate(src.splitlines(), 1):
            if kw in line:
                results.append((i, kw.strip()))
                break
    return results


def mermaid_label(text):
    return text.replace("\\", "\\\\").replace('"', "'")


class ProjectMapGenerator:
    def __init__(self):
        self.py_files = collect(REPO_ROOT, "*.py")
        self.sh_files = collect(REPO_ROOT, "*.sh")
        self.cfg_files = collect(REPO_ROOT, "*.cfg")
        self.agents_files = collect(REPO_ROOT, "AGENTS.md")
        self.lines = []

    def emit(self, line=""):
        self.lines.append(line)

    def ln(self):
        self.lines.append("")

    def header(self):
        self.emit("```mermaid")
        self.emit("graph TB")
        self.ln()
        self.emit("%% ============================================================")
        self.emit("%% BDiffusion -- Project Map (auto-generated)")
        self.emit("%% ============================================================")
        self.ln()

        self.emit("%% Styles")
        self.emit("classDef green fill:#e8f5e9,stroke:#2e7d32")
        self.emit("classDef blue fill:#e3f2fd,stroke:#1565c0")
        self.emit("classDef orange fill:#fff3e0,stroke:#e65100")
        self.emit("classDef red fill:#ffebee,stroke:#c62828")
        self.emit("classDef purple fill:#f3e5f5,stroke:#6a1b9a")
        self.emit("classDef teal fill:#e0f2f1,stroke:#004d40")
        self.emit("classDef amber fill:#fff8e1,stroke:#f9a825")
        self.ln()

    def footer(self):
        self.ln()
        self.emit("%% end of project_map.mmd")
        self.emit("```")

    def style_subgraph(self, title, node_ids, color_class):
        nid = safe_id(title[:40])
        self.emit(f"    subgraph {nid} [{title}]")
        for nid2 in node_ids:
            self.emit(f"        {nid2}")
        self.emit("    end")
        self.ln()

    def generate(self):

        self.header()

        py_file_map = {rel_path(f): f for f in self.py_files}
        sh_file_map = {rel_path(f): f for f in self.sh_files}
        cfg_file_map = {rel_path(f): f for f in self.cfg_files}

        all_fpaths = list(py_file_map.keys()) + list(sh_file_map.keys()) + list(cfg_file_map.keys())
        for f in self.agents_files:
            all_fpaths.append(rel_path(f))

        # ── Section 1: Entry Points ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 1: ENTRY POINTS")
        self.emit("%% ============================================================")
        self.ln()
        self.emit("    subgraph EntryPoints [Entry Points]")
        self.emit("        direction LR")
        for r, f in sorted(py_file_map.items()):
            if r.parent == Path("."):
                classes = get_classes(f)
                funcs = get_functions(f)
                label = r.name
                if classes:
                    clist = ", ".join(c["name"] for c in classes)
                    label += f"\\n[{clist}]"
                if funcs:
                    flist = ", ".join(fn["name"] for fn in funcs)
                    label += f"\\n({flist})"
                self.emit(f"        {node(f)}[\"{label}\"]")
        self.emit("    end")
        self.ln()

        # ── Section 2: Architecture Subgraphs ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 2: ARCHITECTURE LAYERS")
        self.emit("%% ============================================================")
        self.ln()

        # Flow LLMs
        flow_ids = []
        for r, f in sorted(py_file_map.items()):
            if str(r).startswith("flow_llms/"):
                classes = get_classes(f)
                label = r.name
                if classes:
                    clist = ", ".join(c["name"] for c in classes)
                    if len(clist) > 60:
                        clist = clist[:57] + "..."
                    label += f"\\n[{clist}]"
                self.emit(f"        {node(f)}[\"{label}\"]")
                flow_ids.append(node(f))
        if flow_ids:
            self.emit(f"    subgraph FlowModels [Flow-Augmented LLMs]")
            for nid in flow_ids:
                self.emit(f"        {nid}")
            self.emit("    end")
            self.ln()

        # Pipeline
        pipe_ids = []
        for r, f in sorted(py_file_map.items()):
            if str(r).startswith("pipeline/"):
                classes = get_classes(f)
                funcs = get_functions(f)
                label = r.name
                if classes:
                    clist = ", ".join(c["name"] for c in classes)
                    label += f"\\n[{clist}]"
                if funcs:
                    flist = ", ".join(fn["name"] for fn in funcs[:8])
                    if len(funcs) > 8:
                        flist += "..."
                    label += f"\\n({flist})"
                self.emit(f"        {node(f)}[\"{label}\"]")
                pipe_ids.append(node(f))
        if pipe_ids:
            self.emit(f"    subgraph Pipeline [Training / Inference Pipeline]")
            for nid in pipe_ids:
                self.emit(f"        {nid}")
            self.emit("    end")
            self.ln()

        # Tasks
        task_ids = []
        for r, f in sorted(py_file_map.items()):
            if str(r).startswith("tasks/"):
                classes = get_classes(f)
                label = r.name
                if classes:
                    clist = ", ".join(c["name"] for c in classes)
                    label += f"\\n[{clist}]"
                self.emit(f"        {node(f)}[\"{label}\"]")
                task_ids.append(node(f))
        if task_ids:
            self.emit(f"    subgraph Tasks [Task Processors & Evaluators]")
            for nid in task_ids:
                self.emit(f"        {nid}")
            self.emit("    end")
            self.ln()

        # Configs
        cfg_ids = []
        for r, f in sorted(cfg_file_map.items()):
            parts = str(r).split("/")
            label = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
            src = read_file(f)
            sections = sorted(set(re.findall(r"\[(\w+)\]", src)))
            if sections:
                label += f"\\n({', '.join(sections)})"
            self.emit(f"        {node(f)}[\"{label}\"]")
            cfg_ids.append(node(f))
        if cfg_ids:
            self.emit(f"    subgraph Configs [Configuration Files]")
            for nid in cfg_ids:
                self.emit(f"        {nid}")
            self.emit("    end")
            self.ln()

        # Scripts
        scr_ids = []
        for r, f in sorted(sh_file_map.items()):
            self.emit(f"        {node(f)}[\"{r.name}\"]")
            scr_ids.append(node(f))
        if scr_ids:
            self.emit(f"    subgraph Scripts [Shell Launchers]")
            for nid in scr_ids:
                self.emit(f"        {nid}")
            self.emit("    end")
            self.ln()

        # AGENTS.md hierarchy
        if self.agents_files:
            agent_ids = []
            for f in self.agents_files:
                r = rel_path(f)
                self.emit(f"        {node(f)}[\"{r}\"]")
                agent_ids.append(node(f))
            self.emit(f"    subgraph AgentsDocs [DOCS-INDEX Hierarchy]")
            for nid in agent_ids:
                self.emit(f"        {nid}")
            self.emit("    end")
            self.ln()

        # Tools
        tool_ids = []
        for r, f in sorted(py_file_map.items()):
            if str(r).startswith("tools/"):
                funcs = get_functions(f)
                label = r.name
                if funcs:
                    flist = ", ".join(fn["name"] for fn in funcs[:7])
                    if len(funcs) > 7:
                        flist += "..."
                    label += f"\\n({flist})"
                self.emit(f"        {node(f)}[\"{label}\"]")
                tool_ids.append(node(f))
        if tool_ids:
            self.emit(f"    subgraph Tools [Developer Tools]")
            for nid in tool_ids:
                self.emit(f"        {nid}")
            self.emit("    end")
            self.ln()

        # ── Section 3: Data Flow ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 3: DATA FLOW")
        self.emit("%% ============================================================")
        self.ln()

        data_flow_nodes = []
        # HuggingFace datasets
        hf_node = "Data_HF[HF Datasets\\n(load_dataset)]"
        data_flow_nodes.append(hf_node)
        self.emit(f"    {hf_node}")

        # data.py
        data_py = py_file_map.get(Path("data.py"))
        if data_py:
            data_flow_nodes.append(node(data_py))
            self.emit(f"    {node(data_py)}")

        # pipeline/data.py
        pipe_data_py = py_file_map.get(Path("pipeline") / "data.py")
        if pipe_data_py:
            self.emit(f"    {node(pipe_data_py)}")
            data_flow_nodes.append(node(pipe_data_py))

        # Task processors (all)
        for r, f in sorted(py_file_map.items()):
            if str(r).startswith("tasks/") and r.name != "__init__.py" and r.name != "base.py" and r.name != "utils.py" and r.name != "constants.py" and r.name != "templates.py":
                self.emit(f"    {node(f)}")
                data_flow_nodes.append(node(f))

        self.ln()
        # Task base
        task_base = py_file_map.get(Path("tasks") / "base.py")
        if task_base:
            self.emit(f"    {node(task_base)}")
        task_constants = py_file_map.get(Path("tasks") / "constants.py")
        if task_constants:
            self.emit(f"    {node(task_constants)}")
        task_utils = py_file_map.get(Path("tasks") / "utils.py")
        if task_utils:
            self.emit(f"    {node(task_utils)}")
        template_py = py_file_map.get(Path("tasks") / "templates.py")
        if template_py:
            self.emit(f"    {node(template_py)}")

        self.ln()

        self.emit("    %% Edges: Data flow")
        self.emit(f"    {hf_node} -->|\"load_dataset\"| {node(data_py)}")
        self.emit(f"    {node(data_py)} -->|\"line2data\"| {node(task_base)}")
        # Connect task ABC to each processor
        for r, f in sorted(py_file_map.items()):
            if str(r).startswith("tasks/") and r.name not in ("__init__.py", "base.py", "utils.py", "constants.py", "templates.py"):
                self.emit(f"    {node(f)} -->|inherits| {node(task_base)}")
        # Connect constants and utils to each
        for r, f in sorted(py_file_map.items()):
            if str(r).startswith("tasks/") and r.name not in ("__init__.py", "base.py", "utils.py", "constants.py", "templates.py", "HumanEval.jsonl.gz"):
                self.emit(f"    {node(f)} -->|uses| {node(task_constants)}")
                self.emit(f"    {node(f)} -->|uses| {node(task_utils)}")
                if template_py:
                    self.emit(f"    {node(f)} -->|uses| {node(template_py)}")
        self.ln()

        # ── Section 4: Training Flow ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 4: TRAINING FLOW")
        self.emit("%% ============================================================")
        self.ln()

        training_py = py_file_map.get(Path("training.py"))
        pipe_training_py = py_file_map.get(Path("pipeline") / "training.py")
        tr_args_py = py_file_map.get(Path("pipeline") / "argument.py")

        if training_py and pipe_training_py:
            self.emit(f"    {node(training_py)} -->|\"orchestrates via\"| {node(pipe_training_py)}")
            self.emit(f"    {node(pipe_training_py)} -->|\"args\"| {node(tr_args_py)}")

        # training.py build_model
        if training_py:
            self.emit(f"    {node(training_py)} -->|\"builds\"| {node(py_file_map.get(Path('flow_llms') / 'llama.py'))}")
            self.emit(f"    {node(training_py)} -->|\"builds\"| {node(py_file_map.get(Path('flow_llms') / 'qwen2.py'))}")

        # TrainingPipeline -> train_forward_step
        if training_py:
            train_fwd = "Training_train_fwd[\"train_forward_step()\\nforward pass + CE loss\"]"
            self.emit(f"    {train_fwd}")
            val_fwd = "Training_val_fwd[\"valid_forward_step()\\nforward + nstep eval + accuracy\"]"
            self.emit(f"    {val_fwd}")
            self.emit(f"    {node(pipe_training_py)} -->|\"calls\"| {train_fwd}")
            self.emit(f"    {node(pipe_training_py)} -->|\"calls\"| {val_fwd}")

            llama_py = py_file_map.get(Path("flow_llms") / "llama.py")
            qwen_py = py_file_map.get(Path("flow_llms") / "qwen2.py")
            for mod in [llama_py, qwen_py]:
                if mod:
                    self.emit(f"    {train_fwd} -->|\"model.forward()\"| {node(mod)}")
                    self.emit(f"    {val_fwd} -->|\"model.forward()\"| {node(mod)}")
                    self.emit(f"    {val_fwd} -->|\"nstep_inference()\"| {node(mod)}")

            # Flow forward details
            utils_py = py_file_map.get(Path("utils.py"))
            if utils_py:
                self.emit(f"    {train_fwd} -->|\"compute_accuracy\"| {node(utils_py)}")
                self.emit(f"    {val_fwd} -->|\"compute_accuracy\"| {node(utils_py)}")

        self.ln()

        # ── Section 5: Inference Flow ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 5: INFERENCE FLOW")
        self.emit("%% ============================================================")
        self.ln()

        bench_py = py_file_map.get(Path("benchmark.py"))
        bench_code_py = py_file_map.get(Path("benchmark_code.py"))
        eval_py = py_file_map.get(Path("evaluation.py"))
        pipe_inf_py = py_file_map.get(Path("pipeline") / "inference.py")
        dec_util_py = py_file_map.get(Path("pipeline") / "decoding_util.py")

        for ep in [bench_py, bench_code_py, eval_py]:
            if ep:
                self.emit(f"    {node(ep)} -->|\"orchestrates via\"| {node(pipe_inf_py)}")

        if pipe_inf_py and dec_util_py:
            self.emit(f"    {node(pipe_inf_py)} -->|\"calls\"| {node(dec_util_py)}")

        if dec_util_py and llama_py:
            self.emit(f"    {node(dec_util_py)} -->|\"autoregressive_decode\"| {node(llama_py)}")

        # nstep_inference detail
        nstep_node = "NStepInference[nstep_inference()\\nODE Integration]"
        vel_node = "ComputeVelocity[compute_velocity()\\nx1_prediction -> velocity]"
        ode_node = "ODEInt[odeint()\\ntorchdiffeq]"
        self.emit(f"    {nstep_node}")
        self.emit(f"    {vel_node}")
        self.emit(f"    {ode_node}")

        if llama_py:
            self.emit(f"    {node(llama_py)} -->|\"calls\"| {nstep_node}")
            self.emit(f"    {nstep_node} -->|\"calls\"| {vel_node}")
            self.emit(f"    {nstep_node} -->|\"integrates\"| {ode_node}")

        # guidance
        cg_node = "Guidance[Classifier-Free Guidance\\ncond + uncond interpolation]"
        self.emit(f"    {cg_node}")
        self.emit(f"    {vel_node} -->|\"optional\"| {cg_node}")

        # flow representation
        flow_rep_node = "FlowRep[Flow Representation\\ninterpolation / noise\\nx0 + t(x1-x0)]"
        x1_pred_node = "X1Pred[x1 estimation\\nlogits -> flow_weights]"
        self.emit(f"    {flow_rep_node}")
        self.emit(f"    {x1_pred_node}")
        if llama_py:
            self.emit(f"    {node(llama_py)} -->|\"sample_timestep\"| {flow_rep_node}")
            self.emit(f"    {vel_node} -->|\"uses\"| {x1_pred_node}")

        self.ln()

        # ── Section 6: Model Architecture ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 6: MODEL ARCHITECTURE")
        self.emit("%% ============================================================")
        self.ln()

        base_py = py_file_map.get(Path("flow_llms") / "base.py")
        base_comp_py = py_file_map.get(Path("flow_llms") / "base_components.py")
        flow_utils_py = py_file_map.get(Path("flow_llms") / "utils.py")
        llama_comp_py = py_file_map.get(Path("flow_llms") / "llama_components.py")
        qwen2_comp_py = py_file_map.get(Path("flow_llms") / "qwen2_components.py")

        # Base components
        if base_py:
            self.emit(f"    {node(base_py)}")
        if base_comp_py:
            self.emit(f"    {node(base_comp_py)}")
        if flow_utils_py:
            self.emit(f"    {node(flow_utils_py)}")

        # Llama components
        if llama_comp_py:
            self.emit(f"    {node(llama_comp_py)}")

        # Connect llama -> components
        if llama_py and llama_comp_py:
            self.emit(f"    {node(llama_py)} -->|\"uses\"| {node(llama_comp_py)}")

        # Connect llama -> base
        if llama_py and base_py:
            self.emit(f"    {node(llama_py)} -->|\"inherits\"| {node(base_py)}")

        if llama_py and base_comp_py:
            self.emit(f"    {node(llama_py)} -->|\"uses\"| {node(base_comp_py)}")

        if llama_py and flow_utils_py:
            self.emit(f"    {node(llama_py)} -->|\"uses schedules\"| {node(flow_utils_py)}")

        # Same for Qwen2
        if qwen_py:
            self.emit(f"    {node(qwen_py)}")
        if qwen2_comp_py:
            self.emit(f"    {node(qwen2_comp_py)}")

        if qwen_py and qwen2_comp_py:
            self.emit(f"    {node(qwen_py)} -->|\"uses\"| {node(qwen2_comp_py)}")

        if qwen_py and base_py:
            self.emit(f"    {node(qwen_py)} -->|\"inherits\"| {node(base_py)}")

        if qwen_py and base_comp_py:
            self.emit(f"    {node(qwen_py)} -->|\"uses\"| {node(base_comp_py)}")

        if qwen_py and flow_utils_py:
            self.emit(f"    {node(qwen_py)} -->|\"uses schedules\"| {node(flow_utils_py)}")

        # Decoder layer detail
        dec_layer = "FlowDecoderLayer[FlowLlamaDecoderLayer / FlowQwen2DecoderLayer\\nModulated RMSNorm + SDPA + MLP]"
        self.emit(f"    {dec_layer}")

        attn_node = "FlowAttention[Flow Attention\\nseparate Q/O projections\\nattend to frozen KV cache]"
        mod_node = "Modulation[Modulation Head\\nDiT-style 6-component\\ntimestep + guidance]"
        lora_node = "LoRA[LoRA Adapters\\nfor flow gate/up/down\\nand Q/O projections]"

        self.emit(f"    {attn_node}")
        self.emit(f"    {mod_node}")
        self.emit(f"    {lora_node}")

        if llama_comp_py:
            self.emit(f"    {node(llama_comp_py)} -->|\"contains\"| {dec_layer}")
            self.emit(f"    {dec_layer} -->|\"uses\"| {attn_node}")
            self.emit(f"    {dec_layer} -->|\"uses\"| {mod_node}")
            self.emit(f"    {dec_layer} -->|\"optional\"| {lora_node}")

        self.ln()

        # ── Section 7: Dtype / Precision ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 7: PRECISION & DTYPE FLOW")
        self.emit("%% ============================================================")
        self.ln()

        dtype_nodes = [
            ('"BF16/FP16\\nModel Forward\\n(base transformer)"'),
            ('"FP32\\nLogits / Loss\\nCE computation"'),
            ('"FP64\\nODE Integration\\ntorchdiffeq"'),
            ('"FP32\\nFlow Representation\\nnoise + interpolation"'),
        ]
        dtype_ids = [f"Dtype_{i}" for i in range(len(dtype_nodes))]
        for i in range(len(dtype_nodes)):
            self.emit(f"    {dtype_ids[i]}[\"{dtype_nodes[i]}\"]")
        for i in range(len(dtype_ids) - 1):
            self.emit(f"    {dtype_ids[i]} -.->|\"mixed precision\"| {dtype_ids[i+1]}")

        self.ln()

        # ── Section 8: Config Flow ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 8: CONFIGURATION FLOW")
        self.emit("%% ============================================================")
        self.ln()

        tr_cfg = cfg_file_map.get(Path("cfgs") / "training.cfg")
        bench_cfg = cfg_file_map.get(Path("cfgs") / "benchmark.cfg")
        bench_code_cfg = cfg_file_map.get(Path("cfgs") / "benchmark_code.cfg")

        if tr_cfg:
            self.emit(f"    {node(tr_cfg)} -->|\"loaded by\"| {node(tr_args_py)}")
        if bench_cfg:
            self.emit(f"    {node(bench_cfg)} -->|\"loaded by\"| {node(tr_args_py)}")
        if bench_code_cfg:
            self.emit(f"    {node(bench_code_cfg)} -->|\"loaded by\"| {node(tr_args_py)}")

        # Model configs
        model_cfgs = [f for r, f in cfg_file_map.items() if str(r).startswith("cfgs/model/")]
        model_cfg_group = "CfgGroup[Model-LAD Configs\\n8 variants: llama 1B/8B, qwen 1.5B/7B\\nfull vs LoRA]"
        self.emit(f"    {model_cfg_group}")
        self.emit(f"    {node(tr_args_py)} -->|\"+ extra_config\"| {model_cfg_group}")

        if training_py:
            self.emit(f"    {model_cfg_group} -->|\"LAD params\"| {node(training_py)}")

        self.ln()

        # ── Section 9: Duplicate & Stale ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 9: DUPLICATE & STALE")
        self.emit("%% ============================================================")
        self.ln()

        # Detect duplicates
        func_map = defaultdict(list)
        for f in self.py_files:
            funcs = get_functions(f)
            for fn in funcs:
                func_map[fn["name"]].append((f, fn["lineno"]))
        duplicates = {k: v for k, v in func_map.items() if len(v) > 1}

        if duplicates:
            for func_name, locations in sorted(duplicates.items()):
                dup_id = safe_id(f"dup_{func_name}")
                self.emit(f"    subgraph {dup_id} [Duplicate: {func_name}]")
                for loc_path, lineno in sorted(locations, key=lambda x: str(x[0])):
                    loc_nid = safe_id(f"{safe_id(str(loc_path.relative_to(REPO_ROOT)))}_{lineno}")
                    self.emit(f"        {loc_nid}[\"{loc_path.relative_to(REPO_ROOT)}:{lineno}\"]")
                self.emit(f"        {dup_id}_note[\"{func_name} appears in {len(locations)} locations\"]")
                self.emit("    end")
                self.emit("")

        # Stale areas
        for f in self.py_files:
            stale_items = check_stale(f)
            if stale_items:
                labels = []
                for lineno, kw in stale_items:
                    labels.append(f"L{lineno}:{kw}")
                if labels:
                    self.emit(f"    stale_{node(f)}[\"{f.relative_to(REPO_ROOT)}: {'; '.join(labels)}\"]:::red")
                    self.ln()

        self.ln()

        # ── Section 10: Missing Expected Links ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 10: MISSING EXPECTED LINKS")
        self.emit("%% ============================================================")
        self.ln()

        missing = [
            ("utils_logging.py", "Metrics class", "unused -- StatisticsTracker from pipeline/util.py used instead"),
            ("pipeline/data.py", "load_data_from_jsonl", "unused -- data.py:load_conversation_data_from_hf is active"),
            ("utils.py", "estimate_nstep_cross_entropy", "raises NotImplementedError"),
            ("utils.py", "add_pad_token", "duplicate of pipeline/util.py:add_pad_token"),
            ("pipeline/util.py", "build_model", "generic AutoModel builder; training.py:build_model is LAD-aware"),
            ("flow_llms/base_components.py", "BatchedLinear", "defined but may be unused"),
            ("pipeline/data.py", "DataReformatter", "ABC with no concrete implementations"),
            ("pipeline/data.py", "IdentityDataProcessor", "simple pass-through, likely test-only"),
        ]
        for fname, item, note in missing:
            mid = f"missing_{safe_id(fname)}_{safe_id(item)}"
            self.emit(f"    {mid}[\"{fname}: {item}\"]:::amber")
            self.emit(f"    {mid}_note[\"{note}\"]:::amber")
        self.ln()

        # Missing artifacts
        self.emit("%% Missing Artifacts")
        for m in ["VERSION", "CHANGELOG.md"]:
            if not (REPO_ROOT / m).exists():
                aid = f"missing_{safe_id(m)}"
                self.emit(f"    {aid}[\"{m} -- MISSING\"]:::red")

        self.ln()

        # ── Section 11: Shell Script -> Entry Points ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 11: SHELL SCRIPT LAUNCHERS")
        self.emit("%% ============================================================")
        self.ln()

        sh_to_py = {
            "run_training.sh": "training.py",
            "run_bench_tasks.sh": "benchmark.py",
            "run_bench_code.sh": "benchmark_code.py",
            "run_bench.sh": None,
            "run_bench_full.sh": None,
        }
        for sh_name, py_name in sh_to_py.items():
            sh_f = sh_file_map.get(Path("scripts") / sh_name)
            if sh_f is None:
                continue
            if py_name:
                py_f = py_file_map.get(Path(py_name))
                if py_f:
                    self.emit(f"    {node(sh_f)} -->|\"deepspeed\"| {node(py_f)}")
            else:
                content = read_file(sh_f)
                for py_candidate in ["benchmark.py", "benchmark_code.py"]:
                    if py_candidate in content:
                        py_f = py_file_map.get(Path(py_candidate))
                        if py_f:
                            self.emit(f"    {node(sh_f)} -.->|\"calls\"| {node(py_f)}")

        self.ln()

        # ── Section 12: Import Relationships (intra-project only) ──
        self.emit("%% ============================================================")
        self.emit("%% SECTION 12: INTRA-PROJECT IMPORTS")
        self.emit("%% ============================================================")
        self.ln()

        # Build module name map
        mod_to_path = {}
        for r, f in py_file_map.items():
            mod = str(r.with_suffix("")).replace("/", ".")
            mod_to_path[mod] = f
            # Also register parent modules
            parts = mod.split(".")
            for i in range(len(parts) - 1):
                parent = ".".join(parts[:i+1])
                if parent not in mod_to_path:
                    mod_to_path[parent] = f  # last file wins for __init__

        edge_set = set()
        for r, f in py_file_map.items():
            imports = get_imports(f)
            for imp in sorted(imports):
                if imp in IMPORT_EXCLUDE_PREFIXES:
                    continue
                if imp in mod_to_path:
                    target = mod_to_path[imp]
                    src_r = rel_path(f)
                    tgt_r = rel_path(target)
                    if src_r != tgt_r:
                        edge = (node(f), node(target))
                        if edge not in edge_set:
                            edge_set.add(edge)
                            short_label = imp.split(".")[-1]
                            self.emit(f"    {node(f)} -->|\"{short_label}\"| {node(target)}")

        self.ln()
        self.footer()

        return "\n".join(self.lines)


def verify(output_path):
    if not output_path.exists():
        print(f"{output_path} does not exist.")
        return False
    gen = ProjectMapGenerator()
    generated = gen.generate()
    current = output_path.read_text(encoding="utf-8")
    if generated.strip() == current.strip():
        print(f"{output_path} is up-to-date.")
        return True
    else:
        print(f"{output_path} is out-of-date. Regenerate with:")
        print(f"  python3 tools/generate_project_map.py")
        return False


def main():
    if "--check" in sys.argv:
        success = verify(OUTPUT_FILE)
        sys.exit(0 if success else 1)
    else:
        gen = ProjectMapGenerator()
        content = gen.generate()
        OUTPUT_FILE.write_text(content, encoding="utf-8")
        print(f"Generated {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
