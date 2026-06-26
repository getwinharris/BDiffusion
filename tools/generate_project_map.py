#!/usr/bin/env python3
"""
generate_project_map.py

Systematically scans the BDiffusion repository at the AST level and generates
project_map.mmd — a NotebookLM-style source graph for coding agents.

Maps:
  - Entry points, modules, directory structure
  - Deep class hierarchy (methods, bases, decorators, attributes)
  - Per-function internal call graph (which project functions call which)
  - Import dependency edges (file-level)
  - Duplicate / overlapping functions
  - Unused definitions (stale / dead code)
  - Data flow through the pipeline

Usage:
    python3 tools/generate_project_map.py          # generate project_map.mmd
    python3 tools/generate_project_map.py --check   # verify existing map is up-to-date
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "project_map.mmd"

EXCLUDE_DIRS = {".git", "__pycache__", "*.egg-info"}
INTERNAL_PACKAGES = {"utils", "data", "training", "benchmark", "benchmark_code",
                     "evaluation", "utils_logging",
                     "flow_llms", "pipeline", "tasks", "tools"}
IGNORE_MODULES = {"torch", "transformers", "numpy", "deepspeed",
                  "peft", "wandb", "configargparse", "omegaconf",
                  "datasets", "tqdm", "human_eval", "bigcode_eval"}
# Names too common to produce useful call graph edges
BROAD_CALL_NAMES = {"__init__", "__call__", "forward", "step", "run",
                    "train", "eval", "setup", "build", "load", "save",
                    "_build", "_init", "encode", "decode", "get", "set",
                    "update", "reset", "clear", "process", "handle",
                    "_forward", "initializer", "line2data", "prepare",
                    "configure", "register", "_register", "main"}


# ═══════════════════════════════════════════════════════════════
#  FILE I/O helpers
# ═══════════════════════════════════════════════════════════════

def read_file(path):
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def rel_path(path):
    return path.relative_to(REPO_ROOT)


def safe_id(name):
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_")


def node_id(path):
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


def mermaid_escape(text):
    res = text.replace("\\", "\\\\")
    res = res.replace('"', "'")
    res = res.replace("\n", "\\n")
    return res


# ═══════════════════════════════════════════════════════════════
#  DEEP AST ANALYSERS
# ═══════════════════════════════════════════════════════════════

def resolve_name(node):
    """Return a dotted name string from a Name / Attribute / Call node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = resolve_name(node.value)
        if prefix is None:
            return None
        return prefix + "." + node.attr
    if isinstance(node, ast.Call):
        return resolve_name(node.func)
    if isinstance(node, ast.Subscript):
        prefix = resolve_name(node.value)
        if prefix is None:
            return None
        return prefix + "[]"
    return None


class FunctionAnalyzer(ast.NodeVisitor):
    """Walk a function body and collect all call targets, attribute writes,
       local variable names, and references to `self`."""

    def __init__(self):
        self.calls = set()         # set of dotted names called
        self.self_attrs = set()    # attributes written on `self`
        self.locals = set()        # simple local variable names
        self.decorators = []       # decorator strings

    def visit_Call(self, node):
        try:
            name = resolve_name(node.func)
            if name:
                self.calls.add(name)
        except Exception:
            pass
        self.generic_visit(node)

    def visit_Assign(self, node):
        for t in node.targets:
            try:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    self.self_attrs.add(t.attr)
                elif isinstance(t, ast.Name):
                    self.locals.add(t.id)
            except Exception:
                pass
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name) and node.target.value.id == "self":
            self.self_attrs.add(node.target.attr)
        elif isinstance(node.target, ast.Name):
            self.locals.add(node.target.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        # Walk into nested function bodies to find actual calls
        self.generic_visit(node)


def analyze_function_body(func_node):
    """Return (calls, self_attrs, locals) for a single FunctionDef node."""
    fa = FunctionAnalyzer()
    fa.visit(func_node)
    return fa.calls, fa.self_attrs, fa.locals, func_node.lineno


def method_signature(func_node):
    """Return a signature string like (self, input_ids, labels=None)."""
    parts = []
    for arg in func_node.args.args:
        if arg.arg == "self":
            continue
        annotation = ""
        if arg.annotation:
            annotation = resolve_name(arg.annotation) or ""
        if annotation:
            parts.append(f"{arg.arg}: {annotation}")
        else:
            parts.append(arg.arg)
    # defaults
    n_required = len(func_node.args.args) - len(func_node.args.defaults)
    if func_node.args.args and func_node.args.args[0].arg == "self":
        n_required -= 1
    result = []
    for i, p in enumerate(parts):
        default_idx = i - n_required
        if default_idx >= 0 and default_idx < len(func_node.args.defaults):
            d = func_node.args.defaults[default_idx]
            d_str = resolve_name(d) or ast.dump(d)[:15]
            result.append(f"{p}={d_str}")
        else:
            result.append(p)
    return ", ".join(result)


def extract_deep_class_info(tree):
    """Parse a module tree and return deep class info."""
    classes = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [resolve_name(b) or "" for b in node.bases]
        bases = [b for b in bases if b]
        methods = []
        self_attrs = set()
        class_vars = {}
        all_inner_calls = set()
        for item in ast.iter_child_nodes(node):
            if isinstance(item, ast.FunctionDef):
                calls, attrs, locals_, lineno = analyze_function_body(item)
                sig = method_signature(item)
                decorators = [resolve_name(d) or "" for d in item.decorator_list if resolve_name(d)]
                methods.append({
                    "name": item.name,
                    "signature": sig,
                    "lineno": lineno,
                    "calls": calls,
                    "decorators": decorators,
                })
                self_attrs.update(attrs)
                all_inner_calls.update(calls)
            elif isinstance(item, ast.Assign):
                for t in item.targets:
                    if isinstance(t, ast.Name):
                        class_vars[t.id] = resolve_name(item.value) or ""
        classes.append({
            "name": node.name,
            "bases": bases,
            "lineno": node.lineno,
            "methods": methods,
            "self_attrs": sorted(self_attrs),
            "class_vars": class_vars,
            "all_calls": all_inner_calls,
        })
    return classes


def extract_top_functions(tree, filepath):
    """Return list of top-level function info dicts."""
    funcs = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef):
            calls, attrs, locals_, lineno = analyze_function_body(node)
            sig = method_signature(node)
            decorators = [resolve_name(d) or "" for d in node.decorator_list if resolve_name(d)]
            funcs.append({
                "name": node.name,
                "signature": sig,
                "lineno": lineno,
                "calls": calls,
                "decorators": decorators,
                "locals": locals_,
            })
    return funcs


