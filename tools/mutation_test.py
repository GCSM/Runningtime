"""Tiny mutation tester: apply one AST mutation at a time to a module, run a fast test subset in an isolated copy, report survivors.

  python tools/mutation_test.py coach/plancheck.py 200      # try 200 random mutants of that file (4 parallel copies of the repo)

A survivor is a deliberate bug (a flipped comparison, a changed constant, + for -, min for max) that no test noticed: a gap in the tests, or an
equivalent mutant. Line coverage can be 95% while this says 40% (that was the state of plancheck.py before the QA of plan generation).
"""
import ast, json, multiprocessing as mp, os, random, shutil, subprocess, sys, tempfile, time
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(tempfile.gettempdir(), "runningtime-mutation")
os.makedirs(WORK, exist_ok=True)
TESTS = ["tests/test_knowledge.py", "tests/test_plan_shape.py", "tests/test_scenarios.py", "tests/test_plan_integrity.py", "tests/test_actions.py", "tests/test_core.py", "tests/test_sanitize_rules.py", "tests/test_adherence_rules.py", "tests/test_skeleton_rules.py", "tests/test_qa_regressions.py", "tests/test_athlete_sim.py", "tests/test_fuzz_sanitize.py"]

SWAP_CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
SWAP_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
SWAP_NAME = {"min": "max", "max": "min"}

class Collect(ast.NodeVisitor):
    def __init__(self): self.sites = []; self.stack = []
    def generic_visit(self, node):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP_CMP: self.sites.append(("cmp", node))
        elif isinstance(node, ast.BinOp) and type(node.op) in SWAP_BIN: self.sites.append(("bin", node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool) and node.value not in (0, 1, 2): self.sites.append(("const", node))
        elif isinstance(node, ast.Name) and node.id in SWAP_NAME and isinstance(getattr(node, "ctx", None), ast.Load): self.sites.append(("name", node))
        elif isinstance(node, ast.BoolOp): self.sites.append(("bool", node))
        super().generic_visit(node)

def mutate(src, idx):
    tree = ast.parse(src); c = Collect(); c.visit(tree)
    kind, node = c.sites[idx]
    if kind == "cmp": node.ops = [SWAP_CMP[type(node.ops[0])]()]
    elif kind == "bin": node.op = SWAP_BIN[type(node.op)]()
    elif kind == "const": node.value = node.value * 1.5 if isinstance(node.value, float) else node.value + (1 if node.value > 0 else -1) * max(1, abs(node.value) // 4)
    elif kind == "name": node.id = SWAP_NAME[node.id]
    elif kind == "bool": node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    ast.fix_missing_locations(tree)
    return ast.unparse(tree), kind, node.lineno

def worker(args):
    wid, jobs, target = args
    d = f"{WORK}/w{wid}"
    if os.path.exists(d): shutil.rmtree(d)
    shutil.copytree(REPO, d, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "data", ".pytest_cache"))
    
    orig = open(f"{d}/{target}").read(); out = []
    for idx in jobs:
        try: new, kind, line = mutate(orig, idx)
        except Exception as e: out.append((idx, "error", 0, str(e))); continue
        open(f"{d}/{target}", "w").write(new)
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TESTS], cwd=d, capture_output=True, timeout=120)
            status = "survived" if r.returncode == 0 else "killed"
        except subprocess.TimeoutExpired: status = "killed"
        out.append((idx, status, line, kind))
    open(f"{d}/{target}", "w").write(orig)
    return out

if __name__ == "__main__":
    target, n = sys.argv[1], int(sys.argv[2])
    src = open(f"{REPO}/{target}").read(); c = Collect(); c.visit(ast.parse(src)); total = len(c.sites)
    idxs = list(range(total)); random.Random(7).shuffle(idxs); idxs = sorted(idxs[:n])
    chunks = [idxs[i::4] for i in range(4)]
    t = time.time()
    with mp.Pool(4) as p: res = sum(p.map(worker, [(i, ch, target) for i, ch in enumerate(chunks)]), [])
    surv = sorted([r for r in res if r[1] == "survived"], key=lambda r: r[2])
    print(f"{target}: {len(res)} of {total} mutants tried, {len(surv)} survived ({100 * (len(res) - len(surv)) // max(1, len(res))}% killed) in {time.time() - t:.0f}s")
    lines = src.splitlines()
    for idx, st, line, kind in surv: print(f"  line {line} [{kind}]: {lines[line - 1].strip()[:110]}")
    json.dump(res, open(f"{WORK}/result_{os.path.basename(target)}.json", "w"))
