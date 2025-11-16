from typing import Generator, Literal, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import threading
import networkx as nx
import requests
from rich.progress import Progress, DownloadColumn, TextColumn, BarColumn, TransferSpeedColumn, MofNCompleteColumn, TaskID
from rich.console import Console
from rich.tree import Tree
from pyvis.network import Network
from pathlib import Path
import hashlib
from io import BytesIO
import time

from moderr import ModError, ModNotFoundError, ModIncompatibleError
from mod import Mod, Dep


class RateLimiter:
    """
    Modrinth 的接口是有速率限制的
    """

    rate_limit: float
    last_req: float

    def __init__(self, req_per_min: int) -> None:
        self.rate_limit = 60 / req_per_min
        self.last_req = 0.0
        # 为并发加锁
        self.lock = threading.Lock()

    def wait(self):
        # 等待锁
        with self.lock:
            now = time.time()
            delta = now - self.last_req

            if delta < self.rate_limit:
                time.sleep(self.rate_limit - delta)

            self.last_req = time.time()

ProgressGen = Generator[tuple[int, float | None, str | None], None, Sequence[Exception]]

class ModManager:
    # 限制请求速率
    rl: RateLimiter
    # 维护一个线程池来并发请求
    __pool: ThreadPoolExecutor
    console: Console
    # 需要的模组
    mods: list[Mod] = []
    target_version: str
    target_loader: str
    require_client: bool 
    require_server: bool

    # finish时候输出的信息
    finalmsg: list

    # 用于减少依赖图提示信息中的无效内容
    met_condition: set[str]

    # 包含依赖的所有模组
    all_mods: dict[str, Mod]

    def __init__(self, threads: int = 4, console: Console = Console()) -> None:
        self.__pool = ThreadPoolExecutor(threads)
        self.console = console
        self.mods = []
        self.all_mods = {}
        self.finalmsg = []
        self.met_condition = set()
        self.rl = RateLimiter(300)
        # self.__cached_mods = []

    def __enter__(self):
        self.finalmsg = []
        return self

    def __exit__(self, exc_type, exc, tb):
        self.finish()

        return False

    def finish(self):
        self.__pool.shutdown()

        for msg in self.finalmsg:
            self.console.print(msg)
        self.finalmsg = []
        self.met_condition = set()

    def handle_future(self, futures: list[Future], progress: Progress, task_id: TaskID) -> Generator[tuple[int, float | None, str | None], None, list[ModError]]:
        errors: list[ModError] = []
        for future in as_completed(futures):
            message = None
            try:
                message = future.result()
            except ModError as e:
                message = f"[yellow]警告 {e}[/yellow]"
                progress.print(message)
                errors.append(e)
            except Exception:
                # 真的很异常的异常应当上报
                progress.stop()
                self.__pool.shutdown()

                raise
            finally:
                progress.update(task_id, advance=1)
                yield (1, progress.tasks[task_id].total, message)
        return errors

    def init_mod(self, version: str, loader: str, require_client: bool, require_server: bool) -> ProgressGen:
        """
        统一初始化已有的模组

        return: 是否应该继续
        """

        self.require_client = require_client
        self.require_server = require_server
        
        self.target_version = version
        self.target_loader = loader

        errors: list[ModError] = []
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self.console
        ) as progress:
            task_id = progress.add_task("解析模组", True, len(self.mods))

            # 多线程初始化
            result = yield from self.handle_future(
                [self.__pool.submit(mod.init, self.target_version, self.target_loader, require_client, require_server, progress, self.rl) for mod in self.mods], 
                progress, 
                task_id
            )
            errors.extend(result)

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
        return errors

    def check_version(self) -> ProgressGen:
        """
        查询所有模组在此目标版本下的可用性

        return: 是否应该继续
        """

        errors: list[ModError] = []
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self.console
        ) as progress:
            task_id = progress.add_task("搜索版本", True, len(self.mods))

            # 多线程检查版本可用性
            result = yield from self.handle_future(
                [self.__pool.submit(mod.query_version, progress, self.rl) for mod in self.mods], 
                progress, 
                task_id
            )
            errors.extend(result)

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
        return errors

    def resolve_dependencies(self, allow_optional_mod: bool = False) -> ProgressGen:
        """
        解析所有模组在设定版本下的依赖

        return: 是否应该继续
        """

        # 用内置类型先存储图的信息，方便进行修改
        # 项目ID: 信息
        nodes: dict[str, Mod] = {}
        # (依赖项目ID, 父项目ID, 信息)
        edges: list[tuple[str, str, Dep]] = []

        edge_style: dict[
            Literal["required", "optional", "incompatible", "embedded"], 
            dict
        ] = {
            "required": {
                "color": "lightgreen",
                "type": "required",
            },
            "incompatible": {
                "label": "❌", 
                "color": "red", 
                "font": {
                    "align": "middle"
                },
                "type": "incompatible"
            },
            "optional": {
                "color": "lightgrey",
                "type": "optional",
            },
            "embedded": {
                "color": "lightpurple",
                "type": "embedded"
            }
        }

        errors: list[ModError] = []
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self.console
        ) as progress:
            task_id = progress.add_task("解析依赖", True, total=None)

            mods_next_pre: dict[str, Dep]
            mods_next: list[Mod]

            def resolve(mod: Mod) -> tuple[Mod, list[Dep], str]:
                progress.print(f"解析 [bright_black]{mod.title()} {mod.version()}[/bright_black]")
                return (mod, list(mod.dependencies(self.rl)), f"解析 {mod.title()} {mod.version()}")

            mods_cur = self.mods.copy()
            while mods_cur:
                mods_next_pre = {}
                mods_next = []

                # 多线程解析依赖
                futures = [self.__pool.submit(resolve, mod) for mod in mods_cur]

                for future in as_completed(futures):
                    try:
                        mod, deps, message = future.result()
                        nodes[mod.id()] = mod
                        for dep in deps:
                            if not allow_optional_mod and dep.dep_type == "optional":
                                continue
                            edges.append((
                                dep.id,
                                mod.id(),
                                dep
                            ))
                            if dep.id not in nodes and dep.id not in mods_next_pre:
                                mods_next_pre[dep.id] = dep
                    except ModError as e:
                        message = f"[yellow]警告 {e}[/yellow]"
                        progress.print(message)
                        errors.append(e)
                    except Exception:
                        progress.stop()
                        self.__pool.shutdown()

                        raise
                    finally:
                        progress.update(task_id, advance=1)
                        yield (1, progress.tasks[task_id].total, message)

                futures = [self.__pool.submit(dep.to_mod, rl=self.rl) for dep in mods_next_pre.values()]

                for future in as_completed(futures):
                    try:
                        mod = future.result()
                        if mod.id() not in nodes:
                            nodes[mod.id()] = mod
                            mods_next.append(mod)
                    except ModError as e:
                        message = f"[yellow]警告 {e}[/yellow]"
                        progress.print(message)
                        if isinstance(e, ModNotFoundError):
                            not_required_actually = all(dep.dep_type == "optional" for u, _, dep in edges if u == e.except_mod.id()) and any(dep.dep_type == "optional" for u, _, dep in edges if u == e.except_mod.id())
                            if not_required_actually:
                                self.finalmsg.append(f"[yellow]{e}，但是所有需要它的模组都不是强制需求，已从依赖中删除此模组[/yellow]")
                                edges = [(u, v, dep) for u, v, dep in edges if u != e.except_mod.id()]
                                continue
                            nodes[e.except_mod.id()] = e.except_mod
                        errors.append(e)
                        yield (0, progress.tasks[task_id].total, message)
                    except Exception:
                        progress.stop()
                        self.__pool.shutdown()

                        raise

                mods_cur = mods_next

            progress.update(task_id, completed=len(nodes), total=len(nodes))

        # 创建有向图
        dependencies: nx.DiGraph[str] = nx.DiGraph()

        required_mods = [mod.id() for mod in self.mods]

        # 存入节点
        for id, mod in nodes.items():
            attrs = {
                "label": mod.title()
            }
            try:
                mod.version_data()
            except ModError:
                attrs["color"] = "red"
            else:
                if id in required_mods:
                    attrs["color"] = "green"
                elif all(dep.dep_type == "optional" for u, _, dep in edges if u == id) and any(dep.dep_type == "optional" for u, _, dep in edges if u == id):
                    attrs["color"] = "lightgrey"
                else:
                    attrs["color"] = "lightgreen"

            self.all_mods[id] = mod
            dependencies.add_node(id, **attrs)

        # 存入边
        incompatibles: dict[str, list[str]] = {}
        for u, v, dep in edges:
            self.met_condition.add(dep.dep_type)
            attrs = edge_style[dep.dep_type]
            dependencies.add_edge(u, v, **attrs)
            if dep.dep_type == "incompatible":
                incompatibles[u] = incompatibles.get(u, [])
                incompatibles[u].append(v)

        for id, deps in incompatibles.items():
            root: Tree
            if len(deps) > 1:
                root = Tree(f"[yellow]{nodes[id].title()} 与多个模组不兼容[/yellow]")
                for dep in deps:
                    root.add(f"[yellow]不兼容 {nodes[dep].title()}[/yellow]")
            else:
                root = Tree(f"[yellow]{nodes[id].title()} 与 {nodes[deps[0]].title()} 不兼容[/yellow]")
            the_mods_that_require_this_one = [_v for _u, _v, _dep in edges if _u == id and (_dep.dep_type == "required" or _dep.dep_type == "optional")]
            if the_mods_that_require_this_one:
                if len(the_mods_that_require_this_one) > 1:
                    dep_root = Tree("但是以下模组依赖它")
                    for mod in the_mods_that_require_this_one:
                        dep_root.add(f"{nodes[mod].title()}")
                else:
                    dep_root = f"但是 {nodes[the_mods_that_require_this_one[0]].title()} 依赖它"
            errors.append(ModIncompatibleError(root))

        # 创建可视化图
        net = Network(width="100%", height="100vh", notebook=False, directed=True, cdn_resources='local')
        net.from_nx(dependencies)

        net.write_html("dependencies.html", notebook=False, open_browser=False)
        self.finalmsg.append("依赖图已保存")

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                if isinstance(error, ModNotFoundError):
                    root = Tree(f"[yellow]{error}[/yellow]")
                    the_mods_that_require_this_one = [_v for _u, _v, _dep in edges if _u == error.except_mod.id() and (_dep.dep_type == "required" or _dep.dep_type == "optional")]
                    if the_mods_that_require_this_one:
                        if len(the_mods_that_require_this_one) > 1:
                            dep_root = Tree("但是以下模组依赖它")
                            for mod in the_mods_that_require_this_one:
                                dep_root.add(f"{nodes[mod].title()}")
                        else:
                            dep_root = f"但是 {nodes[the_mods_that_require_this_one[0]].title()} 依赖它"
                        root.add(dep_root)
                    e_tree.add(root)
                    continue
                for arg in error.args:
                    if isinstance(arg, Tree):
                        e_tree.add(arg)
                    else:
                        e_tree.add(f"[yellow]{arg}[/yellow]")
            self.finalmsg.append(e_tree)
            dep_desc = Tree("请参阅依赖图 [bold]dependencies.html[/bold]")
            dep_desc.add("[bold][#008000]绿色[/][/bold]节点代表清单中的模组")
            if "required" in self.met_condition:
                dep_desc.add("[bold][#90EE90]淡绿色[/][/bold]节点/箭头代表必要依赖")
            if "optional" in self.met_condition:
                dep_desc.add("[bold][#D3D3D3]灰色[/][/bold]节点/箭头代表可选依赖")
            if "incompatible" in self.met_condition:
                dep_desc.add("[bold][#FF0000]红色[/][/bold]节点/箭头代表冲突项目")
            self.finalmsg.append(dep_desc)

        return errors

    def get_download_link(self) -> ProgressGen:
        errors: list[Exception] = []
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self.console
        ) as progress:
            task_id = progress.add_task("获取下载链接", True, len(self.all_mods))

            # 多线程查找下载链接
            result = yield from self.handle_future(
                [self.__pool.submit(mod.get_version, progress, self.rl) for mod in self.all_mods.values()], 
                progress, 
                task_id
            )
            errors.extend(result)

            
        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
        return errors

    def download_mods(self, mod_dir: str) -> ProgressGen:
        # 转换为路径
        mod_path = Path(mod_dir)
        # 创建下载目录
        mod_path.mkdir(parents=True, exist_ok=True)

        errors: list[Exception] = []
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self.console
        ) as progress:
            task_id = progress.add_task("下载模组", True, len(self.all_mods))

            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                transient=True,
                console=self.console
            ) as download_progress:
                # 下载工具函数
                def download(mod: Mod) -> str:
                    if not mod.file_data:
                        raise ModError(f"模组 {mod.slug_or_id} 还未初始化")
                    _task_id = download_progress.add_task(mod.file_data["filename"], True, mod.file_data["size"])

                    # 哈希器
                    hasher = hashlib.sha512()
                    # 缓冲区
                    buf = BytesIO()

                    self.rl.wait()
                    with requests.get(mod.file_data["url"], stream=True) as r:
                        r.raise_for_status()
                        for chunk in r.iter_content(chunk_size=1024):
                            if not chunk:
                                continue
                            buf.write(chunk)
                            hasher.update(chunk)
                            download_progress.update(_task_id, advance=len(chunk))

                    # 计算哈希
                    actual = hasher.hexdigest().lower()
                    if actual != mod.file_data["hashes"]["sha512"]:
                        buf.close()
                        raise ModError(f"{mod.file_data["filename"]} 的哈希校验失败")

                    # 写入文件
                    buf.seek(0)
                    with open(mod_path / mod.file_data["filename"], "wb") as f:
                        f.write(buf.getbuffer())
                    buf.close()

                    try:
                        message = f"保存为 [bright_black]{(mod_path / mod.file_data["filename"]).relative_to(".")}[/bright_black]"
                        progress.print(message)
                    except ValueError:
                        message = f"保存为 [bright_black]{(mod_path / mod.file_data["filename"])}[/bright_black]"
                        progress.print(message)

                    download_progress.remove_task(_task_id)
                    return message

                # 多线程下载模组
                result = yield from self.handle_future(
                    [self.__pool.submit(download, mod) for mod in self.all_mods.values()], 
                    progress, 
                    task_id
                )
                errors.extend(result)

        if errors:
            e_tree = Tree("下载模组时遇到问题")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
        return errors