def extract_file_imports(tree):
    """Return list of (module, names) import tuples."""
    imports = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name.split(".")[0], alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                names = [a.name for a in node.names]
                imports.append((root, node.module, names))
    return imports


def build_file_index(py_files):
    """Build a rich index of every Python file in the project.

    Returns:
        file_index: {Path -> {classes, functions, imports, ...}}
        name_to_location: {func_or_class_name -> [(Path, lineno, kind)]}
        call_graph: { (Path, func_name) -> set(dotted_call_targets) }
    """
    py_file_map = {rel_path(f): f for f in py_files}
    file_index = {}
    name_to_location = defaultdict(list)
    call_graph_raw = {}  # (path, func_name) -> set of call strings

    for r, f in sorted(py_file_map.items()):
        tree = parse_ast(f)
        if tree is None:
            file_index[f] = {"classes": [], "functions": [], "imports": []}
            continue
        classes = extract_deep_class_info(tree)
        functions = extract_top_functions(tree, f)
        imports = extract_file_imports(tree)

        file_index[f] = {
            "classes": classes,
            "functions": functions,
            "imports": imports,
            "tree": tree,
        }

        # Register class definitions
        for c in classes:
            name_to_location[c["name"]].append((f, c["lineno"], "class"))
            for m in c["methods"]:
                name_to_location[m["name"]].append((f, m["lineno"], "method", c["name"]))

        # Register top-level function definitions
        for fn in functions:
            name_to_location[fn["name"]].append((f, fn["lineno"], "function"))

        # Collect call graph
        for fn in functions:
            key = (f, fn["name"])
            call_graph_raw[key] = fn["calls"]
        for c in classes:
            for m in c["methods"]:
                key = (f, f"{c['name']}.{m['name']}")
                call_graph_raw[key] = m["calls"]

    return py_file_map, file_index, name_to_location, call_graph_raw


def resolve_call_targets(call_graph_raw, name_to_location, py_file_map):
    """Resolve call strings in call_graph_raw to defined locations.

    Returns:
        resolved: { (caller_path, caller_name) -> {(callee_path, callee_name, callee_kind)} }
    """
    resolved = {}
    for caller_key in sorted(call_graph_raw, key=lambda x: (str(x[0]), x[1])):
        caller_path, caller_name = caller_key
        raw_targets = call_graph_raw[caller_key]
        targets = []
        seen_targets = set()
        for target in sorted(raw_targets):
            name = target.split(".")[0]
            if name in name_to_location:
                for loc in sorted(name_to_location[name], key=lambda x: (str(x[0]), x[1], str(x[2]))):
                    tkey = (loc[0], target, loc[2])
                    if tkey not in seen_targets:
                        seen_targets.add(tkey)
                        targets.append(tkey)
        resolved[caller_key] = targets
    return resolved


