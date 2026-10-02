#!/usr/bin/env python3
"""只读清点路由、测试声明及 HTTP 请求引用；不导入或启动被分析项目。

运行：python3 教程/辅助脚本/清点接口与测试.py --output /tmp/openviking-audit
使用 Git 跟踪文件保证清点范围稳定。外部 MCP SDK 的五组动态授权路由
来自 uv.lock 的 1.27.0 tag（URL 保存在 inventory 中），显式单列，
不把它们加入主 REST 直接请求匹配率的分母。
"""
from __future__ import annotations

import argparse
import ast
import collections
import csv
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
UNKNOWN = object()


def read(path):
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


def kw(call, name, default=None):
    return next((x.value for x in call.keywords if x.arg == name), default)


def value(node, env=None, templates=False):
    env = env or {}
    if node is None:
        return UNKNOWN
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id, UNKNOWN)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = value(node.left, env, templates), value(node.right, env, templates)
        if isinstance(a, str) and isinstance(b, str):
            return a + b
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [value(x, env, templates) for x in node.elts]
        return vals if UNKNOWN not in vals else UNKNOWN
    if isinstance(node, ast.Dict):
        keys = [value(x, env, templates) for x in node.keys]
        vals = [value(x, env, templates) for x in node.values]
        if UNKNOWN not in keys and UNKNOWN not in vals:
            return dict(zip(keys, vals))
    if isinstance(node, ast.JoinedStr) and templates:
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                out.append("{" + ast.unparse(part.value) + "}")
        return "".join(out)
    return UNKNOWN


def constant_env(tree):
    env = {}
    # 只求值顶层常量，不用不同函数内的同名局部变量推断请求路径。
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            val = value(node.value, env)
            if val is not UNKNOWN:
                for target in targets:
                    if isinstance(target, ast.Name):
                        env[target.id] = val
    return env


def python_routes(path, scope, extra_prefix="", owner_prefixes=None):
    tree = ast.parse(read(path), filename=path)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    env = constant_env(tree)
    prefixes = dict(owner_prefixes or {})
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if ast.unparse(node.value.func).endswith("APIRouter"):
                prefix = value(kw(node.value, "prefix"), env)
                prefix = prefix if isinstance(prefix, str) else ""
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        prefixes.setdefault(target.id, prefix)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            attr = dec.func.attr.lower()
            if attr not in METHODS | {"api_route", "websocket"}:
                continue
            route_path = value(dec.args[0] if dec.args else kw(dec, "path"), env)
            if not isinstance(route_path, str):
                raise ValueError(f"unresolved route: {path}:{dec.lineno}: {ast.unparse(dec)}")
            methods = value(kw(dec, "methods"), env) if attr == "api_route" else [attr.upper()]
            owner = ast.unparse(dec.func.value)
            prefix = extra_prefix + prefixes.get(owner, "")
            doc = ast.get_docstring(node) or ""
            ancestor = parents.get(node)
            enclosing = []
            while ancestor is not None:
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    enclosing.append(ancestor.name)
                ancestor = parents.get(ancestor)
            for method in methods:
                out.append(dict(scope=scope, method=method, path=prefix + route_path,
                                source=path, line=dec.lineno, handler=node.name,
                                doc=doc, deprecated=value(kw(dec, "deprecated"), env) is True,
                                registration="decorator", enclosing=list(reversed(enclosing))))
    # add_api_route 的显式路径；favicon 的字典循环另行展开。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_api_route" or not node.args:
            continue
        route_path = value(node.args[0], env)
        if not isinstance(route_path, str):
            continue
        methods = value(kw(node, "methods"), env)
        if methods is UNKNOWN:
            methods = ["GET"]
        handler = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
        for method in methods:
            out.append(dict(scope=scope, method=method, path=extra_prefix + route_path,
                            source=path, line=node.lineno, handler=handler, doc="",
                            deprecated=False, registration="add_api_route"))
    return out


