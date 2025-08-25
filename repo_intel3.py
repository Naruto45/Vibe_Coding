#!/usr/bin/env python3
# repo_intel.py
# Strict version: discover .git dirs only via `find / -type d -name ".git" 2>/dev/null`,
# then analyze each repo and write reports. No caching, no extra flags.

import os
import re
import ast
import csv
import json
import sys
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Iterable, Optional

# ========================== Config ==========================
OUT_DIR = Path("./repo_reports3")
MAX_BYTES = 300_000          # per-file size cap
WORKERS = 4                  # analysis threads
SKIP_BINARIES = True
FOLLOW_SYMLINKS = False

DEFAULT_IGNORE_DIRS = {
    ".git", ".yarn", ".gradle", ".idea", ".vscode", ".venv", "venv", "env", "node_modules",
    "Pods", "build", "dist", "target", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".dart_tool", ".expo", "DerivedData"
}

BINARY_EXTS = {
    ".png",".jpg",".jpeg",".gif",".webp",".ico",".bmp",".tiff",".pdf",".zip",".gz",".bz2",".xz",
    ".7z",".dmg",".apk",".aab",".ipa",".mp3",".wav",".mp4",".mov",".m4a",".ogg",".webm",
    ".jar",".war",".class",".o",".a",".so",".dylib",".dll",".bin",".iso"
}

CODE_EXTS = {
    # Python
    ".py",
    # JavaScript/TypeScript
    ".js",".mjs",".cjs",".jsx",".ts",".tsx",
    # Go
    ".go",
    # Java/Kotlin
    ".java",".kt",".kts",
    # Swift
    ".swift",
    # C/C++
    ".c",".h",".hpp",".hh",".cc",".cpp",".cxx",
    # Shell
    ".sh",".bash",".zsh"
}

# ========================== Discovery ==========================

def discover_git_dirs_strict() -> List[str]:
    """
    Run exactly: find / -type d -name ".git" 2>/dev/null
    Return list of absolute .git directory paths.
    """
    print("[discovery] running: find / -type d -name \".git\" 2>/dev/null", file=sys.stderr)
    try:
        # Build one string and use shell to allow 2>/dev/null redirection
        cmd = 'find / -type d -name ".git" 2>/dev/null'
        cp = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        out = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
    except Exception as e:
        print(f"[discovery] ERROR running find: {e}", file=sys.stderr)
        out = []
    print(f"[discovery] found {len(out)} repos", file=sys.stderr)
    # Print each path to terminal
    for p in out:
        print(p)
    return out

# ========================== Git helpers ==========================

