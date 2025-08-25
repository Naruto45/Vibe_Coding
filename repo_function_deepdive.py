#!/usr/bin/env python3
# repo_function_deepdive.py
# Analyze functions per file, build related-function groups, and generate ~3000-word Markdown deep dives via OpenAI.

import os, re, ast, sys, json, time, math, argparse, textwrap, traceback
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Iterable, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== CLI ==========
def parse_args():
    p = argparse.ArgumentParser(description="Function-level deep-dive with OpenAI per related group.")
    p.add_argument("--root", required=True, help="Path to a single repository root to analyze.")
    p.add_argument("--out", default="./deep_reports2", help="Output directory for per-file group markdown analyses.")
    p.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                   help="OpenAI model name (default: gpt-4o). Override with --model or OPENAI_MODEL env.")
    p.add_argument("--max-workers", type=int, default=3, help="Max concurrent OpenAI calls.")
    p.add_argument("--max-file-bytes", type=int, default=600_000, help="Skip files larger than this many bytes.")
    p.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks when walking.")
    p.add_argument("--dry-run", action="store_true", help="Parse & group only; do not call OpenAI.")
    p.add_argument("--include", nargs="*", default=[".py", ".js", ".ts", ".tsx", ".jsx"], help="File extensions to include.")
    p.add_argument("--exclude-dirs", nargs="*", default=[".git","node_modules","venv",".venv","build","dist","target","Pods","DerivedData","__pycache__","coverage",".mypy_cache",".pytest_cache",".idea",".vscode"],
                   help="Directories to skip.")
    p.add_argument("--max-snippet-chars", type=int, default=14_000, help="Max chars of code to embed per group (keeps prompt safe).")
    p.add_argument("--group-word-target", type=int, default=3000, help="Target words per group report.")
    p.add_argument("--rate-limit-sleep", type=float, default=1.0, help="Seconds to sleep between API calls (simple throttle).")
    return p.parse_args()

# ========== Utilities ==========
def read_text(path: Path, max_bytes: int) -> Optional[str]:
    try:
        if path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
        for enc in ("utf-8","utf-16","latin-1"):
            try:
                return data.decode(enc)
            except Exception:
                continue
    except Exception:
        return None
    return None

def human_join(strings: List[str], sep=", ", last_sep=" and "):
    if not strings: return ""
    if len(strings) == 1: return strings[0]
    return sep.join(strings[:-1]) + last_sep + strings[-1]

# ========== PYTHON PARSER ==========
class PyFunction:
    def __init__(self, name: str, start: int, end: int, src: str, calls: Set[str]):
        self.name = name
        self.start = start
        self.end = end
        self.src = src
        self.calls = set(calls)

def parse_python_functions(code: str) -> Dict[str, PyFunction]:
    """Return {func_name: PyFunction} using AST; include list of function calls found inside."""
    funcs: Dict[str, PyFunction] = {}
    try:
        tree = ast.parse(code)
    except Exception:
        return funcs

    # Collect functions with spans
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            # line numbers are 1-based; end_lineno exists in 3.8+
            start = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", start)
            # Grab source segment (fallback to slicing by lines)
            try:
                segment = ast.get_source_segment(code, node)
            except Exception:
                segment = "\n".join(code.splitlines()[start-1:end])
            funcs[name] = PyFunction(name, start, end, segment or "", set())

    # Build a simple call list by scanning Name/Attribute nodes inside each func body
    class CallVisitor(ast.NodeVisitor):
        def __init__(self, current: str):
            self.current = current
            self.calls: Set[str] = set()
        def visit_Call(self, node: ast.Call):
            # cases: foo(), obj.foo()
            fn = node.func
            if isinstance(fn, ast.Name):
                self.calls.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                # record attribute .attr (best-effort)
                self.calls.add(fn.attr)
            self.generic_visit(node)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cur = node.name
            v = CallVisitor(cur)
            v.visit(node)
            if cur in funcs:
                funcs[cur].calls |= v.calls

    return funcs

# ========== JS/TS PARSER ==========
JS_FUNC_PATTERNS = [
    # function foo(...)
    (re.compile(r"\bfunction\s+([A-Za-z0-9_]+)\s*\(", re.MULTILINE), "decl"),
    # const foo = (...) => { ... } OR function expression assigned
    (re.compile(r"\b(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*\([^\)]*\)\s*=>\s*{", re.MULTILINE), "arrow"),
    (re.compile(r"\b(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*function\s*\(", re.MULTILINE), "expr"),
    # class methods (simple)
    (re.compile(r"\b([A-Za-z0-9_]+)\s*\([^)]*\)\s*{", re.MULTILINE), "method_hint"),  # filtered later
]