def api_inventory(files):
    app_path = "openviking/server/app.py"
    app_tree = ast.parse(read(app_path))
    exports_tree = ast.parse(read("openviking/server/routers/__init__.py"))
    exports = {}
    for node in exports_tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                exports[alias.asname or alias.name] = node.module.replace(".", "/") + ".py"
    routes = []
    mounts = []
    for call in ast.walk(app_tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            if call.func.attr == "include_router" and call.args:
                router_name = ast.unparse(call.args[0])
                path = exports.get(router_name)
                if path:
                    p = value(kw(call, "prefix"))
                    p = p if isinstance(p, str) else ""
                    scope = "main_webdav" if router_name == "webdav_router" else "main_rest"
                    routes.extend(python_routes(path, scope, p))
                    mounts.append(dict(name=router_name, source=path, prefix=p, line=call.lineno))
    routes.extend(python_routes(app_path, "main_ui"))
    routes.extend(python_routes("openviking/server/oauth/router.py", "main_oauth_custom"))
    # 读取 app 中静态 favicon 字典；循环是动态注册而非六个装饰器。
    for node in ast.walk(app_tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "_favicon_files" for t in node.targets):
                favicon = value(node.value)
                for path in favicon:
                    routes.append(dict(scope="main_ui", method="GET", path=path,
                                       source=app_path, line=node.lineno, handler="_make_favicon_handler",
                                       doc="Favicon 文件", deprecated=False, registration="dictionary-loop"))
    # app.routes.append(_ScopedRoute(...)) 的方法列表也直接由 AST 求值。
    for call in ast.walk(app_tree):
        if isinstance(call, ast.Call) and ast.unparse(call.func) == "_ScopedRoute":
            for method in value(kw(call, "methods")):
                routes.append(dict(scope="main_mcp_transport", method=method,
                                   path=value(call.args[0]), source=app_path, line=call.lineno,
                                   handler="create_mcp_app", doc="MCP streamable HTTP",
                                   deprecated=False, registration="Starlette Route"))
    # FastAPI 自动文档（app 默认构造，均未禁用）；独立服务的相同自动文档不再重复。
    for path in ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"]:
        routes.append(dict(scope="main_framework", method="GET", path=path,
                           source=app_path, line=next(c.lineno for c in ast.walk(app_tree)
                                                    if isinstance(c, ast.Call) and ast.unparse(c.func) == "FastAPI"),
                           handler="FastAPI default", doc="框架自动文档", deprecated=False,
                           registration="framework default"))
    if not re.search(r'name = "mcp"\nversion = "1\.27\.0"', read("uv.lock")):
        raise ValueError("uv.lock 的 MCP 版本已变化，请重新核对官方 SDK 路由后更新 sdk_specs")
    sdk_source = "https://github.com/modelcontextprotocol/python-sdk/blob/v1.27.0/src/mcp/server/auth/routes.py"
    sdk_specs = [("/.well-known/oauth-authorization-server", ["GET", "OPTIONS"], 85),
                 ("/authorize", ["GET", "POST"], 93), ("/token", ["POST", "OPTIONS"], 100),
                 ("/register", ["POST", "OPTIONS"], 115), ("/revoke", ["POST", "OPTIONS"], 127)]
    for path, methods, line in sdk_specs:
        for method in methods:
            routes.append(dict(scope="main_oauth_sdk", method=method, path=path,
                               source=sdk_source, line=line, handler="create_auth_routes",
                               doc="OAuth SDK 动态授权路由；oauth.enabled 时挂载",
                               deprecated=False, registration="SDK create_auth_routes"))
    independent = {
        "openviking/storage/vectordb/service/api_fastapi.py": ("vectordb_service", ""),
        "openviking/storage/vectordb/service/server_fastapi.py": ("vectordb_service", ""),
        "openviking/session/train/components/dataset_service.py": ("dataset_service", ""),
        "benchmark/aml/server.py": ("aml_adapter", ""),
        "examples/openwebui-plugin/openviking_openwebui/tools.py": ("openwebui_bridge", ""),
        "examples/openwebui-plugin/openviking_openwebui/server.py": ("openwebui_bridge", ""),
        "examples/llamaparse-understanding-bridge/openviking_llamaparse_bridge/bridge.py": ("llamaparse_bridge", ""),
        "bot/demo/werewolf/werewolf_server.py": ("werewolf_demo", ""),
        "bot/vikingbot/compile/router.py": ("bot_gateway", ""),
        "bot/vikingbot/studio/router.py": ("bot_gateway", "/bot/v1"),
    }
    for path, (scope, prefix) in independent.items():
        found = python_routes(path, scope, prefix)
        if path == "bot/vikingbot/compile/router.py":
            for route in found:
                if "register_compile_routes" in route["enclosing"]:
                    route["path"] = "/bot/v1" + route["path"]
        routes.extend(found)
    bot_routes = python_routes("bot/vikingbot/channels/openapi.py", "bot_gateway")
    for route in bot_routes:
        # 两个 factory 各有同名 router 变量；按 AST 外层函数区分，避免行号阈值。
        # 实际装配是 _setup_routes() 中 include_router(get_router(), prefix="/bot/v1")
        # 和 include_router(get_gateway_router())，不替二者共用相同 prefix。
        if "_create_router" in route["enclosing"]:
            route["path"] = "/bot/v1" + route["path"]
        elif "_create_gateway_router" not in route["enclosing"]:
            raise ValueError(f"unknown Bot route factory: {route}")
    routes.extend(bot_routes)
    tools = []
    mcp_source = "openviking/server/mcp_endpoint.py"
    for node in ast.walk(ast.parse(read(mcp_source))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and ast.unparse(dec.func) == "mcp.tool":
                    name = value(kw(dec, "name"))
                    tools.append(dict(name=name if isinstance(name, str) else node.name,
                                      handler=node.name, source=mcp_source, line=dec.lineno,
                                      doc=ast.get_docstring(node) or ""))
    # Starlette Route（不是 FastAPI APIRoute）含 GET 时自动注册 HEAD。
    # SDK、MCP 的 _ScopedRoute 和 FastAPI 自动文档采用这一规则。
    implicit_head = []
    for route in routes:
        if route["scope"] in {"main_oauth_sdk", "main_mcp_transport", "main_framework"} and route["method"] == "GET":
            head = dict(route)
            head.update(method="HEAD", registration=route["registration"] + "; Starlette implicit HEAD")
            implicit_head.append(head)
    routes.extend(implicit_head)
    route_keys = [(r["scope"], r["method"], r["path"]) for r in routes]
    if len(route_keys) != len(set(route_keys)):
        raise ValueError("重复 scope/method/path：请核对实际注册顺序及覆盖关系")
    return dict(routes=routes, mounts=mounts, mcp_tools=tools,
                counts=dict(collections.Counter(r["scope"] for r in routes)),
                sdk_version_source="uv.lock:2655", sdk_source=sdk_source)


def is_test_path(path):
    p = Path(path)
    return ("tests" in p.parts or "test" in p.parts or
            re.search(r"(^test(?:_.*)?|.*_test|.*[.-](test|spec))\.(py|[cm]?js|tsx?|rs|cpp|cc|go|sh)$", p.name) is not None)


def symbols(path):
    content = read(path)
    out = []
    suffix = Path(path).suffix
    if suffix == ".py":
        tree = ast.parse(content, filename=path)
        def walk(node, classes=()):
            if isinstance(node, ast.ClassDef):
                classes = classes + (node.name,)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                if any("fixture" in ast.unparse(d).split("(")[0] for d in node.decorator_list):
                    return
                out.append(dict(name="::".join(classes + (node.name,)), line=node.lineno,
                                scenario=(ast.get_docstring(node) or "").split("\n")[0]))
                return  # 不重复计算测试函数内部的 helper。
            for child in ast.iter_child_nodes(node):
                walk(child, classes)
        walk(tree)
    else:
        patterns = []
        if suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"}:
            # Node 子测试 t.test 也单列；正则只匹配字符串标题，跳过 RegExp.test。
            patterns = [r"(?<![\w.])(?:test|it|t\.test)(?:\.(?:skip|only|todo|concurrent|fails))*\s*(?:\.each\s*\([\s\S]*?\)\s*)?\(\s*(['\"`])((?:\\.|(?!\1)[\s\S])*?)\1"]
        elif suffix == ".rs":
            patterns = [r"#\[(?:[\w]+::)?(?:test|rstest)(?:\([^\]]*\))?\]\s*(?:#\[[^\]]*\]\s*)*(?:async\s+)?fn\s+(\w+)\s*\("]
        elif suffix in {".cpp", ".cc", ".c", ".h"}:
            patterns = [r"\b(?:TEST|TEST_F|TEST_P)\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)",
                        r"\bTEST_CASE\s*\(\s*\"([^\"]+)\"", r"\bvoid\s+(test_\w+)\s*\("]
        elif suffix == ".go":
            patterns = [r"\bfunc\s+(Test\w+)\s*\(\s*\w+\s+\*testing\.T\s*\)"]
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                groups = match.groups()
                name = groups[1] if suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs"} else ".".join(groups)
                out.append(dict(name=name, line=content.count("\n", 0, match.start()) + 1, scenario=name))
    return sorted(out, key=lambda s: s["line"])


def test_inventory(files):
    out = []
    for path in files:
        eligible = is_test_path(path)
        # Rust 单元测试嵌在生产文件中，逐文件计入。
        if path.startswith("crates/") and path.endswith(".rs"):
            eligible = eligible or bool(re.search(r"#\[(?:\w+::)?(?:test|rstest)", read(path)))
        if not eligible:
            continue
        syms = symbols(path)
        kind = "test" if syms else "support_or_data"
        out.append(dict(path=path, kind=kind, symbols=syms, count=len(syms),
                        third_party=path.startswith("third_party/")))
    return out


def request_refs(tests):
    refs = []
    for info in tests:
        path = info["path"]
        # 仅根 tests 下、包含静态测试声明的 Python 文件；其他 SDK/插件另列。
        if not path.startswith("tests/") or not path.endswith(".py") or not info["count"]:
            continue
        tree = ast.parse(read(path))
        env = constant_env(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr = node.func.attr.lower()
            method, url_node = None, None
            if attr in METHODS:
                method = attr.upper()
                url_node = node.args[0] if node.args else kw(node, "url")
            elif attr in {"request", "_request", "_request_with_retry"}:
                m = value(node.args[0] if node.args else kw(node, "method"), env)
                if isinstance(m, str) and m.lower() in METHODS | {"propfind", "mkcol", "move"}:
                    method = m.upper()
                    url_node = node.args[1] if len(node.args) > 1 else kw(node, "url")
            if not method:
                continue
            url = value(url_node, env, templates=True)
            if not isinstance(url, str):
                continue
            # base_url f-string 的前半段不参与 path 判断。
            if "/api/v1" in url:
                url = url[url.index("/api/v1"):]
            elif url.startswith(("http://", "https://")):
                url = re.sub(r"^https?://[^/]+", "", url)
            if not url.startswith("/"):
                continue
            url = url.split("?", 1)[0].split("#", 1)[0]
            refs.append(dict(method=method, path=url, source=path, line=node.lineno,
                             expression=ast.unparse(node)))
    return refs


def matches(route_path, request_path):
    # 路由与请求占位符按单段处理；:path 多段请求可能遗漏，仍保留在 REST 分母。
    route_parts = route_path.split("/")
    request_parts = request_path.split("/")
    if len(route_parts) != len(request_parts):
        return False
    for expected, seen in zip(route_parts, request_parts):
        if expected == seen:
            continue
        if re.fullmatch(r"\{[^{}]+\}", expected):
            if not seen:
                return False
            continue
        # f-string 占位符仅在路由此段也是参数时命中，防止任意变量冒充静态子路径。
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/openviking-audit"))
    args = parser.parse_args()
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    files = [p for p in files if p and (ROOT / p).is_file()]
    api = api_inventory(files)
    tests = test_inventory(files)
    refs = request_refs(tests)
    coverage = []
    for route in api["routes"]:
        if route["scope"] != "main_rest":
            continue
        hits = [r for r in refs if r["method"] == route["method"] and matches(route["path"], r["path"])]
        coverage.append(dict(method=route["method"], path=route["path"], source=route["source"],
                             line=route["line"], files=sorted({r["source"] for r in hits}), refs=hits))
    summary = dict(commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
                   main_rest_denominator=len(coverage), main_rest_matched=sum(bool(c["files"]) for c in coverage),
                   route_scopes=api["counts"], mcp_tools=len(api["mcp_tools"]),
                   test_files=sum(bool(t["count"]) for t in tests), test_symbols=sum(t["count"] for t in tests),
                   test_inventory_files=len(tests),
                   first_party_test_files=sum(bool(t["count"]) and not t["third_party"] for t in tests),
                   first_party_test_symbols=sum(t["count"] for t in tests if not t["third_party"]))
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, data in [("api-inventory.json", api), ("test-inventory.json", tests),
                           ("main-rest-request-matches.json", coverage), ("summary.json", summary)]:
        (args.output / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    with (args.output / "test-files.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["path", "kind", "test_declarations", "third_party"])
        for t in tests:
            writer.writerow([t["path"], t["kind"], t["count"], t["third_party"]])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