def repo_root_from_git_dir(git_dir: str) -> str:
    gd = Path(git_dir)
    if gd.is_dir() and gd.name == ".git":
        return str(gd.parent)
    try:
        cp = subprocess.run(["git","-C", str(gd), "rev-parse", "--show-toplevel"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        top = cp.stdout.strip()
        if top:
            return top
    except Exception:
        pass
    return str(gd)

def git_config_value(repo_root: str, key: str) -> Optional[str]:
    try:
        cp = subprocess.run(["git","-C", repo_root, "config", "--get", key],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        return cp.stdout.strip() or None
    except Exception:
        return None

def get_remote_url(repo_root: str) -> Optional[str]:
    origin = git_config_value(repo_root, "remote.origin.url")
    if origin:
        return origin
    try:
        cp = subprocess.run(["git","-C", repo_root, "remote","-v"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        for line in cp.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    except Exception:
        pass
    return None

def get_default_branch(repo_root: str) -> Optional[str]:
    try:
        cp = subprocess.run(["git","-C", repo_root, "symbolic-ref", "refs/remotes/origin/HEAD"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        ref = cp.stdout.strip()
        if ref and "/" in ref:
            return ref.split("/")[-1]
    except Exception:
        pass
    for b in ("main","master"):
        try:
            cp = subprocess.run(["git","-C", repo_root, "show-ref", "--verify", f"refs/heads/{b}"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
            if cp.returncode == 0:
                return b
        except Exception:
            pass
    return None

# ========================== File walk & parsers ==========================

def should_skip_dir(dirname: str) -> bool:
    return os.path.basename(dirname) in DEFAULT_IGNORE_DIRS

def looks_binary(path: str) -> bool:
    return Path(path).suffix.lower() in BINARY_EXTS

def list_repo_files(repo_root: str, follow_symlinks: bool) -> Iterable[str]:
    for root, dirs, files in os.walk(repo_root, followlinks=follow_symlinks):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        for f in files:
            yield os.path.join(root, f)

def safe_read_text(path: str, max_bytes: int) -> Optional[str]:
    try:
        p = Path(path)
        if p.is_symlink():
            return None
        if p.stat().st_size > max_bytes:
            return None
        with open(p, "rb") as fh:
            data = fh.read()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            for enc in ("utf-16","latin-1"):
                try:
                    return data.decode(enc)
                except Exception:
                    continue
    except Exception:
        return None
    return None

# ---- lightweight symbol extraction ----

def analyze_python(src: str) -> Dict:
    out = {"functions": [], "classes": [], "imports": []}
    try:
        t = ast.parse(src)
        for node in ast.walk(t):
            if isinstance(node, ast.FunctionDef):
                out["functions"].append(node.name)
            elif isinstance(node, ast.AsyncFunctionDef):
                out["functions"].append(node.name + " (async)")
            elif isinstance(node, ast.ClassDef):
                out["classes"].append(node.name)
            elif isinstance(node, ast.Import):
                out["imports"].extend([a.name for a in node.names])
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                out["imports"].append(mod)
    except Exception:
        pass
    return out

RE_FUNC_JS = re.compile(r"\bfunction\s+([A-Za-z0-9_]+)\s*\(|\b([A-Za-z0-9_]+)\s*=\s*\([^)]*\)\s*=>", re.MULTILINE)
RE_CLASS_JS = re.compile(r"\bclass\s+([A-Za-z0-9_]+)\b")
RE_IMPORT_JS = re.compile(r"\bimport\s+(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]", re.MULTILINE)

def analyze_js_ts(src: str) -> Dict:
    funcs = []
    for m in RE_FUNC_JS.finditer(src):
        name = m.group(1) or m.group(2)
        if name: funcs.append(name)
    classes = RE_CLASS_JS.findall(src)
    imports = RE_IMPORT_JS.findall(src)
    return {"functions": funcs, "classes": classes, "imports": imports}

RE_FUNC_GO = re.compile(r"\bfunc\s+(?:\([^)]+\)\s*)?([A-Za-z0-9_]+)\s*\(", re.MULTILINE)
RE_IMPORT_GO = re.compile(r"\bimport\s+(?:\(\s*([^)]*?)\s*\)|\"([^\"]+)\")", re.MULTILINE)

def analyze_go(src: str) -> Dict:
    funcs = RE_FUNC_GO.findall(src)
    im = []
    for m in RE_IMPORT_GO.finditer(src):
        block, single = m.groups()
        if block:
            for line in block.splitlines():
                q = line.strip().strip('"')
                if q: im.append(q)
        elif single:
            im.append(single)
    return {"functions": funcs, "classes": [], "imports": im}

RE_CLASS_JAVA = re.compile(r"\b(class|interface|enum)\s+([A-Za-z0-9_]+)")
RE_METHOD_JAVA = re.compile(r"\b(?:public|private|protected|static|\s)+\s*[A-Za-z0-9_<>\[\]]+\s+([A-Za-z0-9_]+)\s*\(", re.MULTILINE)
RE_IMPORT_JAVA = re.compile(r"\bimport\s+([A-Za-z0-9_.]+);")

def analyze_java_like(src: str) -> Dict:
    classes = [m[1] for m in RE_CLASS_JAVA.findall(src)]
    funcs = RE_METHOD_JAVA.findall(src)
    imports = RE_IMPORT_JAVA.findall(src)
    return {"functions": funcs, "classes": classes, "imports": imports}

RE_FUNC_C = re.compile(r"^[A-Za-z_][A-Za-z0-9_*\s]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{]*\)\s*{", re.MULTILINE)
def analyze_c_cpp(src: str) -> Dict:
    funcs = RE_FUNC_C.findall(src)
    return {"functions": funcs, "classes": [], "imports": []}

RE_FUNC_SH = re.compile(r"^\s*([A-Za-z0-9_]+)\s*\(\)\s*{", re.MULTILINE)
def analyze_shell(src: str) -> Dict:
    funcs = RE_FUNC_SH.findall(src)
    return {"functions": funcs, "classes": [], "imports": []}

# ========================== Dependency sniffers ==========================

def read_package_manifests(repo_root: str) -> Dict[str, Dict]:
    res: Dict[str, Dict] = {}
    # Python
    for fn in ("requirements.txt","pyproject.toml","Pipfile","environment.yml"):
        p = Path(repo_root, fn)
        if p.exists():
            res[fn] = {"content": (p.read_text(errors="ignore")[:50_000])}
    # Node
    pjson = Path(repo_root, "package.json")
    if pjson.exists():
        try:
            res["package.json"] = json.loads(pjson.read_text(errors="ignore"))
        except Exception:
            res["package.json"] = {"content": pjson.read_text(errors="ignore")[:50_000]}
    # Go
    for fn in ("go.mod","go.sum"):
        p = Path(repo_root, fn)
        if p.exists():
            res[fn] = {"content": p.read_text(errors="ignore")[:50_000]}
    # Java/Gradle/Maven
    for fn in ("build.gradle","build.gradle.kts","settings.gradle","pom.xml"):
        p = Path(repo_root, fn)
        if p.exists():
            res[fn] = {"content": p.read_text(errors="ignore")[:50_000]}
    # Swift/Apple
    for fn in ("Package.swift","Podfile","Cartfile"):
        p = Path(repo_root, fn)
        if p.exists():
            res[fn] = {"content": p.read_text(errors="ignore")[:50_000]}
    return res

# ========================== Analyzer ==========================

def analyze_file(path: str) -> Optional[Tuple[str, Dict]]:
    ext = Path(path).suffix.lower()
    if SKIP_BINARIES and looks_binary(path):
        return None
    if ext not in CODE_EXTS:
        return None
    src = safe_read_text(path, MAX_BYTES)
    if src is None:
        return None

    if ext == ".py":
        info = analyze_python(src)
    elif ext in {".js",".mjs",".cjs",".jsx",".ts",".tsx"}:
        info = analyze_js_ts(src)
    elif ext == ".go":
        info = analyze_go(src)
    elif ext in {".java",".kt",".kts"}:
        info = analyze_java_like(src)
    elif ext in {".c",".h",".hpp",".hh",".cc",".cpp",".cxx"}:
        info = analyze_c_cpp(src)
    elif ext in {".sh",".bash",".zsh"}:
        info = analyze_shell(src)
    else:
        return None

    info["lines"] = src.count("\n") + 1
    return path, info

def summarize_symbols(per_file: Dict[str, Dict]) -> Dict[str, int]:
    totals = {"files": 0, "lines": 0, "functions": 0, "classes": 0}
    for _, meta in per_file.items():
        totals["files"] += 1
        totals["lines"] += meta.get("lines", 0)
        totals["functions"] += len(meta.get("functions", []))
        totals["classes"] += len(meta.get("classes", []))
    return totals

def write_repo_report(outdir: Path, repo_name: str, repo_root: str, remote: Optional[str],
                      default_branch: Optional[str], totals: Dict[str,int],
                      per_file: Dict[str, Dict], manifests: Dict[str, Dict]) -> str:
    outdir.mkdir(parents=True, exist_ok=True)
    md_path = outdir / f"{repo_name}.md"
    rel = lambda p: str(Path(p).resolve()).replace(str(Path(repo_root).resolve()) + os.sep, "")

    def fmt_symbols(lst: List[str], max_show=20):
        if not lst: return "_none_"
        shown = ", ".join(sorted(lst)[:max_show])
        more = len(lst) - min(len(lst), max_show)
        return shown + (f" …(+{more})" if more > 0 else "")

    lines = []
    lines.append(f"# {repo_name}\n")
    lines.append(f"- **Path:** `{repo_root}`")
    if remote: lines.append(f"- **Remote:** `{remote}`")
    if default_branch: lines.append(f"- **Default branch (heuristic):** `{default_branch}`")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Files analyzed: **{totals['files']}**")
    lines.append(f"- Total LOC (approx): **{totals['lines']}**")
    lines.append(f"- Functions found: **{totals['functions']}**")
    lines.append(f"- Classes found: **{totals['classes']}**")
    lines.append("")
    if manifests:
        lines.append("## Dependency manifests (snippets or parsed):")
        for k, _ in manifests.items():
            lines.append(f"- `{k}`")
    else:
        lines.append("## Dependency manifests")
        lines.append("_none found_")
    lines.append("")

    lines.append("## Files")
    for fpath, meta in sorted(per_file.items()):
        relp = rel(fpath)
        funcs = meta.get("functions", [])
        classes = meta.get("classes", [])
        imps = meta.get("imports", [])

        lines.append(f"### `{relp}`")
        lines.append(f"- ~{meta.get('lines', 0)} lines")
        if funcs: lines.append(f"- Functions: {fmt_symbols(funcs)}")
        if classes: lines.append(f"- Classes: {fmt_symbols(classes)}")
        if imps: lines.append(f"- Imports: {fmt_symbols(imps)}")
        lines.append("")

    if manifests:
        lines.append("## Manifest Contents (truncated)")
        for k, v in manifests.items():
            lines.append(f"### `{k}`")
            if isinstance(v, dict) and "content" in v:
                snippet = v["content"]
                lines.append("```")
                lines.append(snippet[:4000])
                lines.append("```")
            else:
                try:
                    lines.append("```json")
                    lines.append(json.dumps(v, indent=2)[:4000])
                    lines.append("```")
                except Exception:
                    lines.append("```")
                    lines.append(str(v)[:4000])
                    lines.append("```")

    md_path.write_text("\n".join(lines))
    return str(md_path)

# ========================== Orchestration ==========================

def analyze_repo(git_dir: str) -> Optional[Dict]:
    repo_root = repo_root_from_git_dir(git_dir)
    repo_name = Path(repo_root).name or Path(repo_root).parent.name
    remote = get_remote_url(repo_root)
    default_branch = get_default_branch(repo_root)

    per_file: Dict[str, Dict] = {}
    for path in list_repo_files(repo_root, FOLLOW_SYMLINKS):
        if SKIP_BINARIES and looks_binary(path):
            continue
        res = analyze_file(path)
        if res:
            p, info = res
            per_file[p] = info

    totals = summarize_symbols(per_file)
    manifests = read_package_manifests(repo_root)
    report_path = write_repo_report(OUT_DIR, repo_name, repo_root, remote, default_branch, totals, per_file, manifests)

    return {
        "name": repo_name,
        "root": repo_root,
        "git_dir": git_dir,
        "remote": remote or "",
        "default_branch": default_branch or "",
        "files": totals["files"],
        "lines": totals["lines"],
        "functions": totals["functions"],
        "classes": totals["classes"],
        "report": report_path,
    }

def write_index(rows: List[Dict], outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "index.csv"
    cols = ["name","root","git_dir","remote","default_branch","files","lines","functions","classes","report"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(path)

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Discover .git directories using the exact command
    git_dirs: List[str] = discover_git_dirs_strict()
    if not git_dirs:
        print("\n[info] No .git directories returned by `find / ...`. On macOS, you may need to grant Terminal 'Full Disk Access' in System Settings → Privacy & Security → Full Disk Access.", file=sys.stderr)

    # 2) Analyze each repo
    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as ex:
        futs = {ex.submit(analyze_repo, gd): gd for gd in git_dirs}
        for fut in as_completed(futs):
            gd = futs[fut]
            try:
                row = fut.result()
                if row:
                    results.append(row)
                    print(f"[report] {row['name']} -> {row['report']}", file=sys.stderr)
            except Exception as e:
                print(f"[warn] analysis failed for {gd}: {e}", file=sys.stderr)

    # 3) Write index
    index_path = write_index(results, OUT_DIR)
    print(f"\nDone. Index: {index_path}")
    print("Per-repo Markdown reports are in:", OUT_DIR)

if __name__ == "__main__":
    main()