def detect_unused(name_to_location, resolved_calls, py_files):
    """Find functions/classes defined but never referenced from other files.

    Returns a dict { (path, name) -> kind }
    """
    defined = set()
    for name, locs in name_to_location.items():
        for loc in locs:
            defined.add((loc[0], name, loc[2]))
    referenced = set()
    for caller_key, targets in resolved_calls.items():
        for t in targets:
            referenced.add((t[0], t[1], t[2]))
    # Also consider imports as references
    for f in py_files:
        tree = parse_ast(f)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".")[0]
                    if name in name_to_location:
                        for loc in name_to_location[name]:
                            referenced.add((loc[0], name, loc[2]))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for alias in node.names:
                        if alias.asname:
                            name = alias.asname
                        else:
                            name = alias.name
                        if name in name_to_location:
                            for loc in name_to_location[name]:
                                referenced.add((loc[0], name, loc[2]))
    unused = {}
    for d in defined:
        fpath, dname, kind = d
        # skip __init__, self, standard dunder methods, main
        if dname.startswith("__") or dname == "main" or dname == "__init__":
            continue
        # Check if referenced from a DIFFERENT file
        ref_in_other = False
        for r in referenced:
            if r[1] == dname and r[0] != fpath:
                ref_in_other = True
                break
        if not ref_in_other:
            # Check if defined and called only in the same file
            called_in_same = False
            for ck, ct in resolved_calls.items():
                if ck[0] == fpath:
                    for t in ct:
                        if t[1] == dname:
                            called_in_same = True
                            break
                if called_in_same:
                    break
            if not called_in_same:
                unused[d] = kind
    return unused


# ═══════════════════════════════════════════════════════════════
#  MERMAID GENERATOR
# ═══════════════════════════════════════════════════════════════

class MermaidBuilder:
    def __init__(self):
        self.lines = []
        self.max_label_len = 400
        self._classes: dict[str, str] = {}

    def emit(self, text=""):
        self.lines.append(text)

    def ln(self):
        self.lines.append("")

    def truncate(self, text, maxlen=None):
        maxlen = maxlen or self.max_label_len
        if len(text) > maxlen:
            cut = text[:maxlen-3]
            # truncate at last space to avoid cutting words
            last_space = cut.rfind(" ")
            if last_space > maxlen // 2:
                cut = cut[:last_space]
            return cut + "..."
        return text

    def tag_class(self, nid: str, cls: str) -> None:
        self._classes[nid] = cls

    def header(self):
        self.emit("```mermaid")
        self.emit("flowchart TD")
        self.ln()
        self.emit("%% ============================================================")
        self.emit("%% BDiffusion Knowledge Graph (AST-generated)")
        self.emit("%% ============================================================")
        self.ln()
        self.emit("%% Styles")
        for cls, color in [("green", "#e8f5e9:#2e7d32"), ("blue", "#e3f2fd:#1565c0"),
                           ("orange", "#fff3e0:#e65100"), ("red", "#ffebee:#c62828"),
                           ("purple", "#f3e5f5:#6a1b9a"), ("teal", "#e0f2f1:#004d40"),
                           ("amber", "#fff8e1:#f9a825"), ("pink", "#fce4ec:#880e4f")]:
            bg, br = color.split(":")
            self.emit(f"    classDef {cls} fill:{bg},stroke:{br}")
        self.ln()

    def footer(self):
        self.ln()
        for nid in sorted(self._classes):
            self.emit(f"    class {nid} {self._classes[nid]}")
        self.ln()
        self.emit("%% end of project_map.mmd")
        self.emit("```")

    def section(self, num, title):
        self.emit("%% " + "=" * 60)
        self.emit(f"%% SECTION {num}: {title}")
        self.emit("%% " + "=" * 60)
        self.ln()

    def subgraph(self, title, nids):
        tag = safe_id(title[:40].lower())
        self.emit(f"    subgraph {tag} [{title}]")
        for n in nids:
            self.emit(f"        {n}")
        self.emit("    end")
        self.ln()

    def style(self, nid, cls):
        self.emit(f"    style {nid} {cls}")

    def emit_node(self, nid: str, label: str, cls: str | None = None):
        self.emit(f"        {nid}[\"{self.truncate(label)}\"]")
        if cls:
            self.tag_class(nid, cls)

    def edge(self, src, dst, label="", style="-->"):
        if label:
            self.emit(f"    {src} {style}|\"{mermaid_escape(label)}\"| {dst}")
        else:
            self.emit(f"    {src} {style} {dst}")


# ═══════════════════════════════════════════════════════════════
#  MAIN GENERATOR
# ═══════════════════════════════════════════════════════════════