JS_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)

class JsFunction:
    def __init__(self, name: str, start_idx: int, end_idx: int, start_line: int, end_line: int, src: str, calls: Set[str]):
        self.name = name
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.start_line = start_line
        self.end_line = end_line
        self.src = src
        self.calls = set(calls)

def _brace_span(code: str, start_brace_idx: int) -> int:
    """Return index of matching closing brace from start_brace_idx; -1 if not found."""
    depth = 0
    i = start_brace_idx
    in_str = None
    esc = False
    while i < len(code):
        ch = code[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"', "`"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1

def _line_no(code: str, idx: int) -> int:
    return code.count("\n", 0, idx) + 1

def parse_js_functions(code: str) -> Dict[str, JsFunction]:
    seen: Dict[str, JsFunction] = {}
    for rx, kind in JS_FUNC_PATTERNS:
        for m in rx.finditer(code):
            name = m.group(1)
            if not name:
                continue
            # find the opening brace following this match
            brace_idx = code.find("{", m.end())
            if brace_idx == -1:
                continue
            end_idx = _brace_span(code, brace_idx)
            if end_idx == -1:
                continue
            src = code[m.start():end_idx+1]
            jf = JsFunction(
                name=name,
                start_idx=m.start(),
                end_idx=end_idx+1,
                start_line=_line_no(code, m.start()),
                end_line=_line_no(code, end_idx+1),
                src=src,
                calls=set()
            )
            # collect calls inside the function body
            body = code[brace_idx:end_idx+1]
            calls = set()
            for cm in JS_CALL_RE.finditer(body):
                callee = cm.group(1)
                if callee not in {"if","for","while","switch","return","function"}:
                    calls.add(callee)
            jf.calls = calls
            # avoid overwriting same name with a shorter span
            if name not in seen or (seen[name].end_idx - seen[name].start_idx) < (jf.end_idx - jf.start_idx):
                seen[name] = jf
    return seen

# ========== Grouping (Connected Components) ==========
def build_groups(names: List[str], edges: Dict[str, Set[str]]) -> List[Set[str]]:
    """Undirected connected components: names are nodes, edges are calls between known names."""
    graph: Dict[str, Set[str]] = {n:set() for n in names}
    for a, neigh in edges.items():
        for b in neigh:
            if b in graph and b != a:
                graph[a].add(b)
                graph[b].add(a)
    seen: Set[str] = set()
    groups: List[Set[str]] = []
    for n in names:
        if n in seen: continue
        stack = [n]
        comp = set()
        while stack:
            cur = stack.pop()
            if cur in seen: continue
            seen.add(cur)
            comp.add(cur)
            stack.extend(sorted(graph[cur] - seen))
        groups.append(comp)
    # Sort largest first
    groups.sort(key=lambda g: (-len(g), sorted(g)[0] if g else ""))
    return groups

# ========== Repo Walk ==========
def list_code_files(root: Path, include_exts: List[str], exclude_dirs: List[str], follow_symlinks: bool) -> List[Path]:
    files: List[Path] = []
    for r, dirs, fnames in os.walk(root, followlinks=follow_symlinks):
        # prune
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in fnames:
            p = Path(r) / f
            if p.suffix.lower() in include_exts:
                files.append(p)
    return files

# ========== OpenAI Client ==========
def get_openai_client():
    """
    Works with the official 'openai' Python SDK v1+.
    pip install --upgrade openai
    """
    try:
        from openai import OpenAI
    except Exception as e:
        print("[error] You need the official 'openai' package: pip install --upgrade openai", file=sys.stderr)
        raise
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY env var not set.")
    return OpenAI(api_key=api_key)

def call_openai(client, model: str, sys_prompt: str, user_prompt: str, retries: int=3) -> str:
    last_err = None
    for i in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role":"system","content":sys_prompt},
                    {"role":"user","content":user_prompt},
                ],
                temperature=0.2,
            )
            return resp.choices[0].message.content
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i+1))
    raise last_err

# ========== Report Prompt ==========
def make_group_prompt(repo_root: Path,
                      file_path: Path,
                      language: str,
                      imports: List[str],
                      group_funcs: List[Tuple[str, str]],  # (name, src)
                      callers: Dict[str, Set[str]],
                      callees: Dict[str, Set[str]],
                      target_words: int) -> Tuple[str,str]:
    sys_prompt = (
        "You are a senior staff software engineer and code analyst. "
        "Write clear, deeply technical, and precise explanations."
    )

    # Trim combined snippets to keep prompt reasonable
    total_chars = sum(len(src) for _, src in group_funcs)
    group_funcs_sorted = sorted(group_funcs, key=lambda x: (-len(x[1]), x[0]))

    snippets: List[Tuple[str,str]] = []
    cap = 0
    for name, src in group_funcs_sorted:
        if cap + len(src) > 14000:  # extra safety guard outside CLI
            continue
        snippets.append((name, src))
        cap += len(src)

    imports_block = ""
    if imports:
        imports_block = "Imports detected: " + ", ".join(sorted(set(imports))) + "\n"

    # Describe relationships
    rel_lines = []
    for name, _ in group_funcs:
        cs = sorted(callees.get(name, []))
        rs = sorted(callers.get(name, []))
        rel_lines.append(f"- {name}: calls [{', '.join(cs) if cs else '—'}]; called by [{', '.join(rs) if rs else '—'}]")

    user_prompt = f"""
Analyze the following {language} code from a single repository: **{file_path.relative_to(repo_root)}**.

{imports_block}
Function relationships (per this file / group):
{chr(10).join(rel_lines)}

Your task:
- Produce a ~{target_words} word **Markdown** report for this group of related functions.
- Explain what each function does, parameters, return values, side effects, state & I/O, and error handling.
- Explain how the functions interact and the overall data/control flow.
- Identify edge cases, failure modes, race conditions, security issues (authz, injection, CSRF, XSS), and performance risks.
- Suggest concrete refactors, improved abstractions, and unit/integration tests.
- If the functions touch Flask/HTTP, document the endpoints, request/response schema and status codes.
- If the code uses databases (Mongo/SQL), describe schema assumptions and transactional considerations.
- Provide a short example (pseudo-code) showing the typical execution path across these functions.

Provide an honest technical critique—if something is unclear or risky, say so and propose fixes.

### Code Snippets
{"".join([f"\n#### Function `{name}`\n```{language}\n{src}\n```\n" for name, src in snippets])}
""".strip()
    return sys_prompt, user_prompt

# ========== Per-file Analysis ==========
def extract_imports(language: str, code: str) -> List[str]:
    if language == "python":
        res = []
        try:
            tree = ast.parse(code)
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    res.extend([a.name for a in n.names])
                elif isinstance(n, ast.ImportFrom):
                    res.append(n.module or "")
        except Exception:
            pass
        return [x for x in res if x]
    else:
        # JS/TS
        imports = re.findall(r"\bimport\s+(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]", code)
        reqs = re.findall(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", code)
        return [*imports, *reqs]

def analyze_file(repo_root: Path, p: Path, max_bytes: int, model: str, client, outdir: Path,
                 max_snippet_chars: int, target_words: int, dry_run: bool) -> List[str]:
    """Returns list of generated md file paths."""
    code = read_text(p, max_bytes)
    if code is None:
        print(f"[skip] large or unreadable: {p}", file=sys.stderr)
        return []
    ext = p.suffix.lower()
    language = "python" if ext == ".py" else "javascript"

    generated: List[str] = []
    imports = extract_imports(language, code)

    if language == "python":
        funcs = parse_python_functions(code)  # name -> PyFunction
        names = sorted(funcs.keys())
        if not names:
            return []
        # Build edges within known functions
        edges: Dict[str, Set[str]] = {n:set() for n in names}
        for n, f in funcs.items():
            for c in f.calls:
                if c in funcs:
                    edges[n].add(c)
        groups = build_groups(names, edges)
        # reverse map (callers)
        callers: Dict[str, Set[str]] = {n:set() for n in names}
        for a, nbrs in edges.items():
            for b in nbrs:
                callers[b].add(a)
        # per group: call OpenAI
        base_dir = outdir / p.stem
        base_dir.mkdir(parents=True, exist_ok=True)
        for idx, g in enumerate(groups, start=1):
            group_funcs = [(n, funcs[n].src) for n in sorted(g)]
            # trim snippets to fit
            # compute callees for only group
            sub_edges = {n: set(sorted(edges.get(n, set()) & g)) for n in g}
            sub_callers = {n: set(sorted(callers.get(n, set()) & g)) for n in g}
            sys_p, user_p = make_group_prompt(repo_root, p, "python", imports, group_funcs, sub_callers, sub_edges, target_words)

            md_path = base_dir / f"group_{idx}.md"
            if dry_run:
                md_path.write_text(f"# DRY RUN: {p.name} / group {idx}\n\nFunctions: {', '.join(sorted(g))}\n")
                generated.append(str(md_path))
                continue

            try:
                content = call_openai(client, model, sys_p, user_p)
            except Exception as e:
                print(f"[error] OpenAI call failed for {p} group {idx}: {e}", file=sys.stderr)
                # write a stub with error info for traceability
                stub = f"# ERROR for {p.name} group {idx}\n\n{traceback.format_exc()}"
                md_path.write_text(stub)
                generated.append(str(md_path))
                continue

            md_path.write_text(content)
            generated.append(str(md_path))
            time.sleep(args.rate_limit_sleep)
        return generated

    else:
        # JS/TS
        jmap = parse_js_functions(code)  # name -> JsFunction
        names = sorted(jmap.keys())
        if not names:
            return []
        edges: Dict[str, Set[str]] = {n:set() for n in names}
        for n, f in jmap.items():
            for c in f.calls:
                if c in jmap:
                    edges[n].add(c)
        groups = build_groups(names, edges)
        callers: Dict[str, Set[str]] = {n:set() for n in names}
        for a, nbrs in edges.items():
            for b in nbrs:
                callers[b].add(a)

        base_dir = outdir / p.stem
        base_dir.mkdir(parents=True, exist_ok=True)
        for idx, g in enumerate(groups, start=1):
            group_funcs = [(n, jmap[n].src) for n in sorted(g)]
            sub_edges = {n: set(sorted(edges.get(n, set()) & g)) for n in g}
            sub_callers = {n: set(sorted(callers.get(n, set()) & g)) for n in g}

            # Trim big groups by code size to keep prompt reasonable
            total = 0
            trimmed: List[Tuple[str,str]] = []
            for name, src in sorted(group_funcs, key=lambda x: (-len(x[1]), x[0])):
                if total + len(src) > max_snippet_chars:
                    continue
                trimmed.append((name, src))
                total += len(src)

            sys_p, user_p = make_group_prompt(repo_root, p, "javascript", imports, trimmed, sub_callers, sub_edges, target_words)

            md_path = base_dir / f"group_{idx}.md"
            if dry_run:
                md_path.write_text(f"# DRY RUN: {p.name} / group {idx}\n\nFunctions: {', '.join(sorted(g))}\n")
                generated.append(str(md_path))
                continue

            try:
                content = call_openai(client, model, sys_p, user_p)
            except Exception as e:
                print(f"[error] OpenAI call failed for {p} group {idx}: {e}", file=sys.stderr)
                stub = f"# ERROR for {p.name} group {idx}\n\n{traceback.format_exc()}"
                md_path.write_text(stub)
                generated.append(str(md_path))
                continue

            md_path.write_text(content)
            generated.append(str(md_path))
            time.sleep(args.rate_limit_sleep)
        return generated

# ========== MAIN ==========
def main():
    global args
    args = parse_args()
    repo_root = Path(args.root).expanduser().resolve()
    if not repo_root.exists():
        print(f"[fatal] Root not found: {repo_root}", file=sys.stderr)
        sys.exit(2)

    outdir = Path(args.out).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    files = list_code_files(repo_root, include_exts=[e.lower() for e in args.include],
                            exclude_dirs=args.exclude_dirs, follow_symlinks=args.follow_symlinks)
    if not files:
        print("[warn] No code files found under root with given filters.", file=sys.stderr)
        sys.exit(0)

    print(f"[info] Root: {repo_root}")
    print(f"[info] Files to analyze: {len(files)}")
    for p in files[:10]:
        print(f"  - {p.relative_to(repo_root)}")
    if len(files) > 10:
        print(f"  … (+{len(files)-10} more)")

    client = None
    if not args.dry_run:
        client = get_openai_client()

    generated_all: List[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as ex:
        futs = {ex.submit(analyze_file, repo_root, p, args.max_file_bytes, args.model, client,
                          outdir, args.max_snippet_chars, args.group_word_target, args.dry_run): p
                for p in files}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                md_paths = fut.result()
                if md_paths:
                    print(f"[ok] {p.name}: wrote {len(md_paths)} group reports")
                    generated_all.extend(md_paths)
            except Exception as e:
                print(f"[error] {p}: {e}", file=sys.stderr)

    print(f"\nDone. Wrote {len(generated_all)} Markdown files to {outdir}")

if __name__ == "__main__":
    main()