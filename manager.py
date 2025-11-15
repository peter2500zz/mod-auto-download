from typing import Literal
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

from moderr import ModError, ModNotFoundError
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

    def handle_future(self, futures: list[Future], progress: Progress, task_id: TaskID):
        errors: list[ModError] = []
        for future in as_completed(futures):
            try:
                future.result()
            except ModError as e:
                progress.print(f"[yellow]警告 {e}[/yellow]")
                errors.append(e)
            except Exception:
                # 真的很异常的异常应当上报
                progress.stop()
                self.__pool.shutdown()

                raise
            finally:
                progress.update(task_id, advance=1)
        return errors

    def init_mod(self, version: str, loader: str, require_client: bool, require_server: bool) -> bool:
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
            errors.extend(self.handle_future(
                [self.__pool.submit(mod.init, self.target_version, self.target_loader, require_client, require_server, progress, self.rl) for mod in self.mods], 
                progress, 
                task_id
            ))

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
            return False
        return True

    def check_version(self) -> bool:
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
            errors.extend(self.handle_future(
                [self.__pool.submit(mod.query_version, progress, self.rl) for mod in self.mods], 
                progress, 
                task_id
            ))

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
            return False
        return True

    def resolve_dependencies(self, allow_optional_mod: bool = False) -> bool:
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

            def resolve(mod: Mod) -> tuple[Mod, list[Dep]]:
                progress.print(f"解析 [bright_black]{mod.title()} {mod.version()}[/bright_black]")
                return (mod, list(mod.dependencies(self.rl)))

            mods_cur = self.mods.copy()
            while mods_cur:
                mods_next_pre = {}
                mods_next = []

                # 多线程解析依赖
                futures = [self.__pool.submit(resolve, mod) for mod in mods_cur]

                for future in as_completed(futures):
                    try:
                        mod, deps = future.result()
                        nodes[mod.id()] = mod
                        for dep in deps:
                            edges.append((
                                dep.id,
                                mod.id(),
                                dep
                            ))
                            mods_next_pre[dep.id] = dep
                    except ModError as e:
                        progress.print(f"[yellow]警告 {e}[/yellow]")
                        errors.append(e)
                    except Exception:
                        progress.stop()
                        self.__pool.shutdown()

                        raise
                    finally:
                        progress.update(task_id, advance=1)

                futures = [self.__pool.submit(dep.to_mod) for dep in mods_next_pre.values()]

                for future in as_completed(futures):
                    try:
                        mod = future.result()
                    except ModError as e:
                        if isinstance(e, ModNotFoundError):
                            nodes[e.except_mod.id()] = e.except_mod
                        progress.print(f"[yellow]警告 {e}[/yellow]")
                        errors.append(e)
                    except Exception:
                        progress.stop()
                        self.__pool.shutdown()

                        raise
                    finally:
                        if mod.id() not in nodes:
                            mods_next.append(mod)

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
                elif all(dep.dep_type == "optional" for _, v, dep in edges if v == id) and any(dep.dep_type == "optional" for _, v, dep in edges if v == id):
                    attrs["color"] = "lightgrey"
                else:
                    attrs["color"] = "lightgreen"

            self.all_mods[id] = mod
            dependencies.add_node(id, **attrs)

        # 存入边
        for u, v, dep in edges:
            attrs = edge_style[dep.dep_type]
            dependencies.add_edge(u, v, **attrs)

        # 创建可视化图
        net = Network(width="100%", height="100vh", notebook=False, directed=True, cdn_resources='local')
        net.from_nx(dependencies)

        net.write_html("dependencies.html", notebook=False, open_browser=False)
        self.finalmsg.append("依赖图已保存")

        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
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

            return False
        return True

    def download_mods(self, mod_dir: str):
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
            task_id = progress.add_task("获取下载链接", True, len(self.all_mods))

            # 多线程查找下载链接
            errors.extend(self.handle_future(
                [self.__pool.submit(mod.get_version, progress, self.rl) for mod in self.all_mods.values()], 
                progress, 
                task_id
            ))

            
        if errors:
            e_tree = Tree("由于以下原因，将不会继续")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
            return False

        errors = []
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
                def download(mod: Mod):
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

                    progress.print(f"保存为 [bright_black]{(mod_path / mod.file_data["filename"]).relative_to(".")}[/bright_black]")

                    download_progress.remove_task(_task_id)

                # 多线程下载模组
                errors.extend(self.handle_future(
                    [self.__pool.submit(download, mod) for mod in self.all_mods.values()], 
                    progress, 
                    task_id
                ))

        if errors:
            e_tree = Tree("下载模组时遇到问题")
            for error in errors:
                e_tree.add(f"[yellow]{error}[/yellow]")
            self.finalmsg.append(e_tree)
            return False