def generate():
    py_files = collect(REPO_ROOT, "*.py")
    sh_files = collect(REPO_ROOT, "*.sh")
    cfg_files = collect(REPO_ROOT, "*.cfg")
    agents_files = collect(REPO_ROOT, "AGENTS.md")

    py_file_map, file_index, name_to_location, call_graph_raw = build_file_index(py_files)
    resolved_calls = resolve_call_targets(call_graph_raw, name_to_location, py_file_map)
    unused = detect_unused(name_to_location, resolved_calls, py_files)

    mb = MermaidBuilder()
    mb.header()

    # Global store for node IDs emitted as standalone (not inside a subgraph)
    # so SECTION 3+ can reference them.
    all_nodes = {}  # Path -> node_id

    # ── SECTION 1: Entry points & module architecture ──
    mb.section(1, "ENTRY POINTS & MODULE ARCHITECTURE")

    # Layout all root *.py files as entry points
    entry_nodes = []
    for r in sorted(py_file_map):
        if r.parent != Path("."):
            continue
        f = py_file_map[r]
        nid = node_id(f)
        all_nodes[f] = nid
        classes = file_index[f]["classes"]
        funcs = file_index[f]["functions"]
        parts = [f.name]
        if classes:
            clist = ", ".join(c["name"] for c in classes)
            parts.append("[" + mb.truncate(clist, 55) + "]")
        if funcs:
            flist = ", ".join(fn["name"] for fn in funcs)
            parts.append("(" + mb.truncate(flist, 55) + ")")
        joined_parts = "\\n".join(parts)
        mb.emit(f"        {nid}[\"{mb.truncate(joined_parts)}\"]")
        entry_nodes.append(nid)
    mb.subgraph("Entry Points", entry_nodes)

    # Submodule groups
    subdirs = [
        ("flow_llms", "Flow-Augmented LLMs"),
        ("pipeline", "Training / Inference Pipeline"),
        ("tasks", "Task Processors & Evaluators"),
        ("cfgs", "Configuration Files"),
        ("scripts", "Shell Launchers"),
        ("tools", "Developer Tools"),
        ("figures", "Assets"),
    ]
    for sub, title in subdirs:
        nids = []
        for r in sorted(py_file_map):
            if not str(r).startswith(sub + "/"):
                continue
            f = py_file_map[r]
            nid = node_id(f)
            all_nodes[f] = nid
            classes = file_index[f]["classes"]
            funcs = file_index[f]["functions"]
            parts = [r.name]
            if classes:
                clist = ", ".join(c["name"] for c in classes)
                parts.append("[" + mb.truncate(clist, 60) + "]")
            if funcs:
                flist = ", ".join(fn["name"] for fn in funcs[:20])
                if len(funcs) > 20:
                    flist += "..."
                parts.append("(" + flist + ")")
            joined = "\\n".join(parts)
            mb.emit(f"        {nid}[\"{mb.truncate(joined)}\"]")
            nids.append(nid)
        cfg_nids = []
        for r in sorted({rel_path(f): f for f in cfg_files}):
            if str(r).startswith(sub):
                f = {rel_path(f): f for f in cfg_files}[r]
                nid = node_id(f)
                all_nodes[f] = nid
                src = read_file(f)
                sections = sorted(set(re.findall(r"\[(\w+)\]", src)))
                label = "/".join(r.parts[1:]) if len(r.parts) > 1 else r.name
                if sections:
                    label += f"\\n({', '.join(sections)})"
                mb.emit(f"        {nid}[\"{label}\"]")
                cfg_nids.append(nid)
        # script files
        scr_nids = []
        for r in sorted({rel_path(f): f for f in sh_files}):
            if str(r).startswith(sub):
                f = {rel_path(f): f for f in sh_files}[r]
                nid = node_id(f)
                all_nodes[f] = nid
                mb.emit(f"        {nid}[\"{r.name}\"]")
                scr_nids.append(nid)
        all_nids = nids + cfg_nids + scr_nids
        if all_nids:
            mb.subgraph(title, all_nids)

    # AGENTS.md files
    agent_nids = []
    for f in agents_files:
        nid = node_id(f)
        all_nodes[f] = nid
        mb.emit(f"        {nid}[\"{rel_path(f)}\"]")
        agent_nids.append(nid)
    if agent_nids:
        mb.subgraph("DOCS-INDEX Hierarchy", agent_nids)

    # ── SECTION 2: Deep Class Hierarchy ──
    mb.section(2, "CLASS HIERARCHY & INHERITANCE")

    # Collect all classes with their full inheritance chain
    class_index = {}  # class_name -> (file, class_info)
    for f in py_files:
        for c in file_index[f]["classes"]:
            class_index[c["name"]] = (f, c)

    emitted_inheritance = set()
    for cname, (cf, cinfo) in sorted(class_index.items()):
        if not cinfo["bases"]:
            continue
        src_nid = node_id(cf)
        for b in cinfo["bases"]:
            if b in class_index:
                bf, _ = class_index[b]
                tgt_nid = node_id(bf)
                edge = (src_nid, tgt_nid)
                if edge not in emitted_inheritance:
                    mb.edge(src_nid, tgt_nid, b)
                    emitted_inheritance.add(edge)
            else:
                # external base class like torch.nn.Module — skip
                pass

    # Per-class method detail
    for cname, (cf, cinfo) in sorted(class_index.items()):
        if len(cinfo["methods"]) == 0:
            continue
        nid = node_id(cf)
        for m in cinfo["methods"][:20]:  # limit per class
            sig = m["signature"]
            decorators = m["decorators"]
            label = f"{cname}.{m['name']}({sig})"
            if decorators:
                label += f" [@{', '.join(decorators)}]"
            mnid = f"{nid}_{safe_id(cname)}_{safe_id(m['name'])}"
            mb.emit(f"        {mnid}[\"{mb.truncate(label)}\"]")
            mb.edge(nid, mnid, m["name"], style="-.->")
            # Connect method to functions it calls
            for call_target in sorted(m["calls"]):
                segments = call_target.split(".")
                if any(s in BROAD_CALL_NAMES for s in segments):
                    continue
                if segments[0] in name_to_location:
                    for loc in name_to_location[segments[0]]:
                        if loc[0] != cf:  # external call
                            tnid = node_id(loc[0])
                            mb.edge(mnid, tnid, call_target[:20], style="-.->")
                        break
        if len(cinfo["methods"]) > 20:
            mb.emit(f"        {nid}_{cname}_more[\"+{len(cinfo['methods'])-20} more methods\"]")
            mb.edge(nid, f"{nid}_{cname}_more", "...", style="-.->")

    # Class-level attributes
    for cname, (cf, cinfo) in sorted(class_index.items()):
        if not cinfo["self_attrs"]:
            continue
        nid = node_id(cf)
        attrs_str = ", ".join(cinfo["self_attrs"][:10])
        if len(cinfo["self_attrs"]) > 10:
            attrs_str += "..."
        anid = f"{nid}_{safe_id(cname)}_attrs"
        mb.emit(f"        {anid}[\"{cname} self attrs: {attrs_str}\"]")
        mb.tag_class(anid, "teal")
        mb.edge(nid, anid, "manages", style="-.->")

    # ── SECTION 3: Top-level function call graph ──
    mb.section(3, "FUNCTION CALL GRAPH")

    call_edges = set()
    for caller_key, targets in resolved_calls.items():
        caller_path, caller_name = caller_key
        if caller_path not in all_nodes:
            continue
        src_nid = all_nodes[caller_path]
        for t in targets:
            callee_path, callee_name, kind = t
            # Skip overly broad names
            callee_segments = callee_name.split(".")
            if any(s in BROAD_CALL_NAMES for s in callee_segments):
                continue
            if callee_path not in all_nodes:
                continue
            tgt_nid = all_nodes[callee_path]
            if src_nid == tgt_nid and caller_name == callee_name:
                continue  # self-call, skip
            edge = (src_nid, tgt_nid, caller_name[:20])
            if edge not in call_edges:
                call_edges.add(edge)
                label = callee_name.split(".")[-1]
                mb.edge(src_nid, tgt_nid, label)

    # ── SECTION 4: Internal call chains (method-level) ──
    mb.section(4, "KEY INTERNAL CALL CHAINS")

    # flow_llms call chain
    llama_f = py_file_map.get(Path("flow_llms") / "llama.py")
    qwen2_f = py_file_map.get(Path("flow_llms") / "qwen2.py")
    base_f = py_file_map.get(Path("flow_llms") / "base.py")
    base_comp_f = py_file_map.get(Path("flow_llms") / "base_components.py")
    flow_utils_f = py_file_map.get(Path("flow_llms") / "utils.py")

    # Model architecture chain: forward -> model -> decoder_layer
    mb.emit(f"    nstep_node[nstep_inference\\nODE integration loop]")
    mb.tag_class("nstep_node", "blue")
    mb.emit(f"    vel_node[compute_velocity\\nx1_pred->flow_vel]")
    mb.tag_class("vel_node", "blue")
    mb.emit(f"    odeint_node[odeint\\nfloat64 integration]")
    mb.tag_class("odeint_node", "blue")
    mb.emit(f"    ts_node[timestep scheduling\\nnoise schedule + flow interpolation]")
    mb.tag_class("ts_node", "blue")
    for mod_f in [llama_f, qwen2_f]:
        if mod_f is None:
            continue
        mid = node_id(mod_f)
        mb.emit(f"    {mid} -->|\"nstep_inference()\"| nstep_node")
        mb.emit(f"    nstep_node -->|\"compute_velocity()\"| vel_node")
        mb.emit(f"    nstep_node -->|\"torchdiffeq.odeint()\"| odeint_node")
        mb.emit(f"    vel_node -->|\"forward()\"| {mid}")
        mb.emit(f"    {mid} -->|\"sample_timestep()\"| ts_node")

    # Training flow chain
    train_f = py_file_map.get(Path("training.py"))
    pipe_train_f = py_file_map.get(Path("pipeline") / "training.py")
    if train_f and pipe_train_f:
        t_nid = node_id(train_f)
        pt_nid = node_id(pipe_train_f)
        data_f = py_file_map.get(Path("data.py"))
        d_nid = node_id(data_f) if data_f else None
        mb.emit(f"    {t_nid} -->|\"main() → TrainingPipeline.run()\"| {pt_nid}")
        data_flow_id = "data_flow_node"
        mb.emit(f"    {data_flow_id}[Data Loading\\nHF datasets → processor → DataLoader]")
        mb.tag_class(data_flow_id, "green")
        if d_nid:
            mb.edge(data_flow_id, d_nid, "load_conversation_data_from_hf")
        mb.emit(f"    train_loop[Training Loop\\naccum steps → backward → step]")
        mb.tag_class("train_loop", "green")
        mb.emit(f"    {pt_nid} -->|\"train loop\"| train_loop")
        mb.emit(f"    tfs[train_forward_step\\nmodel.forward() → CE loss]")
        mb.tag_class("tfs", "green")
        mb.emit(f"    train_loop -->|\"train_forward_step()\"| tfs")
        mb.emit(f"    vfs[valid_forward_step\\nforward + nstep eval]")
        mb.tag_class("vfs", "green")
        mb.emit(f"    train_loop -->|\"valid_forward_step()\"| vfs")

    # Inference flow chain
    bench_f = py_file_map.get(Path("benchmark.py"))
    pipe_inf_f = py_file_map.get(Path("pipeline") / "inference.py")
    dec_f = py_file_map.get(Path("pipeline") / "decoding_util.py")
    if bench_f and pipe_inf_f:
        b_nid = node_id(bench_f)
        pi_nid = node_id(pipe_inf_f)
        mb.emit(f"    {b_nid} -->|\"main() → InferencePipeline.run()\"| {pi_nid}")
        if dec_f:
            d_nid = node_id(dec_f)
            mb.emit(f"    {pi_nid} -->|\"forward_step_fn → autoregressive_decode()\"| {d_nid}")
            mb.emit(f"    lad_token[LAD Decode Loop\\nprefill → nstep_inference → sample]")
            mb.tag_class("lad_token", "green")
            mb.emit(f"    {d_nid} -->|\"per-token LAD\"| lad_token")

    # ── SECTION 5: Data flow pipeline ──
    mb.section(5, "DATA FLOW")

    data_f = py_file_map.get(Path("data.py"))
    pipe_data_f = py_file_map.get(Path("pipeline") / "data.py")
    tasks_base_f = py_file_map.get(Path("tasks") / "base.py")
    tasks_utils_f = py_file_map.get(Path("tasks") / "utils.py")
    tasks_templates_f = py_file_map.get(Path("tasks") / "templates.py")
    tasks_constants_f = py_file_map.get(Path("tasks") / "constants.py")

    hf_node = "hf_ds[HuggingFace Dataset\\nload_dataset()]"
    mb.emit(f"    {hf_node}")
    if data_f:
        mb.edge(hf_node, node_id(data_f), "load_conversation_data_from_hf")
    if pipe_data_f:
        mb.edge(hf_node if data_f else hf_node, node_id(pipe_data_f), "load_data_from_jsonl (unused)")

    # Task processors
    task_data_processors = []
    task_eval_processors = []
    for r, f in sorted(py_file_map.items()):
        if not str(r).startswith("tasks/") or r.name in ("__init__.py", "base.py", "utils.py", "constants.py", "templates.py"):
            continue
        classes = file_index[f]["classes"]
        dps = [c["name"] for c in classes if "DataProcessor" in c["name"] or c["name"].endswith("Processor")]
        eps = [c["name"] for c in classes if c["name"].endswith("EvalProcessor")]
        if dps:
            task_data_processors.append((f, dps))
        if eps:
            task_eval_processors.append((f, eps))

    if tasks_base_f:
        mb.emit(f"    data_abc[{tasks_base_f.name} Task ABC]")
        mb.tag_class("data_abc", "teal")
        for f, dps in task_data_processors:
            mb.edge(node_id(f), node_id(tasks_base_f), "inherits Task", style="-.->")
    if tasks_utils_f:
        mb.emit(f"    task_utils[{tasks_utils_f.name}]")
        mb.tag_class("task_utils", "teal")
        for f, _ in task_data_processors:
            mb.edge(node_id(f), node_id(tasks_utils_f), "uses answer extraction")
    if tasks_constants_f:
        const_nid = "n_tasks_constants_data_flow"
        mb.emit(f"    {const_nid}[tasks/constants.py\\nSharedTasks categories]")
        mb.tag_class(const_nid, "amber")
        for f, _ in task_data_processors:
            mb.edge(node_id(f), const_nid, "uses categories")

    # Task DP -> Eval connections
    for f, eps in task_eval_processors:
        dp_name = eps[0].replace("EvalProcessor", "DataProcessor")
        for f2, dps in task_data_processors:
            if any(dp_name in d for d in dps):
                lb = f"{dps[0]} → {eps[0]}"
                mb.edge(node_id(f2), node_id(f), lb)
    # Flag files with DataProcessor but no EvalProcessor
    ep_files = {f for f, _ in task_eval_processors}
    for f, dps in task_data_processors:
        if f not in ep_files:
            nid = f"no_ep_{safe_id(str(rel_path(f)))}"
            mb.emit(f"    {nid}[\"NO EVAL: {rel_path(f)}: {dps[0]} has no matching EvalProcessor\"]")
            mb.tag_class(nid, "red")

    # ── SECTION 6: Duplicate functions (cross-file) ──
    mb.section(6, "DUPLICATE FUNCTIONS")

    # Group top-level function names that appear in multiple files
    func_occurrences = defaultdict(list)
    for f in py_files:
        for fn in file_index[f]["functions"]:
            func_occurrences[fn["name"]].append((f, fn["lineno"]))
    for name, locs in sorted(func_occurrences.items()):
        if len(locs) < 2 or name in BROAD_CALL_NAMES:
            continue
        dup_tag = safe_id(f"dup_{name}")
        mb.emit(f"    subgraph {dup_tag} [\"Duplicate: {name} ({len(locs)} locations)\"]")
        for lpath, lineno in locs:
            lnid = f"{safe_id(str(lpath.relative_to(REPO_ROOT)))}_{lineno}"
            loc_label = f"{lpath.relative_to(REPO_ROOT)}:{lineno}"
            mb.emit(f"        {lnid}[\"{loc_label}\"]")
        mb.emit("    end")
        mb.ln()

    # Method duplicates (same method name in different classes)
    method_occurrences = defaultdict(list)
    for f in py_files:
        for c in file_index[f]["classes"]:
            for m in c["methods"]:
                method_occurrences[m["name"]].append((f, c["name"], m["lineno"]))
    for name, locs in sorted(method_occurrences.items()):
        if len(locs) < 2:
            continue
        dup_tag = safe_id(f"dup_method_{name}")
        if name in BROAD_CALL_NAMES:
            continue
        mb.emit(f"    subgraph {dup_tag} [\"Duplicate method: {name} ({len(locs)} locations)\"]")
        for lpath, cname, lineno in locs:
            lnid = f"{safe_id(str(lpath.relative_to(REPO_ROOT)))}_{cname}_{lineno}"
            loc_label = f"{lpath.relative_to(REPO_ROOT)}:{cname}.{name}() L{lineno}"
            mb.emit(f"        {lnid}[\"{loc_label}\"]")
        mb.emit("    end")
        mb.ln()

    # ── SECTION 7: Unused / stale definitions ──
    mb.section(7, "UNUSED DEFINITIONS & STALE CODE")

    for (fpath, dname, kind) in sorted(unused, key=lambda x: (str(x[0]), x[1], str(x[2]))):
        # skip if it's only used within its own file
        in_file = name_to_location.get(dname, [])
        only_in_same = all(l[0] == fpath for l in in_file)
        called_in_file = False
        for ck, ct in resolved_calls.items():
            if ck[0] == fpath:
                for t in ct:
                    if t[1] == dname:
                        called_in_file = True
                        break
            if called_in_file:
                break
        if only_in_same and called_in_file:
            continue  # U: defined+called in same file, not externally used
        label = f"{rel_path(fpath)}: {dname} ({kind})"
        unused_nid = f"unused_{safe_id(str(rel_path(fpath)))}_{safe_id(dname)}"
        mb.emit(f"    {unused_nid}[\"UNUSED: {label}\"]")
        mb.tag_class(unused_nid, "red")

    # Stale NotImplementedError areas + TODOs
    stale_keywords = ["raise NotImplementedError", "# TODO", "# FIXME"]
    for f in py_files:
        if f.name == "generate_project_map.py":
            continue
        src = read_file(f)
        for kw in stale_keywords:
            for i, line in enumerate(src.splitlines(), 1):
                if kw in line:
                    stale_nid = f"stale_{safe_id(str(rel_path(f)))}_{i}"
                    mb.emit(f"    {stale_nid}[\"STALE: {rel_path(f)}:{i} {kw}\"]")
                    mb.tag_class(stale_nid, "red")
                    break

    # ── SECTION 8: Missing expected artifacts ──
    mb.section(8, "MISSING ARTIFACTS")

    missing_items = [
        ("VERSION", "Version file"),
        ("CHANGELOG.md", "Changelog"),
    ]
    for fname, desc in missing_items:
        if not (REPO_ROOT / fname).exists():
            nid = f"missing_{safe_id(fname)}"
            mb.emit(f"    {nid}[\"MISSING: {fname} ({desc})\"]")
            mb.tag_class(nid, "red")

    # Unused / orphaned modules / classes
    orphan_items = [
        ("utils_logging.py", "Metrics class", "Unused; pipeline/util.py StatisticsTracker used instead"),
        ("pipeline/data.py", "load_data_from_jsonl", "Unused; data.py:load_conversation_data_from_hf is active"),
        ("pipeline/data.py", "DataReformatter", "ABC with zero concrete implementations"),
        ("pipeline/data.py", "IdentityDataProcessor", "Simple pass-through, test-only"),
        ("utils.py", "estimate_nstep_cross_entropy", "Raises NotImplementedError (stub)"),
    ]
    for fname, item, note in orphan_items:
        mid = f"orphan_{safe_id(fname)}_{safe_id(item)}"
        mb.emit(f"    {mid}[\"ORPHANED: {fname}: {item}\\n{note}\"]")
        mb.tag_class(mid, "amber")

    # ── SECTION 9: Shell script launcher mapping ──
    mb.section(9, "SHELL SCRIPT LAUNCHERS")

    sh_entry_map = {
        "run_training.sh": "training.py",
        "run_bench_tasks.sh": "benchmark.py",
        "run_bench_code.sh": "benchmark_code.py",
    }
    for sh_name, py_name in sh_entry_map.items():
        sh_f = None
        for r in sorted({rel_path(f): f for f in sh_files}):
            if r.name == sh_name:
                sh_f = {rel_path(f): f for f in sh_files}[r]
                break
        if sh_f is None:
            continue
        py_f = py_file_map.get(Path(py_name))
        if py_f:
            mb.edge(node_id(sh_f), node_id(py_f), "deepspeed")

    # Meta-scripts that call multiple
    for sh_name in ["run_bench.sh", "run_bench_full.sh"]:
        sh_f = None
        for r in sorted({rel_path(f): f for f in sh_files}):
            if r.name == sh_name:
                sh_f = {rel_path(f): f for f in sh_files}[r]
                break
        if sh_f is None:
            continue
        content = read_file(sh_f)
        for py_candidate in ["benchmark.py", "benchmark_code.py"]:
            if py_candidate in content:
                py_f = py_file_map.get(Path(py_candidate))
                if py_f:
                    mb.edge(node_id(sh_f), node_id(py_f), "calls", style="-.-")

    # ── SECTION 10: Cross-file import edges ──
    mb.section(10, "INTRA-PROJECT IMPORT DEPENDENCIES")

    mod_to_path = {}
    for r, f in py_file_map.items():
        mod = str(r.with_suffix("")).replace("/", ".")
        mod_to_path[mod] = f
        parts = mod.split(".")
        for i in range(len(parts) - 1):
            parent = ".".join(parts[:i + 1])
            if parent not in mod_to_path:
                mod_to_path[parent] = f

    seen_edges = set()
    for r, f in sorted(py_file_map.items()):
        imports = file_index[f]["imports"]
        for imp in imports:
            root = imp[0]
            if root in IGNORE_MODULES:
                continue
            if root in mod_to_path:
                tgt = mod_to_path[root]
                s_nid = node_id(f)
                t_nid = node_id(tgt)
                if (s_nid, t_nid) not in seen_edges:
                    seen_edges.add((s_nid, t_nid))
                    mb.edge(s_nid, t_nid, imp[0])

    mb.footer()
    return "\n".join(mb.lines)


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def verify() -> bool:
    if not OUTPUT_FILE.exists():
        print(f"{OUTPUT_FILE} does not exist.", file=sys.stderr)
        return False
    current = OUTPUT_FILE.read_text(encoding="utf-8")
    fresh = generate()
    if current.strip() == fresh.strip():
        print(f"{OUTPUT_FILE} is up-to-date.")
        return True
    print(f"{OUTPUT_FILE} is out-of-date. Regenerate with:", file=sys.stderr)
    print("  python3 tools/generate_project_map.py", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if project_map.mmd is stale")
    args = parser.parse_args()
    if args.check:
        return 0 if verify() else 1
    content = generate()
    OUTPUT_FILE.write_text(content, encoding="utf-8")
    print(f"Generated